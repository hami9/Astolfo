# Astolfo

A Telegram bot that behaves like a member of your group chat instead of an assistant.
It decides for itself whether a message deserves an instant reply, real reasoning, or a
web search, it reads photos, stickers, GIFs, videos and voice messages, and it keeps its
own spending under control.

Text in, text out — the bot never generates images or audio.

```
message ──▶ participation ──▶ budget ──▶ cache ──▶ router ──▶ model ──▶ reply
             (addressed?)      (caps)     (hit?)    (mode)     (per-mode)
```

## Features

**Layered persona.** The system prompt is built from ordered layers: narrative identity,
voice markers, canon anchors, group behaviour, language mirroring, banned assistant-isms,
an in-character answer to "are you a bot?", and a highest-priority truthfulness layer.
Traits are written as identity rather than commands, with few-shot examples in four moods
and a slim reminder re-injected periodically so the voice does not flatten in long chats.

**Adaptive routing.** Regex heuristics classify most messages for free — small talk goes
to `fast`, technical questions to `think`, time-sensitive facts to `search`, and distress
to `serious`. Only ambiguous messages reach a small LLM dispatcher, and its verdicts are
cached. The chosen mode drives the model, temperature, token ceiling, reasoning budget and
whether web search runs.

**Grounded answers.** Search-mode replies run at low temperature over live web results and
cite their sources. Canon anchors keep the persona from inventing its own lore, and the
truthfulness layer makes "I don't know" the in-character answer rather than a failure.

**Multimodal input.** Photos and stickers are downscaled and encoded, GIFs, videos and
video notes are sampled into frames with ffmpeg (falling back to Telegram thumbnails),
and voice messages are transcoded to mono mp3.

**Credit controls.** Per-call cost is recorded from OpenRouter and persisted. As spend
approaches the daily cap the bot degrades instead of dying: cheap model only, then replies
only when addressed, then a polite stop. See [docs/COST.md](docs/COST.md).

**Group manners.** Reply chance with a cooldown, guaranteed answers on mention or reply,
per-chat settings, long-term notes about running jokes, and admin-gated commands.

## Quick start

```bash
git clone https://github.com/hami9/Astolfo && cd Astolfo
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add your two keys
python main.py
```

Install `ffmpeg` for voice and video analysis (`apt install ffmpeg` / `brew install ffmpeg`).

**Turn off Group Privacy** in BotFather so the bot can see normal group messages:
`/mybots` → your bot → *Bot Settings* → *Group Privacy* → **Turn off**, then remove and
re-add the bot to the group.

Deployment on Replit, Docker or a plain VPS is covered in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Commands

| Command | Description |
| --- | --- |
| `/start`, `/help` | introduction and usage |
| `/chance 0-100` | how often the bot joins conversations unprompted (admin) |
| `/mode auto\|fast\|think\|search` | pin a response mode (admin) |
| `/usage` | today's cost, tokens, cache hits and budget state |
| `/status` | current settings and capabilities |
| `/reset` | clear this chat's history and notes (admin) |
| `/mute`, `/unmute` | silence the bot or bring it back (admin) |

## Configuration

Every setting is an environment variable; see [.env.example](.env.example) for the full
list with defaults. The ones worth knowing:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MODEL_FAST` | `google/gemini-2.5-flash` | everyday chatter |
| `MODEL_THINK` | `google/gemini-2.5-pro` | reasoning-heavy turns |
| `MODEL_ROUTER` | `google/gemini-2.5-flash-lite` | the dispatcher |
| `GROUP_REPLY_CHANCE` | `0.30` | unprompted participation |
| `DAILY_BUDGET_USD` | `0` (off) | spend cap with graceful degradation |
| `RESPONSE_CACHE` | `1` | reuse answers to identical recent messages |
| `WEB_SEARCH` | `1` | ground factual answers in live results |
| `BOT_LANG` | `en` | language of command replies (`en` or `fa`) |

Unknown model ids are detected at startup and replaced from `FALLBACK_MODELS`.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q          # 104 tests, fully offline
ruff check .
```

Tests use a mocked HTTP transport, so the exact OpenRouter request body is asserted
without any network access or credentials.

## Layout

```
main.py                 entry point
astolfo/config.py       environment-driven settings
astolfo/persona.py      layered prompt, few-shot examples, locale detection
astolfo/routing.py      fast / think / search / serious dispatcher
astolfo/llm.py          OpenRouter client with retries and fallbacks
astolfo/budget.py       cost accounting and degradation ladder
astolfo/cache.py        TTL + LRU caches
astolfo/media.py        images, stickers, GIFs, video, audio
astolfo/memory.py       history, long-term notes, persistence
astolfo/chat.py         the message pipeline
astolfo/commands.py     command handlers
astolfo/app.py          wiring, lifecycle, keepalive server
docs/                   architecture, deployment, cost control
```

## Notes

Astolfo and the Fate series belong to TYPE-MOON; this is a non-commercial fan persona and
it is worth saying so in the bot's Telegram bio. Keep your keys in Replit Secrets or a
local `.env` (git-ignored) and never in the repository.

MIT licensed.
