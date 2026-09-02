# Architecture

The bot is a long-polling Telegram application with four concerns kept apart: deciding
whether to speak, deciding how expensively, getting an answer out of whichever provider
is alive, and sounding like the same character while doing it.

```mermaid
flowchart TB
    subgraph tg["Telegram"]
        A["app.py<br/>handlers, lifecycle, keepalive"]
    end
    subgraph pipe["Per message"]
        B["chat.py<br/>the pipeline"]
        C["participation.py<br/>manual · auto · smart"]
        D["budget.py<br/>caps and limits"]
        E["routing.py<br/>fast · think · search · serious"]
        F["persona.py<br/>layered prompt"]
        G["media.py<br/>images · video · voice"]
    end
    subgraph out["Getting an answer"]
        H["llm.py<br/>failover, key rotation, retries"]
        I["providers.py + services.py<br/>11 services, their keys and health"]
        J["offline.py<br/>answers that need no model"]
    end
    subgraph state["State"]
        K["memory.py<br/>history and notes"]
        L["db.py<br/>SQLite, schema v3"]
        M["crypto.py<br/>encrypted keys"]
    end
    A --> B
    B --> C & D & E & G
    E --> F --> H
    H --> I
    H -.->|nothing reachable| J
    B --> K --> L
    I --> L
    M --> L
    N["admin/<br/>the owner's panel"] --> L
```

## Request lifecycle

`chat.handle_message` is the whole pipeline, in this order:

1. **Filter** — other bots, blocked people and muted chats are dropped before anything
   else. An empty message with no attachment is not a message.
2. **Remember** — every message enters the chat history exactly once, whether or not the
   bot answers, so it hears the conversation it is sitting in. Media is stored as a short
   placeholder, never as base64. A counting row is written for the chat and the sender.
3. **Address check** (`text.is_addressed`) — replies to the bot, `@mentions`, text mentions
   and the name "astolfo" all count. Private chats always count.
4. **Budget and limits** (`budget.BudgetTracker.check`) — the monthly cap, the per-chat and
   per-person daily call limits, then the daily spend ladder. Returns an `Allowance` that
   can block the turn or strip capabilities from it. A limit set on one group or one person
   beats the global one.
5. **Participation** — when not addressed, `participation.should_join` applies the chat's
   mode, the cooldown and the reply chance. Media raises the chance.
6. **The offline shortcut** — if the bot is addressed with plain text and no service is
   usable, the turn is answered from `offline.py` and ends here rather than spending a
   round-trip to learn what it already knows.
7. **Response cache** — an identical recent message in the same chat is answered from cache
   with no model call.
8. **Media collection** (`media.collect`) — downloads and converts attachments. A bundle
   the current model cannot read is dropped with an instruction to say so honestly.
9. **Routing** (`routing.Router.decide`) — heuristics first, the LLM dispatcher only when
   they are unsure and the budget allows.
10. **Prompt assembly** (`chat.build_messages`) — static persona, dynamic context, trimmed
    history, optional persona reminder, current turn.
11. **Model call** (`llm.LLMClient.chat`) — service failover, key rotation, model fallbacks,
    retries, optional web search.
12. **Quality guard** (free mode only) — a reply that leaks the prompt, echoes the question
    or repeats the last one is not sent; the model is retired and one other is asked.
13. **Fallback** — with no completion at all, `offline.py` is tried before an apology, so a
    greeting is still answered when every model is gone.
14. **Post-processing** (`text.polish`) — strip markdown, name prefixes and assistant-isms,
    split to Telegram's length limit, append sources for search answers.
15. **Background** — fold old turns into long-term notes.

Steps 7 to 13 run under a per-chat lock, so a busy chat never produces two overlapping
replies, and the lock is released before the reply is sent. A chat already composing a
reply drops the new message rather than queueing it.

## Prompt structure

The prompt is deliberately split in two so that providers can cache the expensive half.

```
messages[0]  system   static persona   identity, voice, canon, group rules, language,
                                       banned behaviour, meta answers, truthfulness,
                                       few-shot examples, output rules   (~10 KB, stable)
messages[1]  system   dynamic context  response mode, media rules, participants, notes
messages[2:] history  trimmed to HISTORY_CHAR_BUDGET characters
messages[-1] user     current turn (text, or text + image/audio parts)
```

`messages[0]` is byte-identical for every turn of the same chat type and locale, which is
what makes provider-side prefix caching effective. For Anthropic models an explicit
`cache_control` breakpoint is attached; Gemini and OpenAI cache stable prefixes on their
own. A test asserts the static block does not change between turns.

Free mode swaps in a compact persona under a quarter of the size: a 9-to-30B model drowns
in the full one and starts quoting the scaffolding back.

## Response modes

| Mode | Trigger | Model | Temperature | Reasoning | Web |
| --- | --- | --- | --- | --- | --- |
| `fast` | small talk, reactions, short messages | `MODEL_FAST` | 0.95 | disabled | no |
| `think` | code, math, comparisons, explanations | `MODEL_THINK` | 0.55 | `THINK_EFFORT` | no |
| `search` | prices, news, versions, dated facts | `MODEL_SEARCH` | 0.25 | off | yes |
| `serious` | distress signals | `MODEL_THINK` | 0.60 | low | no |

Attachments override the model with `MODEL_MEDIA` unless the turn is already `think` or
`serious`. `/mode` pins a mode for a chat, and the budget can force `fast` from above.

## The provider stack

`providers.py` holds the presets, `services.py` holds what the owner has saved, and
`llm.py` is the client that uses both. `discover()` merges three sources in increasing
priority: the presets in code, the environment, and the database.

```mermaid
flowchart LR
    R[request] --> S1[service 1]
    S1 -->|401 / 403| K[next key]
    K --> S1
    S1 -->|429 / 402| S2[service 2]
    S1 -->|400 / 404| S2
    S2 --> S3[service 3 …]
    S1 & S2 & S3 --> OK([answer])
    S3 -->|all spent| F([no completion])
```

- **Keys live on the request, not the client.** The `Authorization` header is built per
  call, which is what makes several keys per one service possible at all.
- **A refused key rests, the service does not.** 401 or 403 rests that key for a day,
  records what it was told, and the next key takes over inside the same turn.
- **A spent service rests.** 429 or 402 rests the whole service for as long as the response
  asks, and the turn continues on the next one.
- **A rejected request moves on.** A 400 or a 404 is the service's opinion of the request,
  not of the bot, so the next service is offered it. A 404 first walks the rest of that
  service's model list, because a renamed model id is the common cause. Every 4xx body is
  logged.
- **Health survives a restart.** Rests are stored as wall-clock time, so a quota that runs
  until tomorrow is still known tomorrow. Nothing probes to recover; ordinary traffic
  retries a service once its rest is over.
- **Only OpenRouter gets OpenRouter's fields.** The fallback model list, the search plugin,
  the provider-sort hint and usage accounting are gated behind `openrouter_extensions`,
  because a plain OpenAI-compatible endpoint answers an unknown field with a 400.
- **Model ids are verified at startup.** Every non-catalog service is asked for its own
  listing, and configured ids it does not offer are dropped with a log line naming what it
  does offer.

`PINNED_SERVICE`, or **📌 use only this** in the panel, stops the chain on one service.

## Participation

Three modes, global or per group, resolved on every unaddressed message:

| Mode | Behaviour |
|---|---|
| `manual` | answers only when replied to, mentioned, or called by name |
| `auto` | also joins conversations on its own, at `GROUP_REPLY_CHANCE` |
| `smart` | auto while that makes sense, manual when it does not |

Smart drops to manual when every service is resting, when 70% of the daily budget is gone,
or when the chat is moving faster than twelve messages a minute — measured from a bounded
deque of arrival times, not a counter that has to be reset. Being spoken to always gets an
answer in every mode; silence is what `/mute` is for.

## Memory

- **Short term** — a bounded deque per chat, trimmed to a character budget when building a
  prompt so a pasted wall of text cannot blow up input cost. Consecutive turns from the
  same role are merged, so a run of group messages reads as overheard conversation rather
  than a queue of questions.
- **Long term** — once history is full, the oldest turns are folded into notes (regulars,
  running jokes, ongoing situations) by a cheap model in the background.
- **Eviction** — chats idle beyond `CHAT_TTL` are dropped, and an LRU bound caps memory at
  `MAX_CHATS`; a chat that is mid-reply is never evicted.

## Persistence

`data/` holds everything stateful, and the directory itself is created 700:

| Path | What is in it |
|---|---|
| `astolfo.db` | chats, people, settings, services, keys, audit — SQLite in WAL mode, mode 600 |
| `secret.key` | the Fernet key that decrypts the stored API keys, mode 600 |
| `usage.json` | daily cost buckets, kept 35 days |
| `control/` | the spool the root helper watches, when it is installed |

The schema is versioned with `PRAGMA user_version` (currently **3**) and migrated
additively — a column is added only if it is not already there, so re-running a migration
is harmless. A file written by a newer version is left alone with a warning rather than
migrated backwards.

| Table | Rows |
|---|---|
| `chats` | one per chat: title, kind, activity counts, mute, mode, daily limit |
| `users` | one per person seen: name, first and last seen, blocked |
| `members` | who is in which chat, and how active |
| `settings` | any setting overridden from the panel; beats the environment |
| `secrets` | the legacy single-key store, emptied by the v2 migration |
| `services` | a preset override, or a service the code has never heard of |
| `credentials` | keys: encrypted value, label, order, health, what it last said |
| `service_usage` | requests, failures, tokens and cost per service per day |
| `audit` | who pressed what in the panel |

**Message text is never written to disk.** History lives in memory; the database keeps
counts. `tests/test_security.py` opens the database file and its write-ahead log and greps
them for the text of a message it just sent.

## Answering with no model

`offline.py` handles what needs no knowledge: greetings, goodbyes, thanks, "how are you",
"who are you", the time, the date, `ping`, and plain arithmetic — parsed with `ast` and
evaluated over four operators, never `eval`. Everything else returns `None`, and the
caller says "my brain is offline" rather than guessing. Nothing in the module invents a
fact; a rule that is not sure declines.

It is used twice: as a shortcut before the model call when no service is usable, and as a
last resort after one that produced nothing.

## Concurrency and lifecycle

- `concurrent_updates(True)` with `AIORateLimiter`, so slow turns do not block the queue
  while Telegram's own rate limits are respected.
- One `asyncio.Lock` per chat guards the reply-composing section.
- An autosave task flushes dirty state every 120 seconds, plus once on shutdown; a Stars
  payment is written immediately rather than waiting for it.
- The keepalive HTTP server answers `GET /` with 200 while the process is alive, for
  platforms that need a URL to ping.
- Settings changed from the panel are adopted in place: a new client is built, the old one
  is closed, and the chat store is deliberately kept so no group forgets mid-reply.

## Failure behaviour

| Failure | Behaviour |
| --- | --- |
| Unknown model id | detected at startup, replaced from `FALLBACK_MODELS` |
| Service returns 404 | walk the rest of its model list, then move to the next service |
| Service returns another 4xx | log the body, offer the request to the next service |
| Key refused (401 / 403) | rest that key for a day, rotate to the next one, same turn |
| Quota or rate limit (429 / 402) | rest the whole service for as long as it asks |
| 5xx | exponential backoff with jitter, honouring `Retry-After` |
| Provider rejects `reasoning` or the web plugin | parameter dropped, request retried |
| Every service resting | offline answers, or one honest "my brain is offline" |
| No completion after retries | offline answer if there is one, else one in-character apology, only if addressed |
| Free model returns nothing or garbage | model retired, one other tried, then the turn ends quietly |
| ffmpeg missing | video falls back to thumbnails, voice is honestly declined |
| No vision model available | attachment dropped, the bot says so in one line |
| Dispatcher returns garbage | the heuristic decision is used |
| Budget exceeded | degrade, then stop with one notice per hour |
| Update fails to start | the server rolls back to the previous commit on its own |

## Testing

The suite is offline by construction. Provider calls go through `httpx.MockTransport`, so
the exact request body each service receives is asserted without a network or a key —
including which fields a non-OpenRouter service must *not* be sent. Fixtures in
`tests/conftest.py` build a `Runtime` against a temporary directory, so the database,
the encryption key and the panel are all exercised for real.
