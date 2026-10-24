# SinceHello

Wrapped-style insights from your message history with one person. `SPEC.md` is the source
of truth for product and architecture decisions; read the relevant section before changing
a module, and update the spec when a decision changes.

## Commands

- `uv sync` installs dependencies.
- `uv run pytest` runs the tests.
- `uv run ruff format . && uv run ruff check .` formats and lints. Run both before committing.
- `uv run sincehello --help` shows the CLI.

## Layout

- `sincehello/connectors/`: one module per app. Implement the `Connector` protocol in
  `base.py`, emit `HandleRecord` / `ConversationRecord` / `MessageRecord`, write with
  `write_batch`, and register a factory in `registry.CONNECTORS`.
- `sincehello/store/`: `schema.sql` (normalized schema, SPEC §5) and `Store`. Connectors
  never write SQL directly. Changing the schema means bumping `SCHEMA_VERSION` and adding
  a migration.
- `sincehello/analysis/`: the deterministic stats engine (SPEC §7.1). Register stats with
  `@stat(...)` in `stats.py`; keyword-only arguments are the stat's parameters and feed
  its `query_id`. Results must be JSON-serializable and must not depend on the wall clock.
- `tests/`: `conftest.py` has `FakeConnector`, an example connector, `ScriptedConnector`
  for custom fixtures, and fixtures.

## Rules

- **Never commit real message data**: no `chat.db` copies, exports, or fixtures built
  from real conversations. Test fixtures must be synthetic or hand-written.
- All timestamps are timezone-aware UTC in records, and integer UTC milliseconds in the store.
- Ingest must be idempotent. Re-running a connector never duplicates rows.
- Analysis covers 1:1 conversations only (`conversation.is_group = 0`). Still store group chats.
- English only in v1.
- PRs must pass CI (`.github/workflows/ci.yml`: ruff format check, ruff lint, pytest) before merging.
