# Eras

`sincehello eras <person>` splits the history into chapters from weekly signals (volume,
reply speed, late-night share, affection, timezone gap), with no LLM.

## Sub-features

- `eras-text` prints one line per era with dates, counts, and signals.
- `eras-json` prints `eras[]` with `id`, `start_utc`, `end_utc`, `message_count`, `signals`.
- `eras-options` honors `--min-weeks` and `--max-eras`.

## How to get to it (user POV)

- Run `sincehello eras 2`.

## Driving it with control.py

Preconditions:

- Baseline from `features/README.md`.

- **Text.** Run `$C cli --name eras -- eras 2`. Exit `0`; six lines `era_1`
  to `era_6`. The first starts `2021-02-13` (173 msgs); `era_5` is
  `2022-01-10 -> 2022-03-28` with `tz gap 14h`, the planted Tokyo stretch.
- **JSON.** Run `$C cli --name eras-json -- eras 2 --json`. `eras[4].signals.tz_gap_hours`
  is `14.0`, and the six `message_count`s sum to 2735.
- **Fewer eras.** Run `$C cli --name eras-max3 -- eras 2 --max-eras 3`. At most
  three lines, still spanning 2021-02-13 to 2022-05-08.

## Gotchas

- Era boundaries fall on week starts, so they won't match the manifest's exact tz switch
  times (`2022-01-09T05:00Z`); allow a few days.
- Eras partition the 1:1 messages. If the counts don't sum to 2735, something dropped or
  double-counted a message.
