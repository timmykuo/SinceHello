# Synthetic histories (SPEC §10.1)

Fake relationship chat histories with **planted, known facts**, for testing the stats
engine and the discovery agent without real messages. Everything here is synthetic.

```sh
uv run sincehello synth list                                   # scenarios
uv run sincehello synth generate bean-goose --seed 1 --messages 12000 --out /tmp/bg
SINCEHELLO_SYNTHETIC_DIR=/tmp/bg uv run sincehello connect synthetic
uv run sincehello people
```

With `SINCEHELLO_SYNTHETIC_DIR` unset, `sincehello connect synthetic` loads the committed
sample in [`samples/bean-goose/`](samples/bean-goose) (seed 42, about 3k lines, 9 facts).
Use a throwaway `SINCEHELLO_HOME` if you don't want it mixed into your real store.

From Python:

```python
from evals import generator
records, manifest = generator.generate("bean-goose", seed=1, target_messages=5000)
generator.write(Path("out"), records, manifest)
records, manifest = generator.read(Path("evals/samples/bean-goose"))
```

## Layout

- `scenarios/<id>.json`: scenario specs (format below). `bean-goose` has a 12-week
  Tokyo long-distance era; `noodle-summit` stays in one timezone.
- `generator/`: `offline.py` (deterministic, no API), `llm.py` (optional Claude rewrite
  of filler text), `manifest.py` (ground truth + validation), `scenario.py`, `clock.py`,
  `text.py` (hand-written filler corpus).
- `samples/bean-goose/`: the committed sample. Regenerate it with
  `uv run sincehello synth generate bean-goose --seed 42 --messages 3000 --out evals/samples/bean-goose`;
  a test fails if the committed copy drifts from the generator.

## Generators

**Offline** (`generator.generate`): seeded, deterministic, needs nothing. Same scenario,
seed and `--messages` give byte-identical output. It mixes filler with the planted
blocks: each person only starts texting while awake in their own local timezone (so
sleep shifts during a long-distance era), replies follow each persona's
`reply_median_s` (log-normal), a reply that would land while the other person sleeps
ends the conversation, and every day has at least one conversation. Volume is
calibrated to `target_messages` (5k–50k works; at high volume replies speed up so the
day can hold the messages, and the manifest records the actual medians).

**LLM** (`--llm`, `evals.generator.llm.rewrite`): keeps everything the offline
generator decided (times, senders, flags, planted messages) and asks Claude
(`claude-opus-5-5` by default, `--model`/`--effort` to change) to rewrite only the filler
text, era by era, in chunks of 120 lines with planted lines shown as locked context.
Rewritten lines that are multi-line, too long, or match a reserved term fall back to the
offline text, then the manifest is recomputed and must equal the offline one. Needs
an API key; costs roughly one request per 120 lines (use
`--max-chunks` for a trial). Refusals and truncated responses keep the offline text.

## `messages.jsonl`

One JSON object per line, sorted by time, timestamps strictly increasing.

| field | type | meaning |
|---|---|---|
| `id` | string | `<history_id>-<NNNNNN>`, unique; becomes `message.source_msg_id` |
| `conv` | `"dm"` \| `"group"` | the 1:1 chat or the group chat (see `manifest.conversations`) |
| `ts` | string | UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `sender` | string | `"me"`, `"them"`, or a group friend key from `manifest.participants` |
| `text` | string \| null | null only for attachment-only messages |
| `is_reaction` | bool | iMessage-style tapback, text like `Loved “…”` (quotes the target) |
| `reacts_to` | string \| null | target message id for reactions |
| `has_attachment` | bool | |
| `edited` | bool | |
| `fact` | string \| null | ground truth: planted fact id this line belongs to |
| `role` | string \| null | ground truth: role in the fact (`key`, `reply`, `decoy`, `apology`, `callback`, `use`, ...) |

`fact` and `role` are for eval scoring and debugging only. The `synthetic` connector never
writes them to the store, so the pipeline can't see them.

The connector does load each reaction's `reacts_to` link, with a `reaction_kind` taken from the
verb (`Loved` → love, `Laughed at` → laugh, ...). By default it also loads the manifest's true
`timezones` into the store's `tz_period` table, so stats use each sender's local time. Pass
`SyntheticConnector(load_timezones=False)` to withhold them when evaluating timezone
inference (M7).

## `manifest.json`

| field | meaning |
|---|---|
| `format_version` | `1` |
| `history_id` | `<scenario_id>-s<seed>` |
| `scenario_id`, `seed`, `source` (`"synthetic"`), `description` | |
| `generator` | `{kind: offline\|llm, target_messages, ...}`; LLM runs add model, effort, counts |
| `participants` | `{me: {name, is_me}, them: {name, handle}, <friend>: {name, handle}}`; handles are fake (555-01xx numbers, example.com) |
| `conversations` | `[{key, source_conv_id, is_group, members, name?}]` |
| `timezones` | per person `[{start_utc, end_utc, tz}]`, the true periods (SPEC §6.1) |
| `eras` | `[{id, name, start_utc, end_utc, dm_messages}]` |
| `stats` | observed totals, see below |
| `facts` | planted facts with exact expected values, in scenario order |

### Definitions

These are the contract; the stats engine should match them or document a difference.

- **1:1 messages**: `conv == "dm"` and not a reaction. Attachment-only messages count in
  totals but never match a text pattern. This is the count `sincehello people` shows.
- **Text match**: case-insensitive `re.search(pattern, text)` over 1:1 messages.
- **Local time**: the sender's timezone period at that instant.
- **Reply time**: the gap between consecutive 1:1 messages from different senders, if
  it is ≤ 6h, attributed to the replier.
- **Conversation start**: the first 1:1 message, plus every 1:1 message ≥ `gap_hours`
  after the previous one.
- **Silence**: the gap between consecutive 1:1 messages.
- **Busiest day**: the calendar date (in `me`'s local time) with the most 1:1 messages.

`stats`: `lines`, `dm_messages`, `dm_by_sender`, `dm_reactions`, `group_messages`,
`attachment_only`, `edited`, `first_message_id`, `last_message_id`, `range`,
`longest_silence {hours, after_message_id, next_message_id}`, `reply_median_s`,
`late_night_share` (00:00–03:59 sender-local share of each person's 1:1 messages).

### Fact types

Every fact entry has `id` and `type` plus the fields below. All ids are message ids.

| type | expected values |
|---|---|
| `first_message` | `message_id`, `sent_at_utc`, `sender`, `text` |
| `first_phrase` (first "i love you") | `pattern`, `message_id`, `sent_at_utc`, `sender`, `sender_local_time`, `reply_message_id`, `reply_sender`, `reply_delay_s`, `count_by_sender`, `decoy_message_ids` (near-misses like "love your"), `traps` (`earliest_group_match_id`: an earlier match that is only in the group chat; `reaction_match_ids`: reactions that quote the phrase) |
| `pet_name` | `term`, `pattern`, `first_message_id`, `first_at_utc`, `first_sender`, `relationship_month` (1-based, counted from the scenario start), `count_by_sender` |
| `inside_joke` | `pattern`, `origin_message_id`, `origin_at_utc`, `origin_sender`, `origin_context_ids` (the whole origin exchange), `count`, `count_by_sender` |
| `busiest_day` | `date`, `tz`, `count`, `runner_up {date, count}`, `utc_date`, `utc_count`, `reason` |
| `conflict_makeup` | `fight_start_message_id`, `fight_starter`, `last_before_silence_id`, `silence_start_utc`, `silence_end_utc`, `silence_hours`, `apology_message_id`, `apology_sender`, `apology_text`, `is_longest_silence` |
| `long_distance` | `person`, `tz`, `other_tz`, `start_utc`, `end_utc`, `weeks`, `offset_diff_hours`, `landed_message_id` (mentions the city), `reunion_message_id`, `dm_messages_during` |
| `reply_speed` | `faster`, `median_reply_s {me, them}` |
| `texts_first` | `sender`, `gap_hours`, `starts {me, them}`, `share` |

The generator refuses to write a history whose computed facts don't match the scenario
(for example, if the planted "i love you" is not the first match, or the fight's
silence is not the longest).

## Scenario format

JSON (stdlib on both the Python and TypeScript sides, and no YAML type coercion in
ground truth). See `scenarios/bean-goose.json` for a complete example.

- `id`, `description`, `start` (date, `me`-local), `duration_days`, `target_messages`
- `personas.me` / `personas.them`: `name`, `handle` (them only), `sleep` `[start, end]`
  local, `reply_median_s`, `style` (`lowercase`, `emoji_rate`, `emojis`, `exclaim`,
  `abbrev`, `double_text`; probabilities)
- `timezones.<who>`: `[{start: UTC instant, tz: IANA}]`, increasing
- `eras`: `[{id, name, start (date), volume (relative), topics}]`; topics are pools in
  `generator/text.py`
- `initiator_share.them`, `reaction_share`, `attachment_share`, `edited_share`
- `group_chat`: `id`, `name`, `friends [{key, name, handle}]`, `share`, `script` (traps)
- `reserved`: regexes filler must never match (fact patterns and pet names are added
  automatically)
- `facts`: each has `id`, `type`, and type-specific fields. Scripted facts use
  `at` (a naive datetime in the first sender's local time, or
  `{month, day_range, time}`) and `script` `[[sender, text, gap_seconds?], ...]`.
  See the two scenarios for every type.

# Judge, human ratings, comparison (SPEC §10.2–10.4)

These score a finished run. Paid commands take a hard `--max-cost` (USD). Results go in
the run's directory under `~/.sincehello/runs/<run>/`, never in this repo.

```sh
uv run sincehello eval agent --sample bean-goose          # a run: eval.json (recall)
uv run sincehello eval judge <run> --max-cost 2           # judge.json: scores per insight
uv run sincehello eval rate <run>                         # ratings.json: your own scores
uv run sincehello eval agreement <run> [<run> ...]        # judge vs. your ratings (free)
uv run sincehello eval judge-calibration --max-cost 1     # judge vs. labelled cases
uv run sincehello eval compare <baseline> <candidate> [--pairwise --max-cost 0.5]
```

- `judge/`: `judge.md` and `pairwise.md` (the judge's system prompts; keep them free of
  run-specific text so they cache), `judge.py` (`LLMJudge`), `agreement.py`.
- `judge_calibration.py`: the critic's invented bean-goose cases plus two more, labelled
  with `keep` and the scores they should get.
- `ratings.py`: the `ratings.json` format. `runs.py`: reading a run back (report, the
  queries behind metrics from `run_log.jsonl`, the store or sample it used).

The judge scores **accuracy, specificity, delight, tone, non_obviousness** (1–5) and
`keep`. `eval rate` asks for the same five scores in one line (`5 4 4 5 3`) and 👍/👎.
`judge.json` adds run-level means, `keep_rate` and `hallucination_rate` (share of cards
with judge accuracy ≤ 2). A run id ending in `-eval-<sample>` is judged against that
synthetic sample; other runs are judged against your local store, so their excerpts go
to the API like during `analyze`.
