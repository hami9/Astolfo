# Architecture

## Request lifecycle

1. **Filter** (`chat.handle_message`) — ignore other bots, muted chats and empty messages.
2. **Remember** — every message enters the chat history exactly once, whether or not the
   bot answers. Media is stored as a short placeholder, never as base64.
3. **Address check** (`text.is_addressed`) — replies to the bot, `@mentions`, text mentions
   and the name "astolfo" all count as being addressed. Private chats always count.
4. **Budget check** (`budget.BudgetTracker.check`) — returns an `Allowance` that can block
   the turn or strip capabilities from it.
5. **Participation** — when not addressed, a per-chat cooldown and probability decide
   whether to join. Media raises the probability.
6. **Response cache** — an identical recent message in the same chat is answered from
   cache with no model call.
7. **Media collection** (`media.collect`) — downloads and converts attachments.
8. **Routing** (`routing.Router.decide`) — heuristics first, LLM dispatcher only when
   they are unsure and the budget allows.
9. **Prompt assembly** (`chat.build_messages`) — static persona, dynamic context, trimmed
   history, optional persona reminder, current turn.
10. **Model call** (`llm.LLMClient.chat`) — retries, model fallbacks, optional web search.
11. **Post-processing** (`text.polish`) — strip markdown, name prefixes and assistant-isms,
    split to Telegram's length limit, append sources for search answers.
12. **Background** — fold old turns into long-term notes.

Steps 6 to 11 run under a per-chat lock, so a busy chat never produces two overlapping
replies, and the lock is released before the reply is sent.

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

## Response modes

| Mode | Trigger | Model | Temperature | Reasoning | Web |
| --- | --- | --- | --- | --- | --- |
| `fast` | small talk, reactions, short messages | `MODEL_FAST` | 0.95 | disabled | no |
| `think` | code, math, comparisons, explanations | `MODEL_THINK` | 0.55 | `THINK_EFFORT` | no |
| `search` | prices, news, versions, dated facts | `MODEL_SEARCH` | 0.25 | off | yes |
| `serious` | distress signals | `MODEL_THINK` | 0.60 | low | no |

Attachments override the model with `MODEL_MEDIA` unless the turn is already `think` or
`serious`.

## Memory

- **Short term** — a bounded deque per chat, trimmed to a character budget when building a
  prompt so a pasted wall of text cannot blow up input cost.
- **Long term** — once history is full, the oldest turns are folded into notes (regulars,
  running jokes, ongoing situations) by a cheap model in the background.
- **Persistence** — `data/state.json` keeps per-chat settings and notes; message text is
  never written to disk. `data/usage.json` keeps daily cost buckets for 35 days.
- **Eviction** — chats idle beyond `CHAT_TTL` are dropped, and an LRU bound caps memory at
  `MAX_CHATS`; a chat that is mid-reply is never evicted.

## Failure behaviour

| Failure | Behaviour |
| --- | --- |
| Unknown model id | detected at startup, replaced from `FALLBACK_MODELS` |
| 429 / 5xx | exponential backoff with jitter, honouring `Retry-After` |
| Provider rejects `reasoning` or the web plugin | parameter dropped, request retried |
| Invalid API key | no retries, logged clearly |
| No completion after retries | one in-character apology, only if addressed |
| ffmpeg missing | video falls back to thumbnails, voice is honestly declined |
| Dispatcher returns garbage | heuristic decision is used |
| Budget exceeded | degrade, then stop with one notice per hour |
