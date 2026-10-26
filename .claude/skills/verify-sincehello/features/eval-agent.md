# Eval agent

`sincehello eval agent --sample <name>` runs the agent on a committed synthetic sample in a
throwaway store and scores which planted facts its insights captured. This is the
number that says whether an agent change helped.

## Sub-features

- `eval-run` ingests the sample into a temp store, runs the agent, and prints the run line.
- `eval-score` prints `[x]`/`[ ]` per planted fact and `recall found/total (pct)`.
- `eval-output` writes `eval.json` next to the run's report.
- `eval-unknown-sample` rejects a missing sample.

## How to get to it (user POV)

- Run `sincehello eval agent --sample bean-goose --max-cost 5`.

## Driving it with control.py

Preconditions:

- Baseline from `features/README.md` (the eval brings its own store; the home only holds
  `runs/`).

- **Scripted score.** Run
  `$C scripted --name eval-scripted --script .claude/skills/verify-sincehello/scripts/ily-run.json -- eval agent --sample bean-goose --max-cost 1`.
  Exit `0`; nine fact rows with only `[x] first_ily        first_phrase     ins_001`, then
  `recall 1/9 (11%)`, then `scripted: 4 requests, 0 steps unused` (the critic runs by default;
  add `--no-critic` and drop the critic step to skip it).
- **Output.** The run dir ends in `-eval-bean-goose` and has `eval.json` with `found` `1`,
  `total` `9`.
- **Unknown sample.** Run `$C cli --name eval-unknown -- eval agent --sample nope`.
  Exit `1`; `no sample 'nope' in .../evals/samples`.
- **Real recall (costs money).** Run `$C cli --name eval-real -- eval agent --sample bean-goose --max-cost 3`.
  Compare recall against the last real run before and after the change. Report both numbers,
  the model, the effort, and the cost.

## Gotchas

- The nine planted facts are `first_message first_ily pet_bean goose_incident moving_day the_fight tokyo reply_speed texts_first`.
- Scripted recall proves the scorer and wiring only. Agent quality claims need a real run,
  and one real run is noisy: run it twice before calling a change a regression.
- Scoring traps: citing only the reply (`...000509`) finds `first_ily` but isn't exact,
  and citing the group-chat "love you" (`...000215`) counts as nothing.
