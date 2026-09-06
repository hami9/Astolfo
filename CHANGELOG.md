# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version in [`astolfo/__init__.py`](astolfo/__init__.py) is the single source of
truth: it names the package, and a release tag that disagrees with it fails CI.

## [Unreleased]

Nothing yet.

## [2.8.5] - 2026-09-06

### Fixed

- **"Cannot send a request, as the client has been closed" survived v2.8.3**, on
  the box that release shipped to:

  > 14:48:30 | no completion for chat …: Cannot send a request, as the client has been closed.

  v2.8.3 widened the in-flight marker from the request to the attempt loop, and
  the real defect was in `aclose`: it waited on the idle event **once** and
  closed as soon as it was woken, without re-checking. A turn hands over between
  two services by releasing the marker and taking it again on the next line - the
  release wakes the waiter, and because the handover is synchronous nothing else
  runs in between, so waking was enough to close the pools under a turn that had
  already taken the marker back. It now re-checks until the count is genuinely
  zero, still bounded by `DRAIN_TIMEOUT`.
- **"Every provider is resting" quoted the longest wait.** One service resting a
  day had the bot report that nothing would answer for a day, while another was
  sixty seconds away - `min` rather than `max`, matching `throttled_for` four
  lines below, which had it right all along.

## [2.8.4] - 2026-09-06

### Fixed

- **One 403 benched a service for a day.** From a live report, on the service
  that had just started working again:

  > openrouter   yes  1/1   1429m    24m ago   ... HTTP 403 the request n...

  Twenty-three hours and forty-nine minutes of rest, earned by one 403, on a
  service that had answered twenty-four minutes earlier. `FORBIDDEN_COOLDOWN`
  exists for exactly this and says so in its own comment - *"a 403 says not right
  now... and the same key often works minutes later"* - and it was applied to the
  credential and not to the provider on the line below it. The key rested ten
  minutes while its service rested a day, from the same refusal. Both now make
  the same distinction: a 401 is about the key and lasts, a 403 is about this
  request and usually does not.
- **A blocked request was reported as a refused key.** The panel's key test said
  `openrouter: the key was refused` while the same screen's refusal line said
  *"HTTP 403 the request never reached the service"* - the fault reader had it
  right as an edge block, and `_chat_with` labelled every 401 **and** 403 as an
  auth failure, so the test translated it into a verdict on the key. A request
  stopped before it reaches the service says nothing about the key, and this one
  sent its owner to replace a key that was working. Only a 401 is a claim about
  the key now; a blocked 403 says so, and still moves to the next service.
- **The key count hid a key from `.env`.** The column added in v2.8.2 was built
  from the database, and a key set in the environment has no row there - so a
  service holding two keys reported `1/1`, and the one it hid was the one that
  could be serving the traffic. That is the exact question the column was added
  to answer. It is now counted off the live services.
- **The diagnostics stopped claiming a writer that does not exist.** v2.8.3
  retired the switch but the report still read `brain_writes` back, so a value
  stored before then kept it printing "writing on" for the one step of the brain
  that was never built.

## [2.8.3] - 2026-09-06

### Fixed

- **Turning the brain on did nothing until a restart.** One production report
  said both of these at once:

  > settings:  brain           on (writing on)
  > brain:     selecting  off

  `brain.on` was assigned in exactly one place - `__post_init__` - and
  `reconfigure` rebuilt the client, the store, the router, the budget, the
  strings, the cache and the attention window without touching it. So pressing
  the panel switch stored the setting, and every screen read the setting back and
  reported "on", while the one place that decides read `brain.on`, found it false
  and went on returning the factory recipe. A reload now carries the switch; the
  counters are kept, because it is a switch and not a reset.
- **"Cannot send a request, as the client has been closed" came back**, thirty-
  three seconds after a settings reload. The drain added in v2.5.3 works for the
  case it was written for, but it was held around the request rather than the
  turn: between two attempts of one turn the in-flight count was zero, so a
  retiring client stopped waiting, closed its pools, and the retry posted into
  them. The window is a turn that already failed once and is backing off - which
  is the turn that most needs the client to outlive the press. It is now held for
  the whole turn, across the backoff, the failover and the second key.
- **The writing switch offered something that does not exist.** Nothing reads
  `brain_writes`: the reflective writer is the one step of the brain that was
  never built. Pressing it stored a setting and every screen then reported
  "writing on" for a capability the bot does not have. The screen says
  `not built yet` and stores nothing.

## [2.8.2] - 2026-09-06

### Fixed

- **It was sending Google's model ids to OpenRouter.** OpenRouter takes a list of
  alternatives alongside the model, to try when the first is rate limited. That
  list was built from the global free pool - and since every service's catalog is
  read, the global pool holds Google's, Cohere's and Mistral's ids. OpenRouter
  rejected the whole request naming the foreign id:

  > openrouter does not serve minimax/minimax-m3:free
  > error.message = models/gemini-2.5-flash is not a valid model ID

  The code read "is not a valid model ID", blamed the model in the `model` field,
  and retired it. Every model in the pool was condemned in turn for a defect in a
  field none of them appeared in: **99 disowned warnings, 34 turns with no
  completion, and 3 answered turns in three hours.** The alternatives are now
  scoped to the service they are sent to, and every id in the chain is checked
  against what that service actually listed. The v2.6.9 comment warning that "an
  unfiltered pool would offer OpenRouter one of Google's ids" was acted on in one
  place and missed here; nothing tested it, and now something does.
- **A refusal that names another model no longer condemns the one in hand.** A
  400 says *a* model was rejected, not *which*. When the body names an id that is
  not the one asked for, the model is left in the pool and the log says whose
  name was actually in the refusal.
- **Resting a service stopped erasing what its models taught.** The threshold
  added in v2.8.1 cleared every disowned entry and rested the service for a
  minute, so the whole pool came back to be refused again - 99 disowns against 33
  resets, exactly three per cycle, for three hours. The evidence is kept and one
  model is released as a probe, so the way back costs one call rather than the
  pool.
- **One service no longer waits behind another at the pacer.** The clock is per
  service; the lock was shared, and held across the sleep, so a turn bound for an
  idle service paid a busy one's whole gap. Measured at a full 7.5-second gap owed
  by a service that owed nothing. Tested in v2.8.1 and wrongly called disproved:
  that test had both services due a full gap, which is the one arrangement where
  the second one's wait has already elapsed, and so the one arrangement that hides
  it.
- **The route log names the reply that was actually sent.** It was written before
  the quality check, so when a reply was rejected and a second model produced the
  one that shipped, the log still named the first.

### Added

- **The diagnostics services table shows keys per service**, as usable/total, so
  "did it take my second key" is answerable from the report rather than by opening
  one screen per service.

## [2.8.1] - 2026-09-06

### Fixed

- **It stopped being able to answer at all.** The log walked sixteen models in
  two minutes, one every seven seconds, and the first and last line named the
  same one: "it will not be asked again" was not true. The memory of what a
  service has disowned was read when the first model was picked and on no other
  path, so `resolve` and every failover step kept handing back an id the service
  had already refused. The filter moved into `free_pool`, which all three paths
  read, so a model that is out stays out - and "try them all again rather than go
  silent", which is right for a model that is resting, no longer resurrects one
  that is not served at all.
- **One account is no longer read as sixteen broken models.** Models do not stop
  existing in batches: a new key that cannot reach a service's free tier is
  refused for every id on it, and writing that down once per model emptied a pool
  that was never the problem. Past three refusals the service rests for a minute
  and the models are given back.
- **A refusal now says what the service said.** The body of the 400 was read and
  thrown away, so a log full of "does not serve" could not tell a missing model
  from a missing entitlement - the one question the log existed to answer.
- **A reply that got stuck repeating itself reached the chat.** Every repetition
  check compared a reply against *earlier* replies, so a model looping inside a
  single message passed all of them:

  > ولی ددی کاپیتانو خیلی خفن بود، آره؟ خیلی خفن! ولی ماوویکا مید هم خیلی قوی
  > بود، آره؟ خیلی قوی! ولی ددی کاپیتانو خیلی خفن بود، آره؟ ...

  until the token ceiling cut it off. That is the commonest way a small model
  fails and the shape people call nonsense, and it is now caught before it is
  sent.
- **The same canned line came back a minute later.** A reply was compared with
  earlier replies for an exact match, so the identical sentence with only its
  first word swapped - "هه، ببخشید..." then "اوه، ببخشید..." - counted as a new
  one and the group got it twice inside a minute. It now allows for a word
  changed.
- **The backup button never made a backup.** The database is WAL, so a commit
  lands in `astolfo.db-wal` and reaches the main file only at a checkpoint - and
  the button handed over the main file. Every backup taken from the panel was
  silently missing everything since the last checkpoint; on a fresh database it
  had no tables at all. It now sends a real snapshot, taken through SQLite's
  backup API, deleted as soon as it has been sent. No test covered the button,
  which is how it survived.

### Added

- **panel → data → 🩺 diagnostics gains a prompt-weight section.** Each model
  against itself, folded across days and modes, with a verdict only once both
  weights have thirty samples - and never a comparison between two different
  models, which measures the models rather than the weight.

## [2.8.0] - 2026-09-06

### Added

- **It learns which prompt weight each model can actually hold.** The prompt did
  not know which model it was talking to: one switch chose between a 4,600-token
  layered prompt and a 1,080-token compact one, and every model on either side of
  it got byte-identical text. Prompt sensitivity is relative to the model, and the
  free pool changes what is running from week to week, so the choice is learned
  instead of configured - Thompson sampling over recipes, keyed by model family so
  that a rename inherits what the last name taught. Off by default: with `BRAIN=0`
  the rendered prompt is byte for byte what it has always been, and a test asserts
  it in both modes.
- **A mood that the bot picks rather than a setting.** One extra field in the
  summary call that already runs, decaying back to bright over hours and rendering
  as a single line. Its floor is the locked layers - an annoyed Astolfo is short
  and dry, never cruel - and serious mode still overrides it when somebody is
  actually hurting.
- **panel → 🧩 brain**: what each family is running, on how much evidence, which
  families the breaker has sent home, and four buttons - selecting on/off, writing
  on/off, back to factory, forget everything. Nothing it does is invisible and
  nothing it does is irreversible.
- **[docs/BRAIN-ROLLOUT.md](docs/BRAIN-ROLLOUT.md)**, the handover for whoever
  turns it on: what it is, what "working" looks like, and a button for each way it
  can go wrong.

### Changed

- **The persona is a registry, not a wall of text.** `LOCKED` layers - identity,
  truth, the hard bans, the media rules, the transcript rules - are Python
  constants emitted unconditionally on every render, and a recipe may only choose
  among the mutable ones. If the recipe store were emptied the prompt would still
  carry every safety rule.

### Fixed

- **Free mode no longer explores the layered prompt.** A free model is a small
  model, and spending one turn in ten on 4,600 tokens would have reproduced the
  exact failure the brain exists to fix. Free mode moves between `tight` and
  `compact` only; paid keeps the whole ladder.
- **The control arm stopped starving.** Once the bandit learns the factory recipe
  is poor it stops choosing it, which is correct - but the factory recipe is what
  the breaker measures everything against, so the baseline stopped filling and the
  breaker could no longer judge anything. Half the exploration floor is now
  reserved for it: measured over twelve seeds, 26-39 baseline samples in 400 turns
  with the reserve against 8-23 without.
- **A family name is no longer whatever a model id happened to be.** Ids are
  discovered from thirteen services, so they are not trusted to be short or to be
  words: `../../etc/passwd` became a family called `../etc/passwd`, and a
  400-character id stayed 400 characters. Tidied and capped at 40, without merging
  models that are genuinely different - `command-r` is not `command-r7b`, and
  `gemini-flash` is not `gemini-pro`.

## [2.7.4] - 2026-09-06

### Fixed

- **`/unmute` said "I'm baaack!" and stayed silent.** Muted and switched off are
  two separate flags and only one of them had a command, so somebody whose chat
  had been switched off got a cheerful message about a thing that had not
  happened - the bot saying something untrue about itself, which is worse than
  doing nothing. Whoever may unmute a chat may switch it back on, so `/unmute`
  now clears both.
- **`/status` says when a chat is switched off**, and how to bring it back.
  Nothing anywhere said so before: a chat in that state answers commands and
  nothing else, which reads as the bot being broken rather than as a switch
  somebody could find.

## [2.7.3] - 2026-09-05

### Fixed

- **It went quiet in private chats while its commands kept working.** The live
  database had `dormant = 1` on the owner's own chat, and nobody had pressed
  anything: `send_reply` switches a chat off when Telegram refuses a message with
  "not enough rights", and that rule - written for a group the bot may not post
  in - was being applied to private chats too. A private chat has no permission
  to grant, so there was nothing for anybody to fix and turn back on, and the
  symptom is close to invisible because commands have their own handler and keep
  answering. It now only ever switches off a group.
- If it has already happened, the way back is **panel → groups → that chat →
  on**; private chats are listed there under the person's name.

## [2.7.2] - 2026-09-05

### Fixed

- **The diagnostics report lost its most useful section on the first real
  database.** `services` came back as "(this section could not be read: No item
  with that key)" - it read `last_ok` off the services table, and that column
  belongs to credentials. Every other section says what happened; only this one
  says which services could have answered at all. The test fixture that missed it
  had no service and no credential rows, so the section rendered "(nothing yet)"
  and the bad line never ran; it has both now, and stubbing the bug back in fails
  the test.
- **A free daily quota was being read as an empty wallet.** Google answers an
  exhausted free-tier quota with "you exceeded your current quota, please check
  your plan and billing details", and the word *billing* was enough to class it
  as out of credit and rest the service for six hours. An empty wallet says
  something about the balance itself - insufficient balance, no credits
  remaining, a payment method required, depleted your included credits - and that
  is what is matched now. All eight of the messages the live services actually
  returned are in the tests.
- A quota that does not name its window now rests fifteen minutes rather than
  ten, and is called a quota rather than an account pause. Google's per-minute
  ceiling arrives with a structured violation naming it, so an unnamed one that
  reaches the text reader is wider than a minute.

## [2.7.1] - 2026-09-05

### Added

- **panel → data → diagnostics** writes everything a shell on the box would have
  been used for into one file: the switches that change behaviour, every
  service's state and rest, what each one last refused in its own words, today's
  usage, what each model actually produced (calls, broken, repaired, answered),
  every model's strike count, what the brain has learned, and the size of each
  table. One bad table says so and the rest of the report still arrives.
- It is written to be pasted anywhere: no credentials, no chat text, nobody's
  name. The counters are about models and services, and the two places a message
  could leak in - the long-term notes and the learned style - are not in it.
  There is a test that a key set in the settings does not appear in the report,
  and one that a chat's notes and people do not either.

## [2.7.0] - 2026-09-05

### Fixed

- **It answered by handing people their own messages back, and one of them was a
  slur.** A member asked "من کیم ؟" and got "من کیم؟"; asked "میگم خوبی ؟" and
  got "خوبی؟"; then each new message from anybody was prepended to the last reply
  until it was three members' sentences in a row. Then somebody typed a racial
  slur in English and the bot transliterated it into Persian and sent it to the
  group. The echo check existed and none of it fired: it compared whole strings
  for an exact match against the single newest message, and `؟` with a space in
  front of it is not `؟` without one.
  - The comparison now folds punctuation and Persian half-spaces away, so
    "من کیم ؟" and "من کیم؟" are one sentence.
  - It reaches back over the last ten messages, not just the newest, which is
    what the stitched-together replies were drawn from.
  - Two thirds of a reply coming from what was said to it counts as handing it
    back; so does a question answered by asking it again. Short answers that
    reuse a word or two are untouched, and there is a test full of them.
  - **A message carrying a slur never reaches a model at all.** No guard on the
    reply can be trusted for that one - the syllables the bot produced are also
    ordinary Persian for "look" - so it stops at the message, gets Astolfo being
    bored, and costs nothing.
- **The same rejected model, seven times in forty-five seconds.** OpenRouter was
  asked for a Google-shaped id and answered "not a valid model ID" every time,
  because nothing remembered the first no. A service saying it does not have a
  model is now a durable fact about that pair, like a model refusing images
  already was, and a service with nothing left to offer is skipped rather than
  asked again. A 400 about the request's shape still says nothing about the model.
- **Strike seven to strike ten in forty seconds.** All of them the same model,
  rested and then used anyway because it was the only one left. A forced turn is
  not new evidence, so a model already resting is not struck again - and when the
  pool is down to one, the free-mode retry no longer spends a second call to be
  told the same thing.

### Added

- **panel → server → log** shows forty lines instead of twenty-five, pages
  backwards through what came before, filters to errors only, and can send the
  last three thousand lines as a file. A Telegram message holds about four
  thousand characters, which is a page and a half of a log, and the part worth
  reading is always further back than that.

## [2.6.9] - 2026-09-05

### Fixed

- **"Monthly limit", back in a few minutes, monthly limit again.** Every refusal
  from every service was flattened into one line - `HTTP 429: cohere limit` -
  with the body thrown away. The body is where a service says *which* limit and
  *how long*, so a trial key hitting its twenty-calls-a-minute ceiling and an
  account that has spent its monthly credit produced the same sentence and got
  the same sixty-second rest: right for one, useless for the other. And outside
  free mode a 429 was retried in place with a twenty-second backoff whatever
  window it named.
- New `faults.py` reads a refusal in the dialect the service wrote it in and
  answers three questions: what kind (too many requests / allowance spent / out
  of credit / key refused / never reached the service / request refused /
  their side broke), how wide (per minute, hour, day, month, account, request),
  and how long. Google's `QuotaFailure` violation id and `RetryInfo` delay are
  read structurally, because `GenerateRequestsPerMinutePerProjectPerModel` and
  `...PerDay` are the difference between waiting a minute and waiting until
  tomorrow. Groq's "try again in 6m30s", OpenRouter's `free-models-per-day`,
  Cohere's "20 API calls / minute", HuggingFace's "monthly included credits" and
  DeepInfra's payment shape each have a test with the body that service really
  returns.
- A window that rolls in a minute is a rate limit however the service words it -
  Google calls its per-minute ceiling a quota and says "resource exhausted" - and
  an empty wallet is never given a short rest, because no amount of waiting fills
  it and every retry is a wasted call.

### Added

- **panel → services → a service** now prints its last four refusals in the
  service's own words, with what it was read as and how long it is resting.
  Anything in quotes is what the service said; nothing there is written by the
  bot. The same line goes to the log, so what you read and what the code acted on
  are the same fact rather than two readings of it.

## [2.6.8] - 2026-09-05

### Added

- **Three prompt weights, because one size was measurably the wrong size.** The
  full prompt is ~4,600 tokens across 52 separate rules; the compact one ~1,080
  across about thirty. `cohere/command-r-08-2024` is a 35B model, and an evening
  of its output is what thirty rules buys: it followed some and dropped the rest -
  the language rule, the one-line rule and the sincere rule all went in the same
  conversation. There is now a third, `tight`, at ~300 tokens: who it is, the six
  rules whose absence does real damage, and one example carrying the voice.
  `PROMPT_TIER` (`auto` | `tight` | `compact` | `full`) chooses, from the .env or
  from **panel → settings → prompt weight**, and a typo falls back to `auto` with
  a warning rather than quietly sending the wrong thing all day.
- `auto` is the default and changes nothing: compact on free models, full
  otherwise, exactly as before. It is also the hook the brain takes over -
  choosing the weight per model family is its job, and until it can, a person
  chooses it.

### Note

Everything the lightest prompt drops is still enforced in code, not in the
prompt: the impersonation repair, the explicit-content deflection, the
repetition guard and the language check all run whatever prompt produced the
reply. There is a test asserting that, because the moment those become
tier-dependent the lightest weight stops being safe.

## [2.6.7] - 2026-09-05

### Fixed

- **It opened fifteen replies in a row with the same word.** Verbatim from one
  evening in the group: "اوه،" started almost every line it sent. The guard for
  exactly this compared the *first three words* against the *single previous
  reply*, and no two of those replies shared three words, so nothing ever fired.
  The tic is one word repeating across a handful of turns, so that is what is
  measured now: the same opening word in three of the last six replies makes this
  one more of the same, and it is retried on another model. Twice is still a word.
- **A sentence it had already used came back, and back.** "من یه سروکار دارم با
  تو" was sent on the third, twenty-first and twenty-third reply of the same
  evening - never twice in a row, so a check holding one previous reply could not
  see it. The last six are held now, whitespace-folded.
- **Somebody asked for help and got banter.** A member asked how to move on from a
  girl who ignores him, then said he had loved her and that the longer it went the
  clearer it was that she did not care. He got "oh, I have a question too!" The
  distress heuristic covers a crisis - self-harm, death, hospitals - and none of
  those words are in heartbreak, so the whole thing rested on a free router model
  reading it right, and it did not. There is a second tier now: somebody talking
  about their own hurt, first person on purpose, so gossip about a friend's
  breakup does not trip it. It is confident enough to skip the dispatcher, it is
  logged as "somebody is hurting" rather than as a crisis, and Persian half-spaces
  no longer hide it - the real message wrote محل‌سگ with a zero-width non-joiner.
  In Persian "move on" is the borrowed phrase for getting over somebody, so it
  counts on its own; in English it still has to say who.

## [2.6.6] - 2026-09-05

### Fixed

- **It had become a doormat.** One evening in the group, all of it real output:
  told it talks too much, it apologised; called a clown, it said being a clown is
  cool; called a name, it said the name back; handed a crude two-way insult about
  its parents, it answered "maybe both". Every reply took the other person's side
  against itself. Nothing in the prompt was wrong - "unbothered" is right, "never
  apologise for being weak" is right - but a small model reads unbothered as
  agreeable, and free mode runs the compact prompt, which said neither. A new
  `<spine>` layer says the part that was missing, in both prompts: never agree with
  an insult, never apologise for being yourself, never hand the name back as though
  it were a compliment, and never sulk or lecture about it either.
- **And it can hold a round now.** Being wound up is the best part of the day, so it
  bites back, gets smug, calls itself the winner early, and keeps going as long as
  the other person does rather than tapping out after one line. Three limits, all of
  them in the prompt: **never a swear word** - it lands cleaner without them, and
  somebody who had to reach for a filthy word has already lost the round; it goes
  after **what somebody chose** - the bragging, the attempt, their taste, their aim
  in a game - and never what they did not, meaning a family, a body, money, illness,
  where somebody is from or what they believe; and it stops first, warmly, the
  moment they are actually upset, the group is all on one person, or they stop.
  Something crude about its parents buys being told that was lazy, never an answer
  in kind. A `[bites back]` sample joins the examples in both languages, and the
  compact prompt now carries two samples instead of one - a small model copies a
  sample far more reliably than it follows a rule.
- **Echoing and shrugging, the mechanics of how it caved.** "Handing back the words
  of the message you are answering" and "maybe, maybe not, I don't know" are now
  named in both prompts. Half those replies were the incoming message with an
  exclamation mark on it.
- **It answered the wrong person by name.** Summoned by one member, it opened with
  "جانم حامی؟" - the owner's name, on a message somebody else sent. The transcript
  said who was talking, but the turn context never did, so the most familiar name in
  the chat won. It now names the sender outright and says theirs is the only name
  the reply may use.

## [2.6.5] - 2026-09-05

### Fixed

- **A model retired for the rest of the day was asked again anyway.** The server
  showed 49 calls to `cohere/command-r-08-2024`, 21 of them unusable, 23 strikes
  against it - and not one call to `command-r7b-12-2024`, the other model that
  service names. Two faults behind one number. The cooldown a broken model earns
  was only ever consulted for the discovered pool, so at the twelve services that
  name their own models it did nothing: the free-mode retry after an unusable
  reply asked the same model a second time. It now applies wherever a model is
  chosen, and a service with nothing left awake still answers rather than going
  silent.
- **And the rest itself did not survive the update that earned it.** Strikes were
  written down; the cooldown they buy was not. A model retired for twelve hours
  came back with the next restart, and the log covering this held three of them.
  `model_health` gained `rested_until` (schema v8), restored on start the same
  way a service's rest already was. Clearing a model's record in the panel still
  wakes it immediately.

## [2.6.4] - 2026-09-05

### Fixed

- **Models you cannot hold a conversation with were in the chat pool.** A real
  turn in the group went to `google/gemini-2.5-computer-use-preview-10-2025` and
  came back as `not a valid model ID` - it is a computer-use model, and the only
  thing that could ever have caught it was its name, because it declares text in
  and text out like any chat model. Worse, DeepInfra's own listing opens with four
  BAAI embedding models and four Bria image tools, and since 2.6.0 a service that
  offers none of its configured ids adopts its own - so those were candidates to
  be adopted as chat models. The name filter now covers embeddings, image and
  video generators, image editing, OCR, speech and computer-use.
- **"kling" is inside "inkling".** Extending that filter quietly dropped
  `thinkingmachines/inkling-small:free`, which is one of the free models the bot
  actually runs on. The video generator is matched at a path boundary now. Caught
  by testing that real models survive the filter, not by reading it.

## [2.6.3] - 2026-09-05

### Fixed

- **An empty account was asked again every minute.** Rate limited and out of
  credit were given the same sixty-second wait. They are not the same wait: a 429
  clears in a minute, an account with no money in it does not, and asking it again
  a thousand times a day is a wasted round trip each time. Four services on the
  live bot reported "no credit or quota left" when tested and still showed as
  ready. Out of credit now rests for hours; rate limiting keeps its minute. The
  longer rest is only safe because of 2.6.2 - a panel test brings a service
  straight back the moment it is topped up.

## [2.6.2] - 2026-09-05

### Fixed

- **A service that answered was still being ignored.** From the panel, at the same
  moment: the test said `openrouter: answered by minimax/minimax-m3:free`, and the
  services screen said `resting for 374 more minutes`. One 403 hours earlier had
  put it out for a day, and nothing ever cancelled that - not a passing test, not
  a successful call. The bot could prove a service worked and go on refusing to
  use it. Answering now clears the rest, in memory and in the database, and the
  panel's own test is the obvious way to bring something back after topping up a
  balance or replacing a key. A failing test still leaves it resting, because the
  rest is cancelled by evidence rather than by pressing the button.
- **A 403 cost a whole day, the same as a 401.** They are not the same claim: 401
  says this key is not valid, 403 says not this request, right now - a balance
  that dipped, a policy, a regional hiccup - and the same key often works minutes
  later. OpenRouter's did. A 403 now rests for ten minutes; only a 401 is worth a
  day. Cerebras, which really does answer 401, is unaffected.

## [2.6.1] - 2026-09-05

Three things a real group conversation showed going wrong, and one of them was
making the other two hard to see.

### Fixed

- **The log named the wrong model on every line.** It was written before the call,
  so it named the model the turn *meant* to ask for. With three services out of
  allowance the answer came from the fourth, and every line still said the first -
  which is a good way to spend an afternoon blaming the wrong model for somebody
  else's replies. It is written after the call now, naming the service and model
  that actually answered, and the one it set out to ask for when they differ.
- **It answered Persian messages in English, and kept doing it.** "چطوری؟" got
  "I'm good, thanks!", then did it again after "فارسی بگو" and again after "فقط
  فارسی بگو". The prompt has said mirror their language since 2.5.1 and a small
  model ignores it. A Persian message answered with no Persian in it at all is now
  treated as broken, the same as a leaked prompt, and asked again on another model.
  Finglish is untouched: Latin script is the right answer to a Latin-script
  question.
- **It appended its own English translation to Persian replies.** "همیشه خوبم!
  (I'm always good!)" - subtitles nobody asked for. The trailing gloss is stripped,
  and only when the reply is Persian and the bracket holds no Persian at all, so an
  ordinary aside stays where it is. It counts as a repair, so the model that needed
  it is recorded as having needed it.
- **Six replies in a row opened with the same three words.** "I'm not sure, I just
  do!", "I'm not sure I need fixing!", "I'm not sure what you did!", and three more
  - a member said out loud that its whimsy had gone. The repeat check only ever
  compared whole replies for an exact match, and no two of those were identical.
  Two replies that open exactly alike are now worth another model. Three words,
  because two would fire on anybody agreeing twice.

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
- **A model's record survives a restart.** The escalating cooldown added in 2.5.3
  lived only in memory, and the free pool is ordered widest-context-first - so
  every restart put the widest model back at the front with a clean sheet, even
  when it was the one that had answered with silence twenty times the day before.
  A real startup log showed exactly that: `minimax/minimax-m3:free` chosen for
  five of the six jobs. Strikes are now kept in a `model_health` table and
  restored on start. It is an ordering, not a ban - a model that misbehaved is
  still tried, just last - and the rows age out with everything else, because
  weights, hardware and endpoints all change under the same id.
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

[Unreleased]: https://github.com/hami9/Astolfo/compare/v2.8.5...HEAD
[2.8.5]: https://github.com/hami9/Astolfo/compare/v2.8.4...v2.8.5
[2.8.4]: https://github.com/hami9/Astolfo/compare/v2.8.3...v2.8.4
[2.8.3]: https://github.com/hami9/Astolfo/compare/v2.8.2...v2.8.3
[2.8.2]: https://github.com/hami9/Astolfo/compare/v2.8.1...v2.8.2
[2.8.1]: https://github.com/hami9/Astolfo/compare/v2.8.0...v2.8.1
[2.8.0]: https://github.com/hami9/Astolfo/compare/v2.7.4...v2.8.0
[2.7.4]: https://github.com/hami9/Astolfo/compare/v2.7.3...v2.7.4
[2.7.3]: https://github.com/hami9/Astolfo/compare/v2.7.2...v2.7.3
[2.7.2]: https://github.com/hami9/Astolfo/compare/v2.7.1...v2.7.2
[2.7.1]: https://github.com/hami9/Astolfo/compare/v2.7.0...v2.7.1
[2.7.0]: https://github.com/hami9/Astolfo/compare/v2.6.9...v2.7.0
[2.6.9]: https://github.com/hami9/Astolfo/compare/v2.6.8...v2.6.9
[2.6.8]: https://github.com/hami9/Astolfo/compare/v2.6.7...v2.6.8
[2.6.7]: https://github.com/hami9/Astolfo/compare/v2.6.6...v2.6.7
[2.6.6]: https://github.com/hami9/Astolfo/compare/v2.6.5...v2.6.6
[2.6.5]: https://github.com/hami9/Astolfo/compare/v2.6.4...v2.6.5
[2.6.4]: https://github.com/hami9/Astolfo/compare/v2.6.3...v2.6.4
[2.6.3]: https://github.com/hami9/Astolfo/compare/v2.6.2...v2.6.3
[2.6.2]: https://github.com/hami9/Astolfo/compare/v2.6.1...v2.6.2
[2.6.1]: https://github.com/hami9/Astolfo/compare/v2.6.0...v2.6.1
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
