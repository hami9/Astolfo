# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version in [`astolfo/__init__.py`](astolfo/__init__.py) is the single source of
truth: it names the package, and a release tag that disagrees with it fails CI.

## [Unreleased]

Nothing yet.

## [2.6.0] - 2026-09-04

The bot could only see one service's models and remembered nothing about how any of
them behaved. Both are fixed here, and nothing about the replies changes: this
release only looks.

### Added

- **Every service is asked what it offers, not just OpenRouter.** Twelve of the
  thirteen carried a hardcoded list of model ids written months ago - `groq` still
  pointed at `llama-3.3-70b-versatile` while Groq had moved on to `llama-4-scout`,
  `qwen3-32b` and both sizes of `gpt-oss`. Every service with a key is now read, and
  the answers merge into one catalog. The consequence you can feel is elsewhere: the
  history budget is sized from the model's real context window, and until now that
  window was known for one service and guessed for the other twelve.
- **A listing that says almost nothing is read anyway.** Only OpenRouter answers with
  prices and modalities; Groq adds the window; the rest return an id and nothing else,
  and all of it used to be thrown away. What is missing is now inferred - the window
  from whichever field the service uses or from a table of model families, vision from
  the name (`vl`, `pixtral`, `llama-4`, `gemma-3`), and silence about price read
  against whether that service actually runs a free tier. A guessed window is shown
  with a `~` so the panel never states a guess as fact.
- **A service that renamed everything heals itself.** Reconciling the configured ids
  against the listing only ever removed ids, never added one. A service that had
  renamed its whole line-up was left with a stale list answering 404 to every message;
  it now adopts the service's own models instead. What is *called* stays deliberately
  short - that list is walked on failover, and a service offering four hundred models
  must not become a four-hundred-deep retry chain.
- **panel → models → 🆕 what is new.** Which models have appeared since this install
  started watching, with the service, the window and whether they are free. Free tiers
  gain and lose models weekly and the only way anyone noticed used to be a 404 in the
  log. What has been listed before is kept in the database, so "new" means new to this
  install rather than new since the last restart.
- **What each model and prompt actually did (schema v7).** A new `outcomes` table
  counts, per day and per service, model, prompt shape and mode: calls, replies a
  human answered, replies that had to be repaired, replies rejected as broken, tokens,
  cost and a running-mean latency. Every one of those signals already existed and went
  to a log line and nowhere else. Bounded at 500 rows a day and pruned with everything
  else.

### Changed

- Whether a reply arrived usable is now judged on every turn, not only in free mode.
  The retry is still free mode's; the evidence belongs to whichever model produced it.
- An answer is credited to the model and prompt that earned it. Whether anybody
  replied is only known on the following turn, and by then free mode may well have
  moved to another model.
- The free pool is filtered by service. Now that every service is read, an unfiltered
  pool would have offered OpenRouter one of Google's model ids.

### Fixed

- A model that outputs audio as well as text - a music generator, say - is no longer
  treated as something you can talk to.
- Speech, transcription and image models are recognised by name, which is all there is
  to go on in a listing that carries no modalities.
## [2.5.4] - 2026-09-05

### Fixed

- **A panel screen that stopped redrawing said nothing about it.** Making the
  redraw survive an expired callback query in 2.5.3 wrapped it in a bare
  `suppress`, which also swallowed the failures worth knowing about - a message
  too long, markup the API rejects, a message somebody deleted. Answering a dead
  query stays silent, because a stale spinner really is nothing; a failed redraw
  is logged with what went wrong.

### Changed

- A test awaited a task by name to join it, which is correct but reads as doing
  nothing - to CodeQL and to the next person. It waits on it with a timeout
  instead, which also asserts the close completes rather than only that it was
  awaited.

## [2.5.3] - 2026-09-05

Five bugs a diagnostic run against the live server turned up. Each one is
something that actually happened in the log, not something that might.

### Fixed

- **Every panel press could kill a reply that was being written.** Changing any
  setting calls `reconfigure`, which builds a new LLM client and closed the old one
  immediately - while requests still held it. Those turns died with `RuntimeError:
  Cannot send a request, as the client has been closed`, and the chat saw nothing.
  A retiring client now waits for its own requests before closing, capped so a
  wedged one cannot hold the connection pools forever, and the wait happens in the
  background so pressing a button still returns at once.
- **A slow panel action lost the change behind it.** Answering an expired callback
  query raises, and that call sat one line *above* the settings reload and outside
  the `try` that wrapped everything else - so a stale spinner took the reload and
  the redraw down with it. Both are best effort now.
- **One model refusing an image blinded its whole service.** The "does not take
  images" learning was keyed by service, so a single refusal from a free model
  marked the whole of OpenRouter text-only: its real vision models became
  unreachable, and the model that had actually refused was asked again nine more
  times. It is keyed by service *and* model now, and a service is skipped for media
  only when everything it would try has refused.
- **A model that answers with silence kept being asked.** One free model returned
  nothing twenty times in a single log: the ten-minute cooldown expired and it
  rejoined the pool to waste another turn. Repeat offences now earn ten minutes,
  then an hour, then the rest of the day. Escalating rather than a blocklist,
  because the free pool is discovered and tomorrow the useless one is a different
  id.
- **Twenty-one replies into a group that would not let it post.** Each one cost a
  model call to produce a message nobody received. The first "not enough rights"
  now switches that chat off exactly as the panel would, with an audit line, so it
  costs nothing until somebody fixes the permission and turns it back on. An
  ordinary send failure - a timeout, a blip - still leaves the chat alone.

## [2.5.2] - 2026-09-05

### Fixed

- **It could be walked into saying explicit things about itself, and about a real
  member of the group.** A member asked a crude yes/no question, it answered, and the
  next question came; twelve answers later it was describing itself and had been asked
  about the owner by name. Every individual reply was short and mild, which is exactly
  why nothing caught it - the thread was the problem, not any one line.

  The prompt had nothing to say about this at all. It covered teasing about the
  character's gender ("unbothered, breeze past it") and assumed the rest followed. It
  does not: a small model reads a yes/no question as a form to fill in, and answers it.
  Both prompts now carry a boundaries block, and the compact one - which is what the
  free models actually run, and where this happened - carries it too. It names the
  ladder rather than only the content: answering "no" is still answering, and the next
  message is "then what about Y", so the rule is not to take the first step. What it
  does instead is what the character would do - get bored, change the subject, tease
  them for trying - because a refusal notice is the one response the persona rules out.

  A prompt rule alone was not enough for the transcript bug in 2.5.1 and it is not
  enough here, so there is a backstop in code as well: a reply the bot wrote that
  contains explicit terms is never sent. It is replaced with one of its own bored
  lines, in the chat's language. The check reads the bot's output only - nothing anyone
  sends is filtered, blocked or judged, and the group's own crude jokes are untouched.
  It holds in paid mode too, unlike the free-mode retry it sits next to, because this
  is not a quality problem that another model would answer differently.

## [2.5.1] - 2026-09-04

All five from one evening of real group output.

### Fixed

- **It answered wearing other people's names.** The prompt shows the conversation as
  "Reza: ..." lines, and a small model does not answer into that - it continues the
  script. Every reply came back as "Arash(IQ 26): ..." and some carried two or three
  further invented turns, putting words in real members' mouths. The output rules now
  say plainly that the transcript is what other people already said and not a script,
  and two guards catch what does it anyway: the copied label is stripped, and
  everything from the first fabricated turn onward is cut. Prose is left alone -
  Persian puts a colon mid-sentence all the time, and eating half a real answer would
  be the worse failure.
- **It mixed languages.** "خوبم، خوبم، Disaster نشدم aún~" - a Persian answer with an
  English adjective and a Spanish word in it. One language per message is now a stated
  rule with that exact slip named as the example, and a reply that drifts into a script
  nobody was writing in (Cyrillic, CJK, Hangul, Thai, Hebrew) is treated as broken and
  asked again on another model. English terms Iranians really say in English are left
  alone.
- **A private chat showed as "?" on a person's screen.** That query selected only the
  chat title, which a private chat does not have, instead of the fallbacks every other
  screen uses.
- **The settings screen was a wall.** Thirteen full-width buttons you scrolled past to
  reach "back". They pair two to a row now, the model and provider entries are gone
  because the models and services screens pick from the live catalog instead of asking
  you to type an id, and the switches added in 2.5.0 - adaptive length, joining on
  merit, focus hold, homework, reading admins - are on it rather than reachable only by
  typing their names.
- **A service that cannot read images was asked on every photo.** cohere answered
  "image content is not supported for this model" and huggingface an input-validation
  error, once per picture, forever. The first refusal is now remembered for the life of
  the process and that service is skipped for media.

## [2.5.0] - 2026-09-04

Written from a pile of the bot's real group output. Every rule added here is a failure
somebody actually saw, not a hypothetical.

### Fixed

- **It rambled.** Comma chains, the same thought padded four times, a message that
  circles back and restates itself. The voice rules now name each of those, cap a reply
  at one line with two as the ceiling, and say to cut every clause that repeats another.
  `MAX_TOKENS_FAST` drops from 260 to 160, because a model fills the room it is given.
- **It repeated itself.** The same joke and nearly the same sentence, sent to two
  different people minutes apart. It is now told never to reuse a joke, a compliment or a
  message shape it has already used in the chat, and to say something else or nothing.
- **It made things up about people.** Whose exam is tomorrow, why somebody sent a video,
  what a sticker meant. Inventing anything about a person's life, plans or day is now a
  named ban, as is explaining what someone else meant or felt.
- **It gave advice nobody asked for.** Bedtimes, study plans, which Python library to
  start with. A friend does not manage anyone, and it is told so directly.
- **Emoji banners.** At most one emoji, and most messages have none.

### Added

- **It knows what it is not for.** Heavy maths, whole programs, homework, essays and long
  translations get a cheerful "that is way past what my head can hold" instead of a bad
  attempt; quick things stay normal conversation. Those requests also stop being escalated
  to the expensive think model, since the answer was always going to be a refusal.
  `HEAVY_LIFTING=1` turns it back into a solver.
- **One train of thought.** Joining a conversation on its own claims its attention for
  `ATTENTION_HOLD` seconds, and while one group has it the others get a fraction of its
  usual eagerness. Being spoken to is never gated by it. Asked where it went, it says it
  got caught up talking somewhere else and never says where, which is both more human and
  cheaper than the alternative.
- **It joins a topic on merit rather than a coin flip.** An open question, media,
  something it has opinions about or a running joke in this chat's notes raise the score;
  a reply between two other people, a sign-off, or having just spoken lower it. The chance
  you set moves the bar instead of being the whole decision. All local: no model call.
  `INTEREST_SCORING=0` restores the coin flip.
- **Replies get shorter when that is what works.** Two signals, both free: past 60% of the
  daily budget the ceiling comes down on a straight line, and after it speaks somebody
  either answers or does not — bucketed short, medium and long, and once a bucket clearly
  wins that is what it aims for. `ADAPTIVE_LENGTH=0` turns both off. Schema v6.
- **It knows who runs the group.** Owner, admin or member, and whether it is itself a
  plain member or an admin, cached for fifteen minutes. It never uses any of it: no
  settings, no permissions, no pinning, no removing anybody, no claiming it did, and no
  policing the chat. `READ_ADMINS=0` skips the lookup.
- **📈 which is doing best**, on the services screen: every service ranked on what it
  actually did today — calls answered, calls failed, cost and tokens per call. Reliability
  decides it; cost only separates services that are otherwise alike, and on free models it
  drops out. A service with fewer than eight calls is left alone rather than judged on
  noise. **⬆️ put the best one first** applies the ranking to the order things are tried
  in. Both cost no API calls, and pinning disables them.

### Fixed (found while building the above)

- The "it just spoke" penalty could never apply: the flag it reads was cleared by the
  reception accounting earlier in the same turn.

## [2.4.0] - 2026-09-04

### Fixed

- **Persian input made it sound stupid.** A phone keyboard produces several spellings of
  the same word — Arabic ي and ك where Persian wants ی and ک, diacritics and kashida that
  survive a copy-paste, Arabic-Indic digits, stretched words, invisible marks — and a
  small model reads each variant as something else. Input is now folded into one spelling
  on its way to the model, which also costs fewer tokens. Nothing is changed on the way
  out: the chat still sees exactly what was typed. Being called by name works through the
  same fold, so a stretched or Arabic-typed "آستولفو" reaches it.
- **Focus jumped off the person being replied to.** Telegram says which message a reply is
  aimed at and the prompt never carried it, so two people talking at once arrived as one
  flat transcript and the answer drifted to whoever was loudest. A turn now reaches the
  model as `Sara → Reza`, with a short quote of what Reza had said, and the history keeps
  the arrow so the thread survives into later turns.
- **A pasted wall of text wiped the conversation.** One long message could fill the whole
  history budget on its own and push every other turn out of the window, which reads
  exactly like the bot losing the thread. A single turn is now clipped in the prompt; what
  is stored is untouched.
- **Notes and the learned style could be lost on a restart.** The background fold changed
  them in memory without marking the store dirty, so they were only written if an ordinary
  message happened to follow.
- **A dropped column broke the schema.** A comment inside the `chats` table body made
  SQLite reject `ALTER TABLE ... DROP COLUMN`, because it re-parses the body. Table
  comments now live above the table.
- Two copies of the title guard had been pasted into `set_every_chat` and
  `update_credential`, neither of which has a title column.

### Added

- **It learns how to talk to each group and each person.** One line for the chat — which
  language and register, how long a message they tolerate, what falls flat — and one line
  each for up to a dozen regulars, about manner rather than facts. It is folded out of the
  summary call that already runs every twelve turns, so it costs no extra request, and
  only the lines about whoever is in the current turn are sent: a group of twenty pays for
  two. **panel → groups → a group** shows it, and **🧠 forget the learned style** clears it.
- **`/about` says where the code is, and what the bot can and cannot do.** The licence,
  the repository, and that anyone can run their own copy with their own bot token and
  their own key — plus a plain list of what it does and what it will not do (text only,
  no identifying people in pictures, no seeing a chat it is not in, no pretending to know).
  `/source` gives that half on its own.
- **The info deliberately does not say which model it is running or whose API pays for
  it.** That is the operator's business, it changes week to week, and a whole chat does
  not need it. `/status` still reports both, to admins. A test asserts no provider or
  model name appears in either text.
- **It stops guessing who is in a photo.** A guess about a real person is wrong often
  enough not to be worth having. Asked whose photo it is, it now dodges in character —
  reacts to something else, teases, asks who it is — rather than guessing or announcing a
  limitation.

### Changed

- **The token optimiser again.** Small models get a short form of the media rules (the
  dynamic block halves on a media turn), the list of people recently talking is dropped
  once there is a transcript that already names them on every line, and the normalisation
  above removes characters that were paid for and understood by nobody.
- A reply that comes back wearing the transcript notation it was shown
  (`Astolfo → Sara: ...`) is stripped like any other name prefix.
- The Persian "out of credit" line named the provider's account to the whole chat; it now
  says only that the API account needs topping up, as the English one always did.

## [2.3.3] - 2026-09-04

### Fixed

- **Saving a chat's state erased its title.** The in-memory state starts without one, so
  the first autosave or panel action wrote an empty title over the name Telegram gave us
  when the bot joined — which is how a named group came to show as a bare numeric id.
  An empty title now means "not known", never "clear it".
- **A private chat was recorded with no name at all.** It has no title, and only the
  membership handler fell back to the person's name; the message path did not. It does
  now, and because a private chat's id *is* the person's id, rows saved before this can
  still be named by joining the people table.

### Added

- **The groups list says what each chat is**: the kind, `@username`, how many people, how
  many messages, when it was last active, and 🔇 or ⏻ when it is muted or switched off —
  enough to recognise a chat before muting or leaving it. The detail screen and the people
  screen use the same name.

## [2.3.2] - 2026-09-03

### Fixed

- The channel handle shipped in 2.3.1 was a typo: it is **@hami294**, not `@hami249`.

### Added

- The [Discord](https://discord.gg/K33PnNafcD) is named in `/about` and in the issue
  template's contact links. It is stored as a whole invite URL rather than a handle,
  because an invite code is not composable from one.
- The mermaid diagram in the README said eleven services; there are thirteen.

## [2.3.1] - 2026-09-03

### Changed

- The channel is now `@hami249` — a typo, corrected to
  [@hami294](https://t.me/hami294) in 2.3.2 — and [hami9.ir](https://hami9.ir)
  is named alongside it. Both appear in `/about`, in the credit line under every
  `/start` and `/help`, and on the panel's home screen. The pinned branding test
  carries the new values, so a silent change to either is still a failure.

## [2.3.0] - 2026-09-03

### Added

- **The database cleans up after itself.** Nothing in it was ever deleted, so on a small
  host the audit trail and the per-day counters grew for as long as the bot ran. After
  `RETAIN_DAYS` (90 by default) the audit trail, the per-service day counters, groups the
  bot was removed from and people nobody has seen since are dropped, and the file is
  compacted so the space actually comes back. Runs at startup and once a day, or on
  demand from **panel → data → 🧽 clean up now**, which reports what went and how much
  was freed. `RETAIN_DAYS=0` keeps everything.
- **A block, a limit or the owner is never forgotten**, however old the row: those were
  decisions somebody made, and dropping them would quietly undo the choice.
- **The data screen shows the size on disk** — database, write-ahead log and all — and
  what the retention window is.
- **DeepSeek and OpenAI** are known out of the box: `deepseek-chat` and `deepseek-reasoner`
  (moving aliases, so they do not go stale), and `gpt-4o-mini` and `gpt-4o` with vision.
  Thirteen services now.

### Note

Long-term notes were never the thing filling the disk — they are capped at 900 characters
per chat. The tables around them were.

## [2.2.0] - 2026-09-03

Three things a busy group made obvious.

### Added

- **A group can be switched off entirely**, from **panel → groups → a group**. Muted stops
  the bot talking; this stops it listening — nothing said there is read, stored, counted
  or answered, and its commands go unanswered too, so the way back is the panel rather
  than a command in the group. It survives a restart, and the screen says exactly what it
  stops.

### Fixed

- **Being spoken to during someone else's turn got no answer at all.** A second message
  arriving while a reply was being composed was dropped outright. Addressed messages now
  wait their turn and are answered one after the other; unprompted chatter is still
  dropped, because it is background. Waiting is capped, so a burst of fifty mentions does
  not become fifty replies.
- **The bot lost the thread on small models.** `HISTORY_CHAR_BUDGET` was one number for
  every model, and the models now run from 8k of context to a million — 9000 characters
  of history plus a 10 KB persona overflows a small window, and what falls out of the
  front is the persona and the oldest turns. The budget is now derived from the context
  window the catalog reports for the model that will actually run, measured against the
  real size of the prompt being built, with a floor so there is always some conversation
  left.
- **Long-term notes never formed in most chats.** Folding only ran once the history
  window was completely full — 80 turns — so a chat that had said 79 things had no
  long-term memory at all, and by the time the eightieth arrived the oldest were already
  being evicted. Notes are folded every 12 turns instead, and each turn is folded once:
  how far the folding has got is tracked rather than re-summarising the same messages.

## [2.1.1] - 2026-09-03

### Fixed

- **Google answered 404 to every request from a new key.** The preset pinned
  `gemini-2.5-flash`, `gemini-2.5-flash-lite` and `gemini-2.0-flash`. Google has since
  retired those *for new users* — `2.0-flash` is gone from the listing altogether, and
  calling `2.5-flash` returns "no longer available to new users" even though the listing
  still names it, so startup validation cannot catch it. The preset now uses the moving
  aliases `gemini-flash-latest`, `gemini-flash-lite-latest` and `gemini-pro-latest`,
  which do not rot. Both flash aliases were confirmed to accept images.
- Google gains the billing note and signup page the other services already show in the
  panel.

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

[Unreleased]: https://github.com/hami9/Astolfo/compare/v2.6.0...HEAD
[2.6.0]: https://github.com/hami9/Astolfo/compare/v2.5.4...v2.6.0
[2.5.4]: https://github.com/hami9/Astolfo/compare/v2.5.3...v2.5.4
[2.5.3]: https://github.com/hami9/Astolfo/compare/v2.5.2...v2.5.3
[2.5.2]: https://github.com/hami9/Astolfo/compare/v2.5.1...v2.5.2
[2.5.1]: https://github.com/hami9/Astolfo/compare/v2.5.0...v2.5.1
[2.5.0]: https://github.com/hami9/Astolfo/compare/v2.4.0...v2.5.0
[2.4.0]: https://github.com/hami9/Astolfo/compare/v2.3.3...v2.4.0
[2.3.3]: https://github.com/hami9/Astolfo/compare/v2.3.2...v2.3.3
[2.3.2]: https://github.com/hami9/Astolfo/compare/v2.3.1...v2.3.2
[2.3.1]: https://github.com/hami9/Astolfo/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/hami9/Astolfo/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/hami9/Astolfo/compare/v2.1.1...v2.2.0
[2.1.1]: https://github.com/hami9/Astolfo/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/hami9/Astolfo/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/hami9/Astolfo/compare/10753be...v2.0.0
[1.0.0]: https://github.com/hami9/Astolfo/tree/10753be
