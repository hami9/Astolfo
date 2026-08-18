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
| **settings** | Any setting by name, plus switches for the common ones |
| **groups** | Every group the bot is in: activity, mute, leave |
| **people** | Who has spoken to it, where, and blocking |
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
