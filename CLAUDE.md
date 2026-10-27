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
  `eras.py` (SPEC §7.2) and `miners.py` (§7.3) are the interface the agent tools call;
  keep their signatures stable. Lexicons live in `analysis/lexicon/`. `tz.py` resolves
  each person's zone; `tz_infer.py` infers periods from sleep windows (SPEC §6.1) into
  `tz_suggestion`, which the user confirms with `sincehello tz confirm`.
- `sincehello/agent/`: the discovery agent (SPEC §7.4–7.5): `tools.py` (tools + strict
  schemas), `loop.py` (the manual loop), `budget.py`, `runlog.py`, `prompts/`. Keep
  `prompts/system.md` free of run-specific text so the cached prefix stays byte-stable.
  `verify/checks.py` has the deterministic insight checks and `verify/critic.py` the LLM
  critic (SPEC §7.6; its prompt `verify/critic.md` is the cached prefix, keep it stable
  too); `report/schema.py` the `report.json` models. `agent/tz_namer.py` names inferred
  timezone periods with one Claude call each (prompt `prompts/tz_namer.md`, also stable).
- `sincehello/curate/`: curation (SPEC §7.7). `rank.curate(report)` fills `deck`; it reads
  only the `Report`, stays deterministic, and runs at the end of every agent run.
- `tests/`: `fake_anthropic.py` has `ScriptedClient`, the offline stand-in for the API
  client; tests must never call the real API. `conftest.py` has `FakeConnector`, an
  example connector, `ScriptedConnector`
  for custom fixtures, and fixtures.
- `evals/`: synthetic histories with planted facts (SPEC §10.1). Format and commands in
  `evals/README.md`; `connectors/synthetic.py` loads them. Regenerate the committed sample
  after changing the generator, or `test_committed_sample_is_reproducible` fails.

## Rules

- **Never commit real message data**: no `chat.db` copies, exports, or fixtures built
  from real conversations. Test fixtures must be synthetic or hand-written.
- All timestamps are timezone-aware UTC in records, and integer UTC milliseconds in the store.
- Ingest must be idempotent. Re-running a connector never duplicates rows.
- Analysis covers 1:1 conversations only (`conversation.is_group = 0`). Still store group chats.
- English only in v1.
- PRs must pass CI (`.github/workflows/ci.yml`: ruff format check, ruff lint, pytest) before merging.
