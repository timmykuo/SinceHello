# Stats

`sincehello stats <person>` prints deterministic stats for your 1:1 history with one person
(no LLM), each tagged with the `query_id` that insights cite. `--json` prints the same data
for machines.

## Sub-features

- `stats-text` prints the human summary with a `[q_...]` id per line.
- `stats-json` prints every stat under `stats.<name>`.
- `stats-tz` applies `--tz` to people with no timezone periods.
- `stats-gap` changes the conversation gap with `--gap-hours`.
- `stats-unknown` rejects an unknown person.

## How to get to it (user POV)

- Run `sincehello stats 2`, or `sincehello stats 2 --json`.

## Driving it with control.py

Preconditions:

- Baseline from `features/README.md`.

- **Text.** Run `$C cli --name stats -- stats 2`. Exit `0`; it starts
  `Stats for Alex Chen`, and includes `Messages               2735  (1163 / 1572)`,
  `Busiest day            2021-11-06 (60 messages)`, `First message          them at 2021-02-12T19:42:00-05:00`,
  and `Cross-timezone         2022-01-09 to 2022-04-02: them Asia/Tokyo (+14h)`.
- **JSON.** Run `$C cli --name stats-json -- stats 2 --json`. Stdout parses as
  JSON (strip the trailing `evidence:` line), `person_id` is `2`, and `stats` has the 16 keys
  `totals heatmap first_message busiest_day longest_silence longest_streak who_texts_first reply_times late_night message_length double_texting top_words top_ngrams top_emoji distinctive_phrases cross_timezone`.
- **Gap option.** Run `$C cli --name stats-gap -- stats 2 --gap-hours 2`. The
  `Texts first` start count differs from the default's `441 starts`, and the `q_...` ids
  change.
- **Unknown person.** Run `$C cli --name stats-unknown -- stats 99`. Exit `1`;
  stderr `unknown person 99`.

## Gotchas

- The sample has timezone periods for both people, so `--tz Asia/Tokyo` changes only the
  `q_...` ids, not the values. A `--tz` change shows up only for people without periods.
- `query_id`s hash the stat's parameters. A changed id with unchanged values is expected
  when you change an option; a changed id with no option change means a parameter leaked in.
- Results must not depend on the wall clock. Run the same command twice; outputs match
  byte for byte except the evidence path.
- `since hello` durations are measured to the last message, not to today.
