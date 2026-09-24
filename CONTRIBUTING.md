# Contributing to khipumaq

Thanks for considering a change. khipumaq does one thing — keep the whole
session record and make it searchable by the instances that come after — and
changes that keep that focus are the easiest to accept.

## Setup

```sh
git clone https://github.com/fsgeek/llm-memory
cd llm-memory
uv sync --group dev
uv run pytest -q
```

Most tests run against a real ArangoDB: they read `db-config.ini` the same way
the package does (see the README) and clean up what they write.

## How changes land

- **Test-first.** A behavior change starts with a test that pins it; a bug fix
  starts with a test that reproduces the bug.
- **Code and tests from separate hands.** Implementation and its validating
  tests are authored independently and land in separate commits.
- **Surgical.** Touch only what the change requires.
- **Nothing leaves the user's database.** No telemetry, no hosted defaults, no
  content in logs. A change that sends episode content anywhere else will not
  be accepted.

## Sign-off (DCO)

Every commit must carry a `Signed-off-by:` line certifying the
[Developer Certificate of Origin](https://developercertificate.org/): that you
wrote the change or otherwise have the right to submit it under this project's
MIT license. `git commit -s` adds it.
