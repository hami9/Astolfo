# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project has not cut a tagged release yet; `main` is the released version. Entries are
grouped by the change that introduced them, newest first.

## [Unreleased]

### Added

- Public project documentation: contributing guide, code of conduct, issue and pull
  request templates, and this changelog.

## API management — services, keys, limits and offline answers

*(pull requests #27, #28, #29)*

### Added

- **A services screen in the owner's panel.** Every service with its state — working,
  resting until, no key, switched off — its keys, its models, its endpoint, today's calls
  and how many failed. None of it costs an API call; only **test** spends one.
- **Several keys per service.** Keys are stored encrypted, shown masked, added by a
  message that is deleted straight away, and labelled. A refused key rests for a day and
  the next takes over mid-conversation, so a key can be replaced with no gap.
- **Custom services from Telegram.** Anything OpenAI-compatible can be added by name, URL
  and model list, with no code change and no restart.
- **Six more services known out of the box**: cerebras, mistral, cohere, huggingface,
  sambanova, deepinfra and aimlapi, each with its billing note and signup page in the
  panel.
- **Per-chat and per-person daily call limits**, set globally, on one group, on one
  person, or on every group at once. Both screens show how much of the cap is used.
- **Manual, auto and smart reply modes**, set globally or per group. Smart is auto while
  that makes sense and manual when it does not: every service resting, most of the budget
  spent, or the chat moving faster than a dozen messages a minute.
- **Pinning a service by hand.** *Use only this* stops the failover chain on one service;
  a button on the list undoes it.
- **Answers that need no model.** Greetings, goodbyes, thanks, "who are you", the time,
  the date and plain arithmetic are answered in character with every service down.
  Anything needing knowledge gets an honest "my brain is offline" rather than a guess.

### Changed

- The `Authorization` header moved off the shared HTTP client and onto each request,
  which is what makes more than one key per service possible.
- Service and key health (`rested_until`) is stored as wall-clock time and reloaded at
  startup, so a quota that runs until tomorrow is still known tomorrow.
- Per-service accounting: requests, failures and cost are recorded per service and shown
  in the panel and in `/usage`.

### Fixed

- A 404 from a service now walks its model list instead of raising, and the response body
  is logged, so a renamed model id names itself in the log rather than producing a bare
  traceback.
- Any other 4xx logs what the service actually said before moving on.

## Owner panel, SQLite store and a hardened deployment

*(pull request #21)*

### Added

- `/panel` in the owner's private chat: settings, groups, people, server health, database
  backup and the audit trail, gated on one numeric Telegram id and re-checked on every
  button press.
- SQLite (WAL) replacing the JSON state for chats, people, settings, keys and the audit
  trail, with a versioned schema and additive migrations.
- Encrypted key storage with a 0600 key file, masked display everywhere, and settings that
  change at runtime with no restart.
- Update and restart from the panel through a systemd path unit and a root helper that
  understands exactly two words, with automatic rollback if the new version fails to
  start.
- Security workflows: CodeQL, `pip-audit`, gitleaks and the bandit rules in ruff, plus
  `SECURITY.md`.

## Several services behind one client

*(pull requests #17, #18, #19, #20)*

### Added

- Provider presets stacking OpenRouter, Google, Groq and GitHub Models behind one client,
  failing over as each runs dry.
- Model id verification per service at startup, replacing unknown ids from
  `FALLBACK_MODELS`.

### Changed

- Each service is sent the request shape it accepts: OpenRouter-only fields are gated so a
  plain OpenAI-compatible endpoint is not handed something it rejects.
- A free-tier rate limit is treated as account-wide rather than per model.

## Free mode and credit control

*(pull requests #9 through #16)*

### Added

- `FREE_MODE`: discover and rotate OpenRouter's zero-cost models as their allowances run
  out, with quality guards that move on from a model answering with nothing.
- Telegram Stars donations (`/donate`).
- A degradation ladder as spend approaches the daily cap — cheap model only, then replies
  only when addressed, then a polite stop with one notice per hour.

## First release

*(pull requests #1 through #8)*

### Added

- The Astolfo persona as ordered prompt layers, with few-shot examples in four moods and a
  reminder re-injected periodically.
- Adaptive routing: `fast`, `think`, `search` and `serious`, decided by heuristics first
  and an LLM dispatcher only when they are unsure.
- Multimodal input: photos, stickers, GIFs, videos, video notes and voice, with ffmpeg and
  a Telegram-thumbnail fallback.
- Group manners: reply chance with a cooldown, guaranteed answers when addressed, per-chat
  settings, long-term notes and admin-gated commands.
- One-command VPS setup with swap for small servers, a virtualenv launcher, Docker and
  Replit support, and the test suite on Python 3.10, 3.12 and 3.13.
