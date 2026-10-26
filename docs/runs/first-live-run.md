# First live run: bean-goose

2026-10-26. This was the first run of the discovery agent against the real API, on the committed
synthetic sample `bean-goose` (seed 42). No real messages were involved.

```
uv run sincehello eval agent --sample bean-goose --max-cost 1
```

Settings: model `claude-opus-5-5`, effort `high`, `--max-turns 60` (default), hard cap $1.

## Attempt 1: failed on turn 1 (bug, fixed)

```
api_error: 0 insights in 1 turns, $0.0000
400 invalid_request_error: Schemas contains too many optional parameters (38), which would
make grammar compilation inefficient. Reduce the number of optional parameters in your tool
schemas (limit: 24).
```

The API allows at most 24 optional parameters across all `strict: true` tool schemas.
`get_stats.params` declared each stat parameter as its own optional key, which accounted for
21 of the 38. The fix changes two things:

- `start` and `end` move to the top level of `get_stats`.
- `params` becomes a list of `{name, value}` pairs, with `name` as an enum of every stat
  parameter.

Integer parameters are cast back to `int` (and a non-integral value is rejected). The total
is now 20 optional parameters, and every tool stays strict.

The test `test_strict_tools_fit_the_api_limit_and_get_stats_takes_param_pairs` in
`tests/test_agent_loop.py` uses `ScriptedClient`. It asserts the limit and drives the new
`get_stats` form through the loop. SPEC §7.4's tool table is updated to match.

## Attempt 2: finished

| | |
|---|---|
| Status / stop reason | `finished` (the agent called `finish`) |
| Turns | 20 of 60 |
| Cost | **$0.3797** of the $1 cap |
| Insights | 16 proposed, 15 accepted, 1 failed and was then fixed |
| Recall | **9/9** planted facts, all exact |
| Egress | 33 messages shared, out of 2,735 (1.21%) |

### Recall

| Fact | Type | Found in |
|---|---|---|
| first_message | first_message | ins_001, ins_011 |
| first_ily | first_phrase | ins_002, ins_009 |
| pet_bean | pet_name | ins_004, ins_012 |
| goose_incident | inside_joke | ins_003 |
| moving_day | busiest_day | ins_008, ins_011 |
| the_fight | conflict_makeup | ins_005 |
| tokyo | long_distance | ins_007, ins_014 |
| reply_speed | reply_speed | ins_010 |
| texts_first | texts_first | ins_011 |

None were missed. Some matches are loose: ins_011 ("Alex starts the conversation") also
counts toward first_message and moving_day.

### Verification failures

There was one failure, on turn 15 (the 💀 vs 😂 emoji insight). Both quotes failed the
verbatim check because the model turned each emoji in its quotes into a stray `\ns`:

| Message | Actual text | Quoted by the model |
|---|---|---|
| 000505 | `'i need a nap!! 💀'` | `'i need a nap!! \ns\ns'` |
| 000509 | `'i love you too 🥹'` | `'i love you too \ns'` |

It resubmitted correctly on turn 16. All 15 accepted insights pass the deterministic
checks. The critic is still `pending` on every insight, because there's no critic pass yet.

There were also two tool errors, both from `get_stats` asking for a parameter the stat
doesn't take: `distinctive_phrases does not take top_n` and `top_emoji does not take top_n`.
Both stats call it `k`.

### Tokens and cache

| | Tokens |
|---|---|
| Cache reads | 282,347 |
| Cache writes (5 min) | 24,108 |
| Uncached input | 501 |
| Output | 10,032 |

Cache reads made up **92.0%** of all input tokens. The system prompt and tool prefix held
steady across all 21 requests.

Tool calls: 16 `propose_insight`, 7 `search_messages`, 6 `first_occurrence`, 5 `get_stats`,
4 `get_context`, 3 `count`, 1 `get_overview`, 1 `finish`. The agent never called
`list_candidates` or `sample_era`.

### Example insights (from `report.json`)

- **ins_002, The first "i love you"**: "Alex said "i love you" first, at 11:48pm on May 14,
  2021, and you said it back within a minute." Body: "It came out of nowhere, three words
  late at night. Your reply in the same minute: "i love you too 🥹"."
- **ins_003, The goose incident**: "On April 3, 2021, a goose chased Alex across a parking
  lot, and "the goose incident" has come up 32 times since." Body: ""it hissed. i dropped my
  whole coffee." Alex named it on the spot and you promised "the goose incident will be
  remembered". You were right: it came up 18 times in 2021 and 14 more in 2022."
- **ins_005, The longest silence, and how it ended**: "After a fight on September 18, 2021,
  you went 60 hours without talking, the longest gap in your history, and you were the one
  who broke it." Body: "Alex wrote "i need some space tonight." Two and a half days later
  you sent "i'm sorry about saturday. i was wrong and i hate not talking to you", and five
  minutes later Alex replied "i'm sorry too. i overreacted. can i call you?""
- **ins_007, The Tokyo chapter**: "For 84 days, from January 9 to April 2, 2022, Alex was
  in Tokyo, 14 hours ahead, and you still exchanged 600 messages." Body: "It opened with
  "just landed in tokyo!! everything smells like vending machine coffee" and ended with
  "i'm home. come over??" on April 3. Your waking hours overlapped by only about five hours
  a day."
- **ins_010, Alex replies fast**: "Alex's median reply time is 74 seconds. Yours is about 8
  minutes (494 seconds)." Body: "Even 90% of Alex's replies come within about 6 minutes.
  That includes the morning after your fight, when "i'm sorry too" came five minutes after
  your apology."

## What the agent struggled with, and suggested changes

1. **Stat parameter names are guessed.** `get_overview.available_stats` lists only a
   summary per stat. The `params` enum is the union of every stat's parameters, so `top_n`
   looks valid everywhere, and the agent guessed it twice. Suggested fixes:
   - List each stat's parameters and defaults in `available_stats`.
   - Make the error say what the stat *does* take, e.g. "top_emoji takes: k".
   - Rename `k` and `top_n` to one name across stats.
2. **There's no way to browse a time window.** To read the messages from a given window,
   the agent called `search_messages` twice with the dummy query `"synthetic"` (0 hits).
   It then fell back to the regex `"."` with `start`/`end`. Suggested fixes:
   - Allow an empty `query` that matches everything.
   - Or add a `mode: "all"` to `search_messages`.
   - Or document the `"."` regex trick in the tool description.
3. **Emoji in quotes get mangled.** See the verification failure above. The checker's
   error message told the agent what was wrong and it recovered in one turn. To avoid the
   wasted turn:
   - Add a prompt line: "quote the words; you may drop emoji at the ends of a quote".
   - Or let the verbatim check accept a quote that matches the message with its leading
     and trailing emoji and whitespace stripped.
4. **The agent ignores the miners and eras.** It never called `list_candidates` or
   `sample_era`, and found everything through regex searches instead. That worked on a
   sample with clean planted facts, but it's unlikely to hold up on real, noisy history.
   Suggested fixes:
   - Recommend `list_candidates` right after `get_overview` in `prompts/system.md`.
   - Or include the top few candidates directly in `get_overview`.
5. **The agent may be undershooting its budget.** Its first thinking block was "Given the
   limited token budget, I should keep my response efficient". It then stopped at 20/60
   turns and $0.38/$1, with no long-tail findings beyond "min late". Suggested fixes:
   - Check what `task_budget_tokens` sends for a $1 cap.
   - Consider a prompt line saying that exploring breadth is worth the budget.
6. **Insights overlap.** For example, ins_012 "first goodnight" repeats the bean
   storyline, and ins_011 is matched to three facts. When the deck is built, dedup or rank
   them, or ask the agent for one insight per storyline.
