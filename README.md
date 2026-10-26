# SinceHello

Spotify Wrapped, but for you and one person you text. Connect your messaging apps, pick a
person, and get warm, evidence-backed insights: your first "I love you", your pet names,
your inside jokes, who texts first, and the eras of your relationship.

Status: early scaffold. See [`SPEC.md`](SPEC.md) for the design.

```sh
uv sync
uv run sincehello connect      # list connectors
uv run sincehello people       # people by message count
uv run sincehello merge 12 someone@example.com   # fold a handle into person 12
uv run sincehello me me@example.com              # mark your own handle
uv run pytest
uv run sincehello connect synthetic   # load the synthetic sample (evals/README.md)
```

All data stays local under `~/.sincehello` (override with `SINCEHELLO_HOME`).

## CI

Every pull request and every push to `main` runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
on Python 3.11 and 3.13: `uv sync --locked`, `ruff format --check`, `ruff check`, and
`pytest`. It is fast, free, and deterministic, and never calls the Claude API. LLM evals
(SPEC §10) will run in a separate, manually triggered workflow.

Run the same checks locally before pushing:

```sh
uv sync --locked
uv run ruff format --check . && uv run ruff check . && uv run pytest
```
