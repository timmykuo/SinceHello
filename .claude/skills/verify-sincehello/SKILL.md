---
name: verify-sincehello
description: "Drive the SinceHello CLI (`sincehello connect/people/stats/eras/candidates/analyze/eval/synth`) against the synthetic bean-goose sample in an isolated data dir, and capture evidence. Use to prove a change to a connector, the store, analysis, the agent, or evals works from the user's side, not just in pytest."
disable-model-invocation: true
---

# Verify SinceHello

SinceHello is a CLI. Verifying a change means running the real `sincehello` commands a user
would run, against the committed synthetic sample, in a data dir no one else uses, and
keeping the transcripts as proof. `scripts/control.py` does the isolation and the evidence.

```bash
C=.claude/skills/verify-sincehello/scripts/control.py   # executable; runs under `uv run`
$C --help
```

Run everything from the checkout root. Each checkout gets its own run id (the checkout's
directory name, or `--run <id>`), so parallel worktrees never share a store.

## Launch

```bash
uv sync
$C launch
```

`launch` deletes and recreates `.verify/<run>/home/`, sets `SINCEHELLO_HOME` to it, runs
`sincehello connect synthetic` (the `evals/samples/bean-goose` sample), then runs `doctor`.
Ready means the last line is `doctor: ok`. There is no server, so nothing has to stay running.

## Doctor

```bash
$C doctor
```

Read-only. It prints the checkout, `git describe` revision, home, and evidence paths, and
checks three things:

- the home is under `.verify/`, never `~/.sincehello` (your real messages)
- the store's schema version matches `SCHEMA_VERSION` in the code
- the store has all 2976 sample messages

Anything else means `PROBLEM: ...` and `doctor: not ready`; relaunch. Run it whenever a
result looks off, and after any schema change.

## Drive

```bash
$C cli --name <label> -- <sincehello args>
$C scripted --name <label> --script <steps.json> -- analyze 2 --max-cost 1
```

- `cli` runs `uv run sincehello <args>` with this run's `SINCEHELLO_HOME` and
  `SINCEHELLO_TZ=America/New_York`, echoes stdout and stderr, and exits with the command's code.
- `scripted` runs `analyze` or `eval agent` in-process with the Claude client replaced by
  `tests/fake_anthropic.ScriptedClient`. The steps file is a JSON list, one entry per API
  request in order: `{"tool": <name>, "input": {...}}` for an agent turn,
  `{"critic": "pass" | <verdict>, "issues": [...]}` for a critic review, or `{"text": ...}`.
  `scripts/ily-run.json` proposes the first-"i love you" insight, has the critic pass it,
  and finishes. This is free and offline. It proves the CLI, the
  loop, tool execution, run output, and scoring, but not the model's judgment.
- Real `analyze` / `eval agent` call Claude and cost money: use `cli` with `--max-cost`, and
  only when the change is to prompts, tools, or model settings, or the user asked.

In the sample, person `2` is Alex Chen (2735 1:1 messages). Jules (3) and Priya (4) appear
only in the group chat, so they list with 0 messages.

`features/README.md` maps every user-facing command to a recipe. Open the file for the
feature you changed.

## Evidence

Every `cli` / `scripted` call writes `.verify/<run>/evidence/NN-<label>.txt` with the exact
command, exit code, stdout, and stderr. When the command creates an agent run
(`analyze`, `eval agent`), the helper also copies that run's directory (`report.json`,
`egress.json`, `run_log.jsonl`, plus `eval.json` for `eval agent`) to
`.verify/<run>/evidence/NN-<label>/<run_id>/`, so cleanup never deletes it.

Proof standards:

- Drive the real command. Calling Python functions directly is a unit test, not a
  verification.
- Prove the action and the resulting state: for a write (ingest, analyze), read it back
  through a second command (`people`, `stats`, the run dir files).
- The only allowed mock is the Claude client, via `scripted`. It is the production
  boundary, and `cli._anthropic_client` exists to be replaced. Say "scripted" whenever you
  report an agent result proven that way.
- Assert values, not just exit codes: the feature files list the sample's known answers.
- If a path can't be reached (no API key, iMessage needs Full Disk Access and real data),
  say so and name what blocked it. Do not report it as verified through another path.

## Cleanup

```bash
$C cleanup
```

Deletes `.verify/<run>/home/` and keeps `.verify/<run>/evidence/`. `.verify/` is gitignored;
never commit it, and never copy anything from `~/.sincehello` into it.

## Hard rules

- Never set `SINCEHELLO_HOME` to `~/.sincehello` and never run `connect imessage` while
  verifying. Real message data stays out of verification runs and out of the repo.
- Never run `synth generate` without `--out` inside `.verify/` unless you're deliberately
  regenerating the committed sample (see `features/synth.md`).
