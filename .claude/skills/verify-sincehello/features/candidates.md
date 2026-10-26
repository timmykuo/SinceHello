# Candidates

`sincehello candidates <person>` lists scored leads for the agent (firsts, pet names, volume
spikes, conflicts), each with the message ids behind it, with no LLM.

## Sub-features

- `candidates-all` lists leads across kinds, best first.
- `candidates-kind` filters to one of `first`, `pet_name`, `spike`, `conflict`.
- `candidates-json` prints `candidates[]`.
- `candidates-bad-kind` rejects an unknown kind.

## How to get to it (user POV)

- Run `sincehello candidates 2`, optionally with `--kind pet_name`.

## Driving it with control.py

Preconditions:

- Baseline from `features/README.md`.

- **All kinds.** Run `$C cli --name candidates -- candidates 2 --limit 12`.
  Exit `0`; the top rows include `first "i love you"  [messages 507, 509]`,
  `conflict: 60h silence, then an apology from me`, `spike on 2021-11-06 (60 messages)`,
  and `pet name "bean"`. These are planted facts.
- **One kind.** Run `$C cli --name candidates-pet -- candidates 2 --kind pet_name`.
  One row, `pet name "bean"  [messages 610, 611, 1344, 1903, 2427]`.
- **JSON.** Run `$C cli --name candidates-json -- candidates 2 --json`. Each
  entry has `kind`, `label`, `score`, and `message_ids`.
- **Bad kind.** Run `$C cli --name candidates-bad-kind -- candidates 2 --kind nope`.
  Exit `1`; `unknown candidate kind 'nope' (known: first, pet_name, spike, conflict)`.

## Gotchas

- The goose-incident inside joke isn't a candidate kind; it surfaces in `stats`
  (`distinctive_phrases`, `top_ngrams`), not here.
- Message ids printed here are numeric store ids; the sample's ids are
  `synthetic:bean-goose-s42-000507`, and the numbers line up.
- The group-chat trap: an earlier "love you" exists only in the group chat. A `first` lead
  pointing before message 507 means group messages leaked into 1:1 analysis.
