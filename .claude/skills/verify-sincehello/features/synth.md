# Synth

`sincehello synth` lists the eval scenarios and generates seeded synthetic histories
(`messages.jsonl` + `manifest.json` with planted facts). Same scenario and seed, same bytes.

## Sub-features

- `synth-list` lists scenarios with their planted-fact descriptions.
- `synth-generate` writes a history and prints line counts and planted-fact count.
- `synth-reproducible` regenerates the committed sample byte for byte.
- `synth-ingest` loads a generated history through the synthetic connector.

## How to get to it (user POV)

- Run `sincehello synth list`, then `sincehello synth generate <scenario> --seed N --out <dir>`.

## Driving it with control.py

Preconditions:

- Baseline from `features/README.md`.

- **List.** Run `$C cli --name synth-list -- synth list`. Exit `0`; rows for
  `bean-goose` and `noodle-summit`.
- **Reproduce the sample.** Run
  `$C cli --name synth-repro -- synth generate bean-goose --seed 42 --messages 3000 --out .verify/<run>/synth/bean-goose`.
  Prints `9 planted facts`. Then
  `diff -q .verify/<run>/synth/bean-goose/messages.jsonl evals/samples/bean-goose/messages.jsonl`
  and the same for `manifest.json` both report no difference.
- **Other scenario.** Run
  `$C cli --name synth-noodle -- synth generate noodle-summit --seed 1 --messages 2000 --out .verify/<run>/synth/noodle`.
  Exit `0`; a `noodle-summit-s1:` line.
- **Ingest it (mutates the store).** Run
  `SINCEHELLO_SYNTHETIC_DIR=.verify/<run>/synth/noodle $C cli --name synth-ingest -- connect synthetic`.
  It prints `2071 new of 2071 messages`. Then `$C cli --name synth-people -- people`
  shows `Jordan Lee` with `1911` next to Alex Chen's `2735`: ingest adds to the store, it
  doesn't replace it. Relaunch afterwards.

## Gotchas

- The default `--out` is `./synthetic/<history id>`. Always pass `--out` under `.verify/`.
- The committed sample uses `--messages 3000`. Without it the scenario default (about 12k
  lines) is generated, and it won't match.
- A generator change that alters the reproduce diff must regenerate `evals/samples/bean-goose`
  in the same change, or `test_committed_sample_is_reproducible` fails.
- `--llm` rewrites filler with Claude and costs money; planted lines stay locked, so
  manifests match the offline run.
