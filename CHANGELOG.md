# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version in [`astolfo/__init__.py`](astolfo/__init__.py) is the single source of
truth: it names the package, and a release tag that disagrees with it fails CI.

## [Unreleased]

Nothing yet.

## [2.1.0] - 2026-09-03

Pick the model from Telegram. Free models on OpenRouter appear and disappear weekly, and
following that used to mean editing `.env` and restarting.

### Added

- **A models screen in the panel.** The catalog is read from the service and shown as a
  list — context window, whether it can see or hear, and what it charges — and a press
  assigns one to a job. It applies to the next message: no `.env`, no restart, and
  conversations in progress are not disturbed.
- **Six jobs, set on their own**: fast, think, search, media, router and summary. The
  media job only offers models that can actually read a picture.
- **Paid models are hidden until asked for**, and the price is on the screen before the
  choice rather than on the bill after it.
- **Search and paging** for a catalog that runs to dozens of free models, and **sync**
  to read the listing again when a service adds or retires something.
- **Tokens counted per model** — calls, tokens in, tokens out and cost, today, in the
  panel and in `/usage`. On free models nothing costs anything, so the work done is the
  only thing that separates one from another.

### Changed

- The catalog is parsed into records (`astolfo/catalog.py`) rather than a set of ids, so
  the panel can show what a model is rather than only its name. `llm.py` reads the same
  records for free-mode rotation.
- `by_model` in the usage history records calls and tokens as well as cost. A file
  written by an older version, which held a bare cost, is read and upgraded in place.

## [2.0.0] - 2026-09-02

Run the whole bot from Telegram. The release that added the owner's panel, moved every
piece of state into SQLite, and turned one API key into a managed stack of eleven
services.

### Added

- **The owner's panel.** `/panel` in the owner's private chat: settings, services,
  groups, people, server health, a database backup and the audit trail — gated on one
  numeric Telegram id and re-checked on every button press, because a panel message can
  be forwarded and its buttons travel with it.
- **A services screen.** Every service with its state — working, resting until, no key,
  switched off — its keys, models, endpoint, today's calls and how many failed. None of
  it costs an API call; only **test** spends one.
- **Several keys per service.** Stored encrypted, shown masked, added by a message that
  is deleted straight away, and labelled. A refused key rests for a day and the next
  takes over mid-conversation, so a key can be replaced with no gap.
- **Custom services from Telegram.** Anything OpenAI-compatible can be added by name, URL
  and model list, with no code change and no restart.
- **Eleven services known out of the box** — openrouter, google, groq, github, cerebras,
  mistral, cohere, huggingface, sambanova, deepinfra and aimlapi — each with its billing
  note and signup page in the panel.
- **Per-chat and per-person daily call limits**, set globally, on one group, on one
  person, or on every group at once. Both screens show how much of the cap is used.
- **Manual, auto and smart reply modes**, global or per group. Smart is auto while that
  makes sense and manual when it does not: every service resting, most of the budget
  spent, or the chat moving faster than a dozen messages a minute.
- **Pinning a service by hand.** *Use only this* stops the failover chain on one service;
  a button on the list undoes it.
- **Answers that need no model.** Greetings, goodbyes, thanks, "who are you", the time,
  the date and plain arithmetic are answered in character with every service down.
  Anything needing knowledge gets an honest "my brain is offline" rather than a guess.
- **Update and restart from the panel**, through a systemd path unit and a root helper
  that understands exactly two words, with automatic rollback if the new version fails to
  start within twelve seconds.
- **SQLite (WAL) as the datastore** for chats, people, settings, services, keys and the
  audit trail, with a versioned schema and additive migrations. Message text is never
  written to it, and a test checks the database file and its write-ahead log to keep it
  that way.
- **Encrypted key storage** with a 0600 key file and masked display everywhere.
- **Security workflows**: CodeQL, `pip-audit`, gitleaks and the bandit rules in ruff, plus
  `SECURITY.md`.

### Changed

- The `Authorization` header moved off the shared HTTP client and onto each request,
  which is what makes more than one key per service possible.
- Service and key health (`rested_until`) is stored as wall-clock time and reloaded at
  startup, so a quota that runs until tomorrow is still known tomorrow.
- Per-service accounting: requests, failures and cost are recorded per service and shown
  in the panel and in `/usage`.
- Settings changed from the panel are adopted in place — a new client is built and the old
  one closed, while the chat store is deliberately kept so no group forgets mid-reply.

### Fixed

- **`astolfo.admin` was missing from the built wheel**, so an installed package failed at
  import. The package list is now discovered rather than written by hand.
- A 404 from a service walks its model list instead of raising, and the response body is
  logged, so a renamed model id names itself in the log rather than producing a bare
  traceback. Any other 4xx logs what the service actually said before moving on.
- A key refused by one service no longer costs the turn: the next key, then the next
  service, is offered the same request.

### Documentation

- A README written as a public front page, an architecture document covering the pipeline
  and the provider stack, deployment and cost-control guides, `CONTRIBUTING.md`, a code of
  conduct, issue and pull request templates, and this changelog.
- `.env.example` names every setting the code reads, with its real default — checked by a
  test, so it cannot drift again.

## [1.0.0] - 2026-08-18

The bot itself: persona, routing, media, and enough cost control to leave it running.
This one predates tagging, so it is a name for the history rather than a release you can
download; the link below goes to the last commit it covers.

### Added

- The Astolfo persona as ordered prompt layers — narrative identity, voice markers, canon
  anchors, group behaviour, language mirroring, banned assistant-isms and a
  highest-priority truthfulness layer — with few-shot examples in four moods and a slim
  reminder re-injected periodically so the voice does not flatten in long chats.
- Adaptive routing: `fast`, `think`, `search` and `serious`, decided by regex heuristics
  first and a small LLM dispatcher only when they are unsure, with cached verdicts.
- Multimodal input: photos, stickers, GIFs, videos, video notes and voice, with ffmpeg and
  a Telegram-thumbnail fallback. Text out only, always.
- Group manners: reply chance with a cooldown, guaranteed answers when addressed, per-chat
  settings, long-term notes about running jokes, and admin-gated commands.
- `FREE_MODE`: discover and rotate OpenRouter's zero-cost models as their allowances run
  out, with a compact persona, reply validation and behaviour ranking to keep a small
  model in character.
- Several services stacked behind one client, failing over as each runs dry, and model id
  verification per service at startup.
- Telegram Stars donations (`/donate`), which need no payment provider and no card.
- A degradation ladder as spend approaches the daily cap — cheap model only, then replies
  only when addressed, then a polite stop with one notice per hour.
- One-command VPS setup with swap for small servers, a virtualenv launcher, Docker and
  Replit support, and the test suite on Python 3.10, 3.12 and 3.13.

[Unreleased]: https://github.com/hami9/Astolfo/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/hami9/Astolfo/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/hami9/Astolfo/compare/10753be...v2.0.0
[1.0.0]: https://github.com/hami9/Astolfo/tree/10753be
