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
