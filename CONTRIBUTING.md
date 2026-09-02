# Contributing

Thanks for looking. Issues, questions and pull requests are all welcome, including
small ones — a typo in a doc is a real contribution.

## Getting set up

```bash
git clone https://github.com/hami9/Astolfo && cd Astolfo
make install       # virtualenv + dev dependencies
make check         # ruff and the full suite
```

`make help` lists every target. Without make it is the same three commands:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

`pre-commit install` runs ruff, the secret scan and the suite before each commit, so a
red CI run is something you rarely see.

The suite is fully offline. It needs no API key, no bot token and no network, so a clean
clone should go green on the first run. If it does not, that is a bug worth an issue.

To actually run the bot you need `TELEGRAM_BOT_TOKEN` and one provider key in a `.env`
(copy [.env.example](.env.example)). Install `ffmpeg` if you are touching media handling.

## What CI checks

Every push and pull request runs:

| Check | Command | Notes |
|---|---|---|
| Tests | `pytest -q` | on Python 3.10, 3.12 and 3.13 |
| Lint | `ruff check .` | includes the bandit security rules |
| CodeQL | — | the `python` query pack |
| Audit | `pip-audit` | known vulnerabilities in dependencies |
| Secrets | `gitleaks` | no key may enter the history, ever |

Run `pytest -q && ruff check .` before pushing and you will almost never see a red build.

## House style

The code is written to be read. A few things it is consistent about:

- **English only**, in code, comments, commit messages and docs. User-facing strings are
  the exception: they live in `astolfo/strings.py` and are translated.
- **Comments explain why, not what.** Most functions have none, because the name and the
  code say it. A comment earns its place by recording a decision, a constraint, or
  something that surprised somebody.
- **Line length 100**, enforced by ruff. Configuration lives in
  [pyproject.toml](pyproject.toml) — there is no separate lint config to keep in sync.
- **No new runtime dependencies** without a reason in the pull request. The bot targets a
  1 GB VPS, and the standard library covers more than it looks like it does.
- **Type hints on anything public.** `from __future__ import annotations` at the top of
  every module.

## Tests

- **No test may reach the network.** Provider calls are asserted through
  `httpx.MockTransport`; see `tests/test_providers.py` for the pattern and
  `tests/conftest.py` for the fixtures.
- A change in behaviour needs a test that fails before it and passes after.
- Test names read as sentences — `test_a_refused_key_steps_aside_for_the_next_one` — so a
  failure tells you what broke without opening the file.
- `tests/test_security.py` checks that message text never reaches the database or its
  write-ahead log. If your change stores anything from a message, expect it to fail, and
  do not "fix" it by loosening the assertion.

## Pull requests

1. Branch from `main`.
2. Keep the change to one subject. Two unrelated fixes are two pull requests.
3. Write the description as the [template](.github/pull_request_template.md) asks: what
   changed, why, and how you checked it.
4. Green CI is required before merge.

Commit messages use a short type prefix (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`) and describe the effect rather than the diff.

## Cutting a release

Only a maintainer does this, but it is written down so it is the same every time.

1. Bump `__version__` in [`astolfo/__init__.py`](astolfo/__init__.py). Nothing else holds
   a version number — `pyproject.toml` reads it from there.
2. Move the `Unreleased` entries in [CHANGELOG.md](CHANGELOG.md) under a new
   `## [X.Y.Z] - YYYY-MM-DD` heading, and add the compare link at the bottom.
3. `make release-check` — lint, the suite, the build, the wheel's package list, and that
   the changelog has notes for this version.
4. Merge, then tag the merge commit and push it:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```

The `Release` workflow takes it from there: it refuses a tag that disagrees with
`__version__`, runs everything again, builds the sdist and wheel, publishes a GitHub
release with that version's changelog section and the artifacts attached, and pushes
`ghcr.io/hami9/astolfo:X.Y.Z` and `:latest`.

What counts as which number, for a bot rather than a library:

| Bump | For |
|---|---|
| major | a migration that cannot be rolled back, or a setting that changes meaning |
| minor | a new command, panel screen, provider or capability |
| patch | fixes and documentation |

## Security

Do not open a public issue for a vulnerability. [SECURITY.md](SECURITY.md) says where to
send it instead.

Never commit a key, a token, or a `.env`. If one reaches a branch, revoke it at the
provider first — rewriting the history afterwards is the smaller half of the job.

## Things deliberately out of scope

So nobody spends a weekend on a pull request that cannot be merged:

- **Image or audio generation.** The bot is text-out by design.
- **Multiple accounts at one provider** to get past its free quota. Several keys per
  service is for keys you already hold. Anything aimed at creating or cycling accounts to
  evade a limit will be closed.
- **Storing message text.** History lives in memory and is folded into notes; the disk
  keeps counts and settings only.
- **A web dashboard.** Everything is run from Telegram on purpose. That constraint is a
  feature, not a gap waiting to be filled.
