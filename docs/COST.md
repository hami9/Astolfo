# Credit control

Every optimisation here is on by default. `/usage` reports what they are saving.

## Where the money goes

A group bot's cost is dominated by **input tokens**, not output: the persona prompt plus
chat history is sent on every single turn, while replies are two lines long. The design
follows from that.

## 1. Route cheap turns to cheap models

Most group messages are banter. Regex heuristics classify them for free and send them to
`MODEL_FAST` with reasoning disabled (`reasoning: {"max_tokens": 0}`), so no thinking
tokens are billed for "lol". Only genuinely hard turns reach `MODEL_THINK`, and only
verifiable ones pay for web search.

## 2. Do not pay to decide

The LLM dispatcher is the routing safety net, not the routing mechanism:

- it is skipped entirely when the heuristics are confident (`>= 0.85`),
- it is skipped for messages shorter than `ROUTER_MIN_WORDS`,
- it runs on `MODEL_ROUTER` with an 80-token ceiling,
- its verdicts are cached by normalised text for `ROUTER_CACHE_TTL`,
- it is disabled automatically when the budget is tight.

## 3. Keep the prompt prefix cacheable

The persona block is byte-identical across turns, so providers serve it from their prompt
cache at a large discount. Anything that varies per turn — mode, participants, notes —
lives in a second, much smaller system message after it. Anthropic models get an explicit
`cache_control` breakpoint; Gemini and OpenAI cache stable prefixes implicitly.
`/usage` shows the resulting cache hit rate.

## 4. Bound the history

History is trimmed by `HISTORY_CHAR_BUDGET` characters rather than a message count, so one
pasted wall of text cannot multiply the cost of every following turn. Attachments are kept
in history as placeholders, never as base64.

## 5. Never answer the same thing twice

An identical message in the same chat within `RESPONSE_CACHE_TTL` is answered from cache
with no model call. Only `fast`, non-media, non-search replies are cached, so time-
sensitive answers never go stale.

## 6. Shrink media before sending it

Images are downscaled to `IMAGE_MAX_DIM` and re-encoded as JPEG at `IMAGE_QUALITY`.
Videos and GIFs are reduced to `VIDEO_FRAMES` sampled frames; audio becomes 16 kHz mono
48 kbps mp3, capped at `MAX_AUDIO_SECONDS`.

## 7. Talk less

`GROUP_REPLY_CHANCE` and `REPLY_COOLDOWN` bound unprompted participation, `MAX_TOKENS_FAST`
keeps replies short, and one reply is generated per chat at a time.

`REPLY_MODE` decides how that budget is spent. `manual` answers only when spoken to, which
is the cheapest the bot gets while still being useful. `smart` (the default) is `auto`
until spending or traffic says otherwise: it drops to manual once 70% of the daily budget
is gone, while every service is resting, or while the chat is moving faster than a dozen
messages a minute — an uninvited reply into a fast conversation is the least valuable call
the bot makes. Any group can be set differently from **panel → groups**.

## 8. Cap the loud ones individually

A daily budget is global, so one busy group can spend everyone's. `CHAT_DAILY_CALL_LIMIT`
and `USER_DAILY_CALL_LIMIT` cap model calls per day, and **panel → groups** and
**panel → people** set either one on a single group or a single person, which beats the
global value. Both screens show how much of the cap is used today. A capped chat still
gets everything in the next section for free.

## 9. Choose the model rather than inheriting it

`/panel → models` lists what the service offers with its context window and price, and a
press assigns one to a job. The cheapest useful setting is usually a small model on
`fast` and `router` — which together take most of the traffic — with the expensive one
kept for `think`. **📊 token usage** then shows what each one actually did today: calls,
tokens in, tokens out, cost. On free models every cost is zero, so the tokens are the
only thing that tells you which model is carrying the group.

## 10. Answer some things for nothing

Greetings, goodbyes, thanks, "who are you", the time, the date and plain arithmetic are
answered from `offline.py` with no model call at all when no service is usable. It is a
small share of traffic, but it is the share that would otherwise waste a call to say
"hello" back — and it is what keeps the bot present rather than silent when the allowance
is gone.

## Budgets and degradation

Set a cap and the bot manages itself:

```bash
DAILY_BUDGET_USD=0.50
MONTHLY_BUDGET_USD=10
CHAT_DAILY_CALL_LIMIT=200   # optional per-chat cap
```

| Spend | State | Behaviour |
| --- | --- | --- |
| < 80% | `full` | everything enabled |
| 80–100% | `cheap` | fast model only, no web search, no dispatcher |
| 100–120% | `addressed_only` | answers only when mentioned or replied to, cheap |
| ≥ 120% | `stopped` | no model calls; one in-character notice per hour |

Costs come from OpenRouter's `usage.cost` field, so `TRACK_COST=1` is required for budgets
to work. Totals are stored in `data/usage.json` and survive restarts, so a restart loop
cannot reset the cap.

## Tuning for a very small budget

```bash
MODEL_THINK=google/gemini-2.5-flash    # skip the expensive tier entirely
ROUTER_LLM=0                           # heuristics only
GROUP_REPLY_CHANCE=0.15
HISTORY_CHAR_BUDGET=3000
MAX_TOKENS_FAST=180
SUMMARIES=0
PROVIDER_SORT=price
```

## Running with no credit at all

`FREE_MODE=1` switches the bot onto OpenRouter's zero-cost models. It does not pick
from a hardcoded list — free models appear and disappear constantly — but reads the
model catalog at startup and uses everything priced at zero, longest context first.
Every request also carries the rest of the free pool as fallbacks, so a model that is
rate limited is swapped out server-side instead of failing the message.

The binding limit changes in this mode: free models are rationed by **requests per
minute and per day**, not by tokens. So the preset switches off everything that costs
an extra call per message rather than everything that costs tokens:

| Setting | Free mode | Why |
| --- | --- | --- |
| `WEB_SEARCH` | off | the search plugin is billed even when the model is free |
| `ROUTER_LLM` | off | the dispatcher is a second call per ambiguous message |
| `SUMMARIES` | off | folding notes is another call |
| `GROUP_REPLY_CHANCE` | 0.12 | fewer unprompted replies |
| `REPLY_COOLDOWN` | 45s | spreads requests out |
| `VIDEO_FRAMES` / `IMAGE_MAX_DIM` | 2 / 768 | free vision models are small |
| `FREE_RPM` | 8 | the whole bot shares one per-minute allowance |

Any variable you set explicitly still wins over the preset.

Selection is by capability, not just price. A model qualifies only if every priced
dimension is zero *and* its whole output is text — generators advertise themselves as
`text+image->text+audio`, so a music model reports text output too and would otherwise
rank first on context length — and classifier and retrieval models are skipped by name.

**The free allowance belongs to the account, not to a model.** A free-tier 429 comes
back from every model at once, whichever provider it is from, so touring the pool only
spends five requests to learn the same thing and pushes the account further over the
limit. A 429 therefore pauses the whole bot for as long as the response asks (a minute
by default), and requests are refused instantly while that pause lasts rather than
queueing up. `FREE_RPM` spaces requests out globally so the limit is approached less
often; every chat draws on the same budget, so the gap is shared across all of them.
A throttled turn is not announced in the chat, since it is routine and clears itself.

**Models rotate when they stop being useful.** A rate limit, an exhausted quota, or a
model that answers with nothing at all puts it to rest (ten minutes for a rate limit or
an empty answer, six hours for a quota) and the turn continues on the next model
immediately, without waiting. Before blaming a silent model the optional reasoning
parameter is dropped and it gets one more try, since some models answer an unsupported
parameter with silence rather than an error. A resting model is skipped on
later messages until its cooldown passes, and only when the whole pool is spent does the
bot report that it has run out.

**Images, GIFs and voice work** whenever the catalog offers a free model with the right
input. GIFs and videos are sampled into still frames first, so they need image support
rather than video support, and some free models accept audio, so voice messages can work
too. When nothing in the pool can read an attachment it is dropped and the bot says so
honestly rather than failing the turn.

`/status` reports which pool is in use and whether images are available.

### Keeping a small model in character

Free models are a fraction of the size of the paid ones and fail in recognisable ways,
so free mode adds three guards:

- **A compact persona.** The full layered prompt is around 10 KB; a 9-to-30B model
  drowns in it and starts quoting the scaffolding back. Free mode sends a version under
  a quarter of that size, keeping the identity, the hard limits and one example.
- **Reply validation.** A reply that leaks the prompt, answers in `assistant:`
  transcript format, echoes the question, or repeats the previous reply is not sent.
  The model is retired and one other model is asked; if that also fails the turn ends
  quietly rather than forwarding nonsense.
- **Behaviour ranking.** Models that return nothing or write rejected replies sink in
  the pool, so the ones that behave are tried first. They stay available as a last
  resort rather than being dropped.

None of this applies on paid models, which are trusted and answered in one call.

## Letting the chat pay for it

`/donate` sends a Telegram Stars invoice. Stars need no payment provider, no merchant
account and no card from the sender, which is the only rail that works for every user
in a group; the balance is withdrawable from the bot account afterwards. `/donate 50`
picks an amount, and `DONATE_AMOUNTS` sets the suggestions, the first being the
default. Set `DONATE=0` to hide the command entirely.

Received stars are counted in the same daily record as spending and shown by `/usage`,
and a payment is written to disk immediately rather than at the next autosave.

## Stacking several services

`PROVIDERS` names the services to draw on, in order, and each is used only when its
key is present:

```bash
PROVIDERS=openrouter,google,groq
GOOGLE_API_KEY=...
GROQ_API_KEY=...
```

When one runs out of allowance the turn continues on the next rather than failing,
and the spent one is rested so later messages skip it. Every service keeps its own
quota, so stacking them multiplies the daily budget.

This is not the same as holding several accounts at one service to get past its
limits: that breaks the terms every provider sets, and since all the traffic comes
from one server with one usage pattern it typically ends with every account closed,
including any that has credit on it.

Only OpenRouter is sent OpenRouter's own request fields — the fallback model list,
the web-search plugin, the provider-sort hint and the usage accounting. Everything
else gets a plain OpenAI-compatible body, because an ordinary endpoint answers an
unknown field with a 400 rather than ignoring it. A request one service rejects is
offered to the next, and so is a key one service refuses, so a stale `GOOGLE_API_KEY`
costs a log line rather than the turn.

At startup every service that is not the catalog one is asked for its own model
listing, and configured ids it does not offer are dropped with a log line naming
what it does offer. If an id survives that and still answers 404 when called, the
next id on the list is tried before the service is passed over — the response body
is always logged, so a wrong endpoint or a renamed model is one `journalctl` away
rather than a guess.

### The services the bot already knows

| Service | Where a key comes from | Billing |
|---|---|---|
| `openrouter` | openrouter.ai | free models discovered automatically |
| `google` | aistudio.google.com | free tier |
| `groq` | console.groq.com | free tier |
| `cerebras` | cloud.cerebras.ai | free tier |
| `mistral` | console.mistral.ai | free tier |
| `cohere` | dashboard.cohere.com | trial keys are free and rate limited |
| `sambanova` | cloud.sambanova.ai | free tier |
| `huggingface` | huggingface.co/settings/tokens | monthly credit, then paid |
| `github` | a GitHub token with models access | free within the account's limits |
| `deepinfra` | deepinfra.com | pay as you go after the signup credit |
| `aimlapi` | aimlapi.com | small free allowance, then paid |

The billing column is what each service advertises, and the panel repeats it beside
the name so a paid one is never a surprise. It is not a promise: what a key
actually grants is what its **test** button reports.

Endpoints and model names here are the ones these services publish. When one
changes something, the bot notices at startup — it asks each service for its own
model list and logs anything it no longer offers — and both the endpoint and the
models are editable from the panel, so it is a two-minute fix rather than a
release. Free mode does not protect against a paid service: put the free ones
above the paid ones in the order, or switch the paid ones off until you want them.

All of this is managed from `/panel → services` rather than the environment: keys
(more than one per service), the order, whether a service is used at all, its
endpoint and its models, plus what each one cost today. `PROVIDERS` and the
`<NAME>_API_KEY` variables still work and are still the fallback, so nothing has
to move.

Each service is asked for its own models — the one that publishes a catalog gets the
discovered free list, the rest get what their preset or `<NAME>_MODELS` names. Presets
carry only the endpoint and some model names, and both are overridable
(`GROQ_BASE_URL`, `GOOGLE_MODELS`, …), so a service changing either is a
configuration edit rather than a new release. `/status` lists the services in use.
