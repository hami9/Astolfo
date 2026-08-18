# Security

## Reporting a problem

Report anything you find privately, not as a public issue: open a
[security advisory](https://github.com/hami9/Astolfo/security/advisories/new),
or message [@ham1235i](https://t.me/ham1235i) on Telegram.

Please include what you did, what happened, and what you expected. A first reply
should come within a few days.

## What this project protects

The bot runs on one server, owned by one person, and answers to one Telegram
account. The things worth protecting are the API keys, the panel, and the
database of who talks where.

- **The panel** exists only in the owner's private chat and is gated on their
  numeric Telegram id. Every button press is re-checked, because a panel message
  can be forwarded and its buttons travel with it. `MASTER_ID` settles the id
  outright; without it, a username is used exactly once to learn the id and never
  consulted again.
- **API keys** are stored encrypted in the database with a key file beside it,
  are shown only masked, and the message that delivers one is deleted from the
  chat. They are never written to the log or the audit trail.
- **The server** is not reachable from a chat. The bot runs unprivileged and can
  ask a root helper for exactly two things — `restart` and `update` — by writing
  one word to a spool file. Nothing from Telegram is ever passed to a shell.
- **Message text is never stored.** The database keeps who is in which chat and
  how active they are, as counts. A test checks the database file and its
  write-ahead log to keep it that way.

## What it does not protect against

Said plainly, because a security file that only lists strengths is not useful.

- **Someone who already has the server.** The encryption key sits next to the
  database, so root, or the bot's own user, can read the keys. Encryption is
  there to make a *copied* database — a backup, a file pulled to a laptop —
  useless on its own.
- **Whoever controls the repository.** `update` runs the code on the branch. That
  is what an auto-update is, and it is why the helper accepts nothing else.
- **A compromised Telegram account.** Whoever is signed in as the owner has the
  panel. Use two-factor authentication on that account.

## If a key leaks

Revoke it at the provider first, then set the new one from the panel
(**keys → replace**). The old value is overwritten in the database; nothing else
has to be edited and the bot does not need a restart.
