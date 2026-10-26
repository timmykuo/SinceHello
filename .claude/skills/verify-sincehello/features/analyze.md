# Analyze

`sincehello analyze <person>` runs the discovery agent on one person and writes the run to
`$SINCEHELLO_HOME/runs/<run_id>/`: the draft report, an egress log of what went to Claude,
and an event log.

## Sub-features

- `analyze-run` runs the loop and prints status, insight count, turns, cost, and share.
- `analyze-critic` reviews each proposed insight with the LLM critic and prints its line;
  `--no-critic` skips it, `--critic-model` / `--critic-effort` configure it.
- `analyze-output` writes `report.json`, `egress.json`, and `run_log.jsonl`.
- `analyze-options` honors `--max-cost`, `--effort`, `--max-turns`, `--model`.
- `analyze-bad-effort` rejects an unknown effort.
- `analyze-unknown` rejects an unknown person.

## How to get to it (user POV)

- Run `sincehello analyze 2 --max-cost 5`, optionally with `--no-critic`.

## Driving it with control.py

Preconditions:

- Baseline from `features/README.md`.
- Scripted recipes need nothing else. The real-model recipe needs `ANTHROPIC_API_KEY` and
  costs money.

- **Scripted run.** Run
  `$C scripted --name analyze-scripted --script .claude/skills/verify-sincehello/scripts/ily-run.json -- analyze 2 --max-cost 1 --effort medium --critic-effort low`.
  Exit `0`; `finished: 1 insights in 3 turns`, `critic: 1 calls, ...; kept 1 pass`, and
  `scripted: 4 requests, 0 steps unused`.
- **No critic.** Copy `ily-run.json` without its `{"critic": ...}` step, then run it with
  `-- analyze 2 --max-cost 1 --no-critic`. No `critic:` line, `scripted: 3 requests`.
- **Run output.** Open the second `evidence:` path the run printed
  (`.verify/<run>/evidence/NN-analyze-scripted/<run_id>/`). It holds exactly `report.json`, `egress.json`,
  `run_log.jsonl`. `report.json` has one insight titled `The first "I love you"` with
  evidence ids `...000507` and `...000509`; `egress.json` has `total_messages` `2735`.
- **Bad effort.** Run `$C cli --name analyze-bad-effort -- analyze 2 --effort extreme`.
  Exit `1`; `--effort must be one of low, medium, high, xhigh, max`. No API call.
- **Unknown person.** Run `$C cli --name analyze-unknown -- analyze 99`. Exit
  `1`; `unknown person 99 (see \`sincehello people\`)`. It fails before any API call, so no
  key is needed.
- **Real model (costs money).** Run `$C cli --name analyze-real -- analyze 2 --max-cost 1 --effort low --max-turns 15`.
  Status line reports cost at or under `$1`, and `egress.json` lists only ids the run's
  tool results returned.

## Gotchas

- Scripted cost (`$0.0873` for three turns) comes from the fake usage numbers, not a bill.
  Never cite it as a real cost.
- `messages shared` counts messages sent to Claude, by the agent's tools and the critic's
  excerpts. The scripted ILY run reports `12 messages shared (0.44%)`, all from the critic;
  with `--no-critic` it reports `0`, because the script cites ids without reading them. A
  real run that reports 0 shared is wrong.
- Each proposed insight costs one critic request. A script missing a `{"critic": ...}` step
  fails with `the agent made more requests than the script has`.
- A script that runs out raises inside the loop; `scripted: ... steps unused` shows how
  much of it ran. Script exactly the turns the change needs.
- Insights in `report.json` are unranked. Curation is M6.
