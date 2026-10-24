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
```

All data stays local under `~/.sincehello` (override with `SINCEHELLO_HOME`).
