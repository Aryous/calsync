# Contributing

Use an issue to describe a bug or propose a change. For bugs, include the
revision, platform, reproduction steps using synthetic calendars, and the
expected and observed behavior. Report security issues as described in
[SECURITY.md](SECURITY.md).

Create a branch and keep each pull request focused. Read [AGENTS.md](AGENTS.md)
for the architecture and safety invariants. Changes to behavior need a
regression test and corresponding documentation updates.

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
uv run pytest -W error -q
```

For web UI changes, install the test browsers and run the browser suite:

```sh
uv run playwright install chromium webkit
uv run pytest -m browser -q
```

Tests use fake accounts and temporary state. Preserve the test isolation;
never use real credentials or calendar contents in tests, fixtures,
screenshots, logs, or commits.

Contributions are provided under the project's [MIT license](LICENSE).
