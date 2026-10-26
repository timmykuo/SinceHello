# SinceHello verification map

This directory is the maintained source for verifying SinceHello's user-facing behavior.
Read this index, then use the matching feature file as the recipe. `C` below is
`.claude/skills/verify-sincehello/scripts/control.py`.

## Baseline preconditions

- `$C launch` ends with `doctor: ok` (fresh `.verify/<run>/home`, bean-goose
  sample ingested, 2976 messages).
- Person `2` is Alex Chen with 2735 1:1 messages.
- No API key is needed unless a recipe says "costs money".

## Driving conventions

- Start every recipe from the baseline. A recipe that mutates the store (ingest, synth into
  the store) says so; relaunch after it.
- Run every command through `$C cli --name <label> -- ...` or
  `$C scripted ...`. Keep labels unique within a run.
- Prefer `--json` output for assertions. Text output is for the user and may change.
- Treat commands as literal. Keep quoted values and flags unchanged.

## Proof and skip reporting

- CLI proof is the evidence file: command, exit code, stdout, stderr.
- Write proof includes a second command that reads the written state back.
- Record the feature file and sub-feature ID with each cited evidence file.
- Agent results proven with `scripted` are reported as scripted. A real-model claim needs
  a real run.
- Report an unreachable path with the attempted command and the unmet precondition.

## Feature entry contract

Each feature file starts with an H1 and one paragraph on the user-visible behavior, then
exactly four H2s in this order: `Sub-features`, `How to get to it (user POV)`,
`Driving it with control.py` (starts with `Preconditions:`; labeled bullets pairing an
action with an exact command and the observable result), and `Gotchas`.

## Full sweep

Walk top to bottom for a broad regression.

- [Ingest](./ingest.md): list connectors, ingest the sample, re-ingest without duplicates.
- [People](./people.md): the person list that every other command keys off.
- [Stats](./stats.md): deterministic stats, text and JSON, timezone option.
- [Eras](./eras.md): chapters from weekly signals.
- [Candidates](./candidates.md): leads for the agent, by kind.
- [Analyze](./analyze.md): the discovery agent run and its output files.
- [Eval agent](./eval-agent.md): recall of planted facts on the sample.
- [Synth](./synth.md): scenarios and seeded generation.
