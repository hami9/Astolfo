# Running the bot from Telegram

Everything below happens in one place: a private chat with the bot, from the
account that owns it. The panel never appears in a group.

## Becoming the owner

The bot trusts exactly one numeric Telegram id.

- `MASTER_ID=123456789` in `.env` settles it and is the recommended setting.
- With no `MASTER_ID`, the first person whose username matches `MASTER_USERNAME`
  (default `ham1235i`) is claimed once and their id is written to the database.
  After that the username is never consulted again, because usernames can be
  given up and taken by somebody else.

`/panel` from anyone else does nothing at all — not even an error, which would
confirm the command exists. Repeated attempts are logged and recorded.

## What the panel does

| Screen | What it is for |
|---|---|
| **services** | Every service: keys, health, today's calls, order, on/off, and adding your own |
| **models** | Which model does which job, chosen from the live catalog, and the tokens each spent |
| **settings** | Any setting by name, plus switches for the common ones |
| **groups** | Every group: who is in it, activity, mute, switch off, leave, how talkative it is, daily limit |
| **people** | Who has spoken to it, where, blocking, and per-person limits |
| **server** | Health, log, update, restart |
| **data** | Row counts, the audit trail, a backup of the database |

### The services screen

The list shows each service with a mark — working, resting, no key, switched off —
the number of calls it took today and how many failed. None of that costs an API
call: it is what the bot recorded while it was working. Only **test** spends one.

Open a service to see its keys, its endpoint and its models. From there you can:

- **add a key**, optionally labelled: send `work laptop: the-key`
- **test** it, which reports whether the key is refused, out of quota, rate
  limited, or fine
- switch the service **off** without deleting anything, or move it up and down
  the order things are tried
- **wake it now**, if it is resting and you know the quota has reset
- correct its **endpoint** or **models** when a service renames something
- **add a service** the code has never heard of — anything OpenAI-compatible:
  send `name url model,model`

A service can hold more than one key. The first usable one is used; if a key is
refused it rests for a day, records what it was told, and the next one takes over
without the chat noticing — so a key can be replaced with no gap. That is what
several keys are for: keys you already hold. Several accounts at one service to
get around its free quota is a different thing, it breaks their terms, and it
usually ends with all of them closed.

A key you send is stored encrypted and **your message is deleted** right away.
Keys are only ever shown masked (`sk-or-…f2f4`). Destructive actions — removing a
key, leaving a group, blocking, updating, restarting — take a second press.

Changing a key or a setting takes effect immediately. There is no restart, and
the conversations in progress are not disturbed.

### The models screen

Free models appear and disappear weekly, and the old way to follow that was to
edit `.env` and restart. This screen reads the catalog from every service that
has a key and lets you press one.

Six jobs, each set on its own:

| Job | What runs on it |
|---|---|
| **fast** | everyday chatter — most messages land here |
| **think** | code, maths, comparisons, explanations |
| **search** | grounded answers over live web results |
| **media** | photos, GIFs, video and voice |
| **router** | the dispatcher deciding which of the above a message is |
| **summary** | folding old turns into long-term notes |

Open a job and the list shows what the service offers, longest context first,
with its window, whether it can see or hear, and what it charges. **Free only**
is the default; **show every model** includes the paid ones, and the price is on
the screen before you choose rather than on the bill afterwards. The **media** job
only lists models that can actually read a picture. Long lists page, and **🔎
search** filters by name.

Pressing one writes the setting and it applies to the next message — no restart,
and conversations in progress are not disturbed. **🔄 sync catalog** reads the
listing again, for when a service has added or retired something since startup.

In free mode the bot rotates the whole free pool automatically and these settings
are what it falls back to; turn free mode off to run exactly what you picked.

Only OpenRouter answers a listing in full. Groq adds the context window; the rest
return an id and nothing else, so the window is inferred from whichever field the
service uses or from the model family's name, and vision is read from the name.
An inferred window is shown with a `~` in front of it, because it is a guess and
the screen should not pretend otherwise.

### What is new

**🆕 what is new** lists the models that have appeared since this install started
watching — newest first, with the service, the window, whether it is free, and how
long ago it showed up. Anything first seen in the last week is badged.

What has been listed before is kept in the database rather than in memory, so
"new" means new to this install and not merely new since the last restart. A model
no service has listed for a long time is forgotten, and counts as new again if it
comes back — which by then is what it is. **🔄 scan again** re-reads every service
and stays on this screen so you can see what came back.

### What the models cost you

**📊 token usage** breaks today down per model: calls, tokens in, tokens out and
cost. On free models every cost is zero, so the number that tells them apart is
the work — which model is actually carrying the group, and which one you chose
and never used. `/usage` shows the busiest three in the same terms.

### How it decides to join in

An unprompted reply used to be a coin flip: the same chance for "guys I got tickets!!"
and for two people three replies deep into a conversation with each other. It now scores
the message first — an open question, media, something it has opinions about, a running
joke from this chat's notes — and subtracts for the things that mean *stay out*: a reply
between two other people, a sign-off, or having just spoken. The chance you set is still
what decides how talkative it is; it moves the bar rather than being the whole decision,
and `INTEREST_SCORING=0` puts the coin flip back.

It also has one train of thought. Joining a conversation on its own claims its attention
for `ATTENTION_HOLD` seconds, and while one group has it the others get a fraction of its
usual eagerness — one bot behaving like twenty was both the least human thing it did and a
straight multiplier on the bill. Being spoken to is never gated by this. If somebody asks
where it went, it says it got caught up talking somewhere else and never says where.

### How talkative it is

Three modes, set globally or on one group, from **groups → a group**:

- **manual** — answers only when it is replied to, mentioned, or called by name.
- **auto** — also jumps into conversations on its own, at the chance you set.
- **smart** (the default) — auto while that makes sense, manual when it does not:
  every service is resting, most of the day's budget is gone, or the chat is
  moving faster than a dozen messages a minute.

The group's own setting beats the global one; **follow the global mode** hands it
back. The row of buttons on the groups list applies a mode to *every* group at
once, which is what you want after adding the bot to several at a time.

Being spoken to always gets an answer. Manual is "only when asked", never silence
— that is what **mute** is for.

### Knowing which chat you are looking at

Each row names the chat the best way it can: its title, else the person's name for a
private chat, else its `@username`, else the id. Under the name are the kind, how many
people, how many messages and when it was last active, and 🔇 or ⏻ if it is muted or
switched off — enough to recognise a chat before muting or leaving it.

A private chat has no title, which is why it used to show as a bare id. Its id is the
person's id, so even a row saved before the name was recorded can still be named.

### Switching a group off

**Mute** stops the bot talking. **⏻ switch off entirely**, on a group's screen, stops it
listening: nothing said there is read, stored, counted or answered, and the group's
commands go unanswered too — which is why it is switched back on from the panel rather
than from inside the group. It survives a restart.

Use mute for "be quiet in here" and off for "pretend you are not in this group", when
leaving would lose the group's settings and notes.

### Limits

A daily cap on model calls can be set on one group, on one person, or on every
group at once. The specific one wins over the global `CHAT_DAILY_CALL_LIMIT` and
`USER_DAILY_CALL_LIMIT`; 0 means "follow the global one". Both screens show how
much of the cap has been used today.

### Which service is actually doing best

**📈 which is doing best** ranks every service on what it has really done today: how many
calls it answered, how many it failed, what each answered call cost and how many tokens it
took. Reliability decides it — a service that answers is worth more than one that saves a
tenth of a cent and returns 402 — and cost only separates services that are otherwise
alike, which on free models means it drops out entirely. A service with fewer than eight
calls is left alone rather than judged on noise.

**⬆️ put the best one first** applies that ranking to the order things are tried in. It
only moves the services it can judge, so a key added this morning is neither promoted nor
buried. Producing the ranking costs no API calls: it is the counters the bot wrote while
it was working. Pinning a service disables both, since nothing else is being tried anyway.

### Choosing a service by hand

Services are normally tried top to bottom, failing over as each runs out. **📌 use
only this** on a service pins everything to it, and nothing else is tried — if it
is out of allowance the turn fails rather than quietly spending somewhere else.
The list then shows a **🔀 automatic order** button to undo it.

### Answering several people at once

Only one reply is composed per group at a time, so two people never get two half-written
answers. Being spoken to while that is happening waits its turn rather than being dropped,
and the waiting is capped at two — a burst of mentions produces a few answers, not fifty.
Unprompted chatter that arrives mid-reply is dropped, because it is background rather than
a question.

### How long its replies are

`MAX_TOKENS_FAST` is the ceiling, and two things bring it down on their own:

- **The budget.** Past 60% of the daily budget, replies get shorter on a straight line to
  half length at the cap. A shorter answer is the cheapest saving there is.
- **Whether anybody answers.** After it speaks, somebody either replies to it or does not.
  Replies are bucketed short, medium and long, and once a bucket has clearly won — eight
  samples and a real margin, not one lucky message — that is what it aims for. A group
  that scrolls past long messages gets short ones; a group that talks back to them keeps
  them. `ADAPTIVE_LENGTH=0` turns both off.

Nothing here costs a model call. It is arithmetic over counters, and no message text is
part of it.

### What it will not do

Out of the box it is a group-chat regular, not a solver: heavy maths, whole programs,
homework, essays and long translations get a cheerful "that is way past what my head can
hold" instead of a bad attempt. Quick things — a small sum, a word translated, what
something means — are just conversation and it answers them normally. Those requests are
also kept away from the expensive think model, since the answer was going to be a refusal.
`HEAVY_LIFTING=1` turns it into a solver if that is what you want.

### Who runs the group

With `READ_ADMINS=1` it looks up who runs each group, at most once every fifteen minutes,
and the prompt then knows whether it is talking to the owner, an admin, or a member — and
whether it is itself a plain member or an admin. It never uses any of it: no settings, no
permissions, no pinning, no removing or muting anybody, no invite links, and no claiming
it did. It can be useful to whoever runs the place without behaving like staff, and it does
not police the chat.

### How much it remembers

History is trimmed to what the chosen model can actually hold, not to a fixed number of
characters: the catalog reports each model's context window, and the budget is measured
against the real size of the prompt being built. Moving a job to a small model therefore
shortens its memory rather than overflowing it — which is what "the bot lost the thread"
usually turns out to be.

A single pasted wall of text is clipped in the prompt rather than allowed to fill the
whole budget, so one long message cannot push the rest of the conversation out of the
window.

Older turns are folded into long-term notes every twelve turns, each turn folded once.
**panel → groups → a group** shows the notes a chat has accumulated.

### How it learns to talk to you

The same background call that writes the notes also writes down *how* this chat likes to
be talked to: one line for the group — which language and register, how long a message
they tolerate, what falls flat — and one line each for up to a dozen regulars. It is about
manner, never about facts, and it costs no extra model call.

Only what applies is sent: the group's line plus a line for whoever just spoke and whoever
they were answering. A group of twenty people therefore pays for two lines, not twenty.

**panel → groups → a group** shows it under *learned style*, and **🧠 forget the learned
style** clears it so the bot starts over — useful after the group changes character, or if
it has picked up a habit you do not like.

### Keeping the thread when two people are talking

Telegram knows which message a reply is aimed at, and the bot now passes that on: the turn
reaches the model as `Sara → Reza`, with a short quote of what Reza had said. Two
conversations running at once in one group used to arrive as a single flat transcript, so
the reply drifted to whoever was loudest rather than to the person who was replied to.

### Persian input

Persian arrives in several spellings of the same word — an Arabic keyboard gives ي and ك
where Persian wants ی and ک, diacritics and kashida survive a copy-paste, and Arabic-Indic
digits are different characters from the ones a model mostly saw in training. Small models
read each variant as something else, which is when the bot starts sounding stupid. Input is
folded into one spelling on its way to the model; the chat still sees exactly what was
typed.

### What the group is told about the bot

`/about` names the repository and the licence, says anyone can run their own copy, and
lists plainly what the bot can and cannot do. `/source` is that half on its own.

Neither says which model it is running on or whose API is paying for it. That changes
week to week, it is nobody's business but yours, and telling a whole chat is an invitation
to argue about it — the persona already dodges the question when someone asks directly.
`/status` still reports the model and the service, but only to an admin of the chat; in a
private chat with you it reports everything.

### Photos of people

It can see a picture but it cannot tell who is in one, and it does not try — a guess about
a real person is wrong often enough that the answer is not worth having. Asked whose photo
it is, it dodges in character rather than announcing a limitation: reacts to something else
in the frame, teases, or asks who it is.

### When nothing can answer

If every service is resting or out of quota, the bot still handles what needs no
model at all: greetings, goodbyes, thanks, "who are you", the time, the date, and
plain arithmetic — in character, in the chat's language. Anything that needs
actual knowledge gets an honest "my brain is offline right now" rather than a
guess. It never invents an answer to cover an outage.

## Updating the server from the panel

The bot runs as an unprivileged user and stays that way. It cannot restart or
update itself, so **server → update** writes one word into
`data/control/request`, and a small root helper (`deploy/astolfo-agent.sh`,
started by a systemd path unit) is the only thing that acts on it. The helper
understands two words, `restart` and `update`, and nothing from the file is ever
passed to a command.

An update fetches, resets to the remote branch, reinstalls requirements and
restarts. If the new version fails to start within twelve seconds, the server
rolls itself back to the previous commit without being asked. When the bot comes
back it messages whoever pressed the button with the commit it is running.

Worth knowing: an update runs whatever is on the branch, so whoever controls the
repository controls the server. That is true of any auto-update, and it is the
reason the helper does not accept anything else.

### Keeping the database small

Nothing in it was ever deleted before, so on a small host the audit trail and the
per-day counters grew for as long as the bot ran. **data** now shows the size on
disk and cleans up once a day, plus once at startup:

| Dropped after `RETAIN_DAYS` (90) | Never dropped |
|---|---|
| the audit trail | a person who is blocked |
| per-day service counters | a person with a limit of their own |
| groups it was removed from, and their members | the owner |
| people nobody has seen since | anyone still in a group |

A block or a limit was a decision somebody made; forgetting it would quietly undo
the choice, so those rows stay however old they are. **🧽 clean up now** does it on
demand and reports what went and how much space came back — SQLite does not shrink
the file on a delete, so the compaction runs straight after.

Long-term notes are capped at 900 characters per chat, so they were never the thing
filling the disk. `RETAIN_DAYS=0` keeps everything.

## Where the data lives

`data/astolfo.db` (SQLite, mode 600) holds chats, people, settings, encrypted
keys and the audit trail. `data/secret.key` (mode 600) decrypts the keys.

Encryption protects a **copy** of the database — a backup, a file pulled to a
laptop. It is not protection against someone who already has the server, because
the key file sits next to it. Keep the backup you download from the panel
somewhere private, and keep `secret.key` out of it if you would rather the copy
be useless on its own.

The database records who is in which chat and how active they are. It never
stores message text; a test checks the database file and its write-ahead log for
exactly that.

## If the panel is unreachable

Everything the panel does can still be done on the server:

```bash
sudo systemctl restart astolfo          # restart
sudo journalctl -u astolfo -f           # follow the log
sudo -u astolfo git -C /opt/astolfo pull && sudo systemctl restart astolfo
sudo nano /opt/astolfo/.env             # keys and settings, the old way
```

A value stored from the panel wins over the same value in `.env`. To go back to
the file, use **settings → reset one to the .env value**, or delete the row:

```bash
sudo -u astolfo sqlite3 /opt/astolfo/data/astolfo.db \
  "DELETE FROM settings WHERE key='free_mode';"
```
