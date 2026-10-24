# SinceHello — Spec (v0.1, draft)

> "Spotify Wrapped, but for you and one person you text."
> Connect your messaging apps, pick a person, press **Analyze**, and get a set of
> warm, evidence-backed insights: your first "I love you", your pet names, your
> inside jokes, who texts first, the eras of your relationship.

This doc is the spec for a learning project. The main goal is to practice AI
engineering: agent loops, tool design, verification loops, evals, and cost control.
The product goal is a fun, accurate "Wrapped" for one relationship.

---

## 1. Decisions so far

| Topic | Decision |
|---|---|
| Users | Just me for now. The design shouldn't block letting others use it later. |
| Product vision | **Connect apps → one-click analyze.** |
| v1 source | **iMessage only**, read straight from `chat.db` on my Mac. |
| Later sources | Instagram, Messenger, WhatsApp, via guided exports (see §4.3). |
| Unofficial bridges/scrapers | Out of scope. We can revisit later. |
| Content types | **Text only** in v1. No images, voice memos, or tapback analysis. |
| Scope of one analysis | **One person, all time**, across every connected app. **1:1 conversations only**; group chats come later. |
| Sensitive moments | **Included**: fights, makeups and hard stretches are fair game, with no softening for now. We can tune the tone later. |
| Agent loop | **Hand-written manual loop** in v1. Moving to the SDK Tool Runner later is optional. |
| Critic model | **Opus 5.5 to start**, then try a cheaper model and let evals decide. |
| LLM | **Claude API.** |
| Data sent to Claude | **Snippets only**, fetched through agent tools, plus computed stats. A bulk "deep mode" is a later opt-in. |
| Redaction | None in v1. |
| Agent role | **Agentic discovery**: an agent explores the history with tools and a verifier/critic loop checks every claim. |
| Budget | **$1–$5 per full analysis run**, enforced by a hard cap. |
| Evals | **All three**: synthetic ground truth, LLM judge, and human ratings. |
| Output (v1) | **Report JSON** (plus a readable Markdown rendering). The visual UI comes later. |
| Stack | **Python core**. A TypeScript UI comes later and reads the same JSON. |

---

## 2. Can the apps be "connected" live? (research summary)

Mostly no. None of these apps offers an official API for your personal DMs.

| App | Live API for personal DMs? | Realistic "connect" |
|---|---|---|
| iMessage | No API, but the data lives in a local SQLite database | **Automatic**: read `~/Library/Messages/chat.db` (Mac, Full Disk Access) |
| Instagram | No. The Graph API covers business/creator inboxes only | **Guided export**: "Download your information" → JSON zip |
| Messenger | No. The Messenger Platform covers Pages only | **Guided export**: "Download your information" → JSON zip |
| WhatsApp | No. The Business API covers business accounts only | **Guided export**: per-chat "Export chat" → `.txt` |

So "connecting" an app means one of two connector kinds (§4):

- **Automatic connectors** read local data directly. iMessage is the only one in v1.
- **Guided connectors** walk the user through a platform export, then watch for and
  ingest the file. You do this once per app and re-import when you want fresh data.

The UX still reads as "add your apps, then one click". Some apps just need a file
drop once.

---

## 3. Architecture overview

```
 ┌───────────────┐   ┌──────────────┐   ┌───────────────┐
 │  Connectors   │──▶│  Normalized  │──▶│   Identity    │
 │ (iMessage v1) │   │ store SQLite │   │  resolution   │
 └───────────────┘   └──────────────┘   └───────┬───────┘
                                                │  one "Person" = many handles
                                                ▼
                     ┌──────────────────────────────────────┐
                     │  Local analysis (no LLM, all data)    │
                     │  • stats engine  • era detection      │
                     │  • candidate miners (firsts, phrases) │
                     │  • full-text search index (FTS5)      │
                     └───────────────┬──────────────────────┘
                                     │ tools (snippets only)
                                     ▼
       ┌──────────────────────────────────────────────────────┐
       │  LOOP 1  Discovery agent (Claude, tool use)           │
       │          explores → proposes candidate insights       │
       └───────────────┬──────────────────────────────────────┘
                       ▼
       ┌──────────────────────────────────────────────────────┐
       │  LOOP 2  Verification                                 │
       │   a) deterministic checks (evidence exists, quotes    │
       │      match, "first" really is first)                  │
       │   b) LLM critic (interpretation, tone, cuteness)      │
       │   failed → back to the agent with feedback (≤ N tries)│
       └───────────────┬──────────────────────────────────────┘
                       ▼
       ┌──────────────────────────────────────────────────────┐
       │  Curation: rank, dedupe, order into a "deck"          │
       └───────────────┬──────────────────────────────────────┘
                       ▼
                report.json  +  report.md

  Offline:  LOOP 3  Eval harness (synthetic chats, LLM judge, human ratings)
```

Everything left of the agent runs locally and can see all the data. Claude only
sees what the tools return (§7.4).

---

## 4. Connectors

### 4.1 Connector interface

```python
class Connector(Protocol):
    id: str                    # "imessage", "instagram", ...
    kind: Literal["automatic", "guided"]

    def check(self) -> ConnectorStatus: ...
        # e.g. "needs Full Disk Access", "no export found", "ready", "last synced 2d ago"
    def setup_instructions(self) -> list[str]: ...
        # guided steps shown to the user
    def ingest(self, store: Store, since: datetime | None) -> IngestReport: ...
        # incremental when possible; idempotent (re-running never duplicates)
```

Every connector writes into the same normalized schema (§5), and nothing downstream
knows which app a message came from except through the `source` field.

### 4.2 iMessage connector (v1)

- **Location:** `~/Library/Messages/chat.db`. Copy it (along with its `-wal` and `-shm`
  files) to a temp directory before reading. Never open the live database.
- **Permission:** the terminal or app needs Full Disk Access. `check()` detects the
  permission error and explains the fix.
- **Tables used:** `message`, `handle`, `chat`, `chat_message_join`, `chat_handle_join`.
- **Gotchas the connector must handle:**
  - `message.date` counts **nanoseconds since 2001-01-01 UTC** (the Apple epoch).
    Convert it, then store UTC plus the local timezone offset.
  - On newer macOS, `message.text` is often NULL and the text lives in the
    `attributedBody` blob (an NSAttributedString typedstream), so we need a decoder.
    This is the riskiest part of the connector and needs tests against real samples.
  - **Tapbacks** (`associated_message_type` 2000–2006 and 3000–3006) are stored as
    messages like `Loved "…"`. Since v1 is text-only, store them flagged
    `is_reaction=1` and **exclude them from text analysis**, or "Loved" becomes
    the top word.
  - Edited and unsent messages (macOS 13+): keep the final text and set an `edited` flag.
  - **1:1 only (v1):** ingest every conversation, but analysis uses only conversations
    where `conversation.is_group = 0`. Group chats are stored for later use.
  - `is_from_me` gives the sender direction. In 1:1 chats the other sender comes from `handle_id`.
  - One person often has **several handles** (a phone number plus an Apple ID email).
    These get merged in identity resolution (§6).
  - Attachments-only messages (U+FFFC placeholder) are counted for stats but carry no text.
- **Contact names:** optionally read the macOS AddressBook database
  (`~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb`) to map
  handles to names for the person picker. If that's unavailable, fall back to the raw handle.
- **Incremental sync:** remember the max `ROWID` ingested and only pull newer rows next time.

### 4.3 Guided connectors (later phases, designed now)

| App | Export | Parsing notes |
|---|---|---|
| Instagram | Accounts Center → Download your information → **JSON**, Messages | `messages/inbox/<thread>/message_N.json`. Text is **mojibake** (UTF-8 bytes stored as Latin-1 escapes) and must be re-decoded. Timestamps are in ms. |
| Messenger | Same flow, Facebook → Messages, **JSON** | Same format and mojibake issue as Instagram. With end-to-end encrypted chats, the export may be incomplete. Document this for the user. |
| WhatsApp | Per chat → Export chat → Without media | `.txt` format, and the date format **depends on the phone's locale**. Needs a tolerant parser plus a confirmation step ("we read this as DD/MM — correct?"). |

The guided UX: show the steps → the user picks the zip/txt (or drops it in a watched
`imports/` folder) → `check()` validates it → ingest.

---

## 5. Normalized data model (SQLite, local)

```sql
person(id, display_name, is_me)
handle(id, person_id NULL, source, raw_value)          -- phone, email, IG username...
conversation(id, source, source_conv_id, is_group)
conversation_member(conversation_id, handle_id)
message(
  id, source, source_msg_id, conversation_id,
  sender_handle_id, is_from_me,
  sent_at_utc, tz_offset_min,
  text, is_reaction, has_attachment, edited,
  UNIQUE(source, source_msg_id)
)
message_fts  -- FTS5 virtual table over message.text (for the search tool)
```

Derived tables, all rebuilt per analysis run and cached:
`stats_cache`, `era`, `candidate`, `insight`, `run_log`, `egress_log`.

---

## 6. Identity resolution

The goal: **one Person = every handle, on every app, that belongs to the same human.**

- Automatic merges: handles that share a phone number after E.164 normalization,
  and handles linked to the same AddressBook contact.
- Cross-app merges are manual in v1. The user confirms "this IG username is the same
  person as this phone number". There's no fuzzy auto-merge in v1, because merging the
  wrong person would wreck the report.
- The user's own handles are marked as `is_me`.

---

## 7. Analysis pipeline

### 7.1 Local stats engine (deterministic, no LLM)

These are computed over all messages with the chosen person. Exact numbers always
come from here, never from the LLM.

- Message totals per person, per year and month, and per weekday × hour heatmap
- First message ever (and who sent it), plus "since hello" duration
- Busiest day ever, longest silence, longest daily streak
- **Who texts first**: the share of conversation-starts after a gap of ≥ N hours, for each person
- Reply-time distributions (median, by person, by era)
- Late-night share (00:00–04:00 local)
- Top words (stopwords removed), top bigrams and trigrams, top emoji, by person
- **Distinctive phrases**: words and n-grams that are frequent here but rare in a
  baseline. The baseline is the user's other conversations or a general English
  frequency list. Distinctive phrases are prime candidates for inside jokes.
- Message-length stats and the "double-texter" score

### 7.2 Era detection (local)

This splits the relationship into chapters without sending everything to Claude.

- Signals per week: message volume, reply-time median, late-night share,
  vocabulary drift (cosine distance between rolling word distributions), and the
  share of affectionate terms (from a small local lexicon).
- Algorithm: change-point detection, e.g. PELT via `ruptures` on the normalized
  signal vector, with a minimum era length (~6 weeks) and a maximum of ~8 eras.
- Output: `era(id, start, end, signal_summary)`. Naming and summarizing the eras is
  the agent's job: it calls `sample_era` and reads a sampled window from each.

### 7.3 Candidate miners (local, cheap)

These give the agent leads to check. They don't decide anything.

- Firsts: the earliest match for curated pattern families such as "i love you / ily /
  love u", "goodnight / gn", "miss you", "babe / baby / ...", and "happy birthday".
- Pet-name candidates: short tokens that appear at message start or end, that one
  person uses a lot more than the other, and that aren't in a standard dictionary.
- Spikes: days with volume > 3σ above the mean (often big moments).
- Conflicts and makeups: long gaps or sharp drops in tone, followed by apology terms.
  These are included as insights (see §12).

### 7.4 Agent tools (the only window Claude has into the messages)

| Tool | Returns | Notes |
|---|---|---|
| `get_overview()` | Person names, date range, totals, era list, top-level stats | Called first; a cheap orientation |
| `get_stats(name, params)` | One stat block from §7.1 | Numbers only, no message text |
| `search_messages(query, mode, sender?, start?, end?, limit≤25)` | Matching messages (id, ts, sender, text) | `mode`: keyword (FTS5) or regex |
| `first_occurrence(pattern, sender?)` | Earliest match plus its id | The deterministic answer to "first time…" |
| `count(pattern, sender?, group_by?)` | Counts by sender, year, or era | For "you said X 412 times" |
| `get_context(message_id, before≤15, after≤15)` | Surrounding messages | For reading a moment |
| `sample_era(era_id, n≤40, strategy)` | A sample of messages from one era | `strategy`: random, spike_days, or longest_threads |
| `list_candidates(kind)` | Miner output from §7.3 | Leads, not facts |
| `propose_insight(insight)` | The validation result | Validates against the schema and runs the deterministic checks immediately |
| `finish(summary)` | — | Ends the discovery loop |

Tool design rules:

- Every tool caps how many messages it returns and logs each message id it sends to
  `egress_log`. The run report states how many messages and what percentage of the
  history were shared with Claude.
- Tool results are compact JSON, with long messages truncated to about 500 characters.
- `strict: true` schemas on every tool.

### 7.5 LOOP 1 — the discovery agent

- **Model:** `claude-opus-5-5` by default (configurable, §9), with adaptive thinking
  and an explicit `effort` setting. Effort is the lever to tune in evals.
- **Loop implementation:** a **hand-written manual loop** (`while stop_reason ==
  "tool_use"`), built on the plain `anthropic` SDK without an agent framework. It's
  about 100 lines, and owning it makes the budget guard, egress logging, the
  verification hook in `propose_insight`, and mid-run system messages
  straightforward. Moving to the SDK Tool Runner later is an option once the loop is
  well understood.
- **System prompt sets:** the goal (find warm, specific, *true* insights), the seed
  categories (§8), the rule "every claim must cite message ids, and numbers must come
  from stats or count tools", and the tone guidelines (§12).
- **Agenda:** the agent works through the seed categories, then has free exploration
  time for "anything else delightful". It ends with `finish`, or when it hits the turn
  cap or token budget.
- **Stopping and guards:**
  - Max turns, e.g. 60.
  - Per-run token budget via `task_budget` (beta) so the agent paces itself, plus a
    **hard dollar cap** enforced in our own code from `response.usage`.
  - At 80% of budget, a mid-conversation system message tells it to wrap up.
- **Context management:** prompt caching on the system prompt and tools. Tool-result
  clearing (context editing) for old search results once insights have been proposed.
- **Refusals:** check `stop_reason == "refusal"` and enable server-side fallbacks.
  Intimate content can trip classifiers, so log refusals and continue with other categories.

### 7.6 LOOP 2 — verification

Each proposed insight passes through:

1. **Deterministic checks**, run in `propose_insight` itself:
   - Every cited `message_id` exists and belongs to this person's conversations.
   - Quoted text appears verbatim in the cited message.
   - `first`-type claims: re-run `first_occurrence` with the same pattern; the cited id must match.
   - Numeric claims must reference a stat or count query id. We recompute the query and compare.
   - Failures go straight back to the agent as tool errors, with the reason.
2. **LLM critic** (a separate call that is not in the agent's context), run on
   insights that pass step 1. It gets the insight plus the cited messages and their
   context and returns `{verdict: pass|revise|reject, issues[], scores{accuracy,
   specificity, delight, tone}}`.
   - It catches misreadings: sarcasm read as sincere, "love you" said to a third person
     in a forwarded message, inside jokes misattributed.
   - `revise` goes back to the agent with the critic's notes, at most 2 revisions per insight.
3. Only `pass` insights move on to curation.

### 7.7 Curation

- Dedupe overlapping insights (e.g. "pet name: bub" vs. "you call each other bub").
- Score = critic delight × specificity × confidence. Make sure categories are spread
  out (§8).
- Order the deck like Wrapped: opener (since hello) → stats → language → milestones
  → eras story → closer.
- The target deck size is 12–20 cards (configurable).

---

## 8. Insight catalog (seed goals; the agent can find others)

| Category | Examples |
|---|---|
| **Milestones / firsts** | First message ever; first "I love you" (who said it, plus a reply-time callout); first "goodnight"; first pet name; first "miss you"; first plan to meet |
| **Language** | Most-used words and emoji, per person; pet names and their evolution; inside jokes (distinctive phrase + origin story via `first_occurrence` + `get_context`); "your words": phrases only the two of you use |
| **Rhythms / stats** | Total messages; busiest day ever (and what happened); who texts first; reply-speed champion; night-owl hours; longest streak; double-texter award |
| **Story / eras** | 3–8 named chapters ("The 2am Era", "Long-Distance Summer") with dates, a 1–2 sentence summary, and one representative quote each |
| **Free discovery** | Anything else that is delightful and verifiable |

### 8.1 Insight schema (the core of `report.json`)

```json
{
  "id": "ins_014",
  "category": "milestone",
  "kind": "first_occurrence",
  "title": "The first \"I love you\"",
  "headline": "Alex said it first — March 3, 2021, 11:48pm",
  "body": "You replied 41 seconds later with \"I love you too 🥹\".",
  "metric": { "query_id": "q_first_ily", "value": "2021-03-04T04:48:00Z" },
  "evidence": [{ "message_id": "imessage:8812", "quote": "i love you" }],
  "era_id": "era_2",
  "scores": { "accuracy": 5, "specificity": 5, "delight": 5, "tone": 5 },
  "verification": { "deterministic": "pass", "critic": "pass", "revisions": 0 }
}
```

Top-level `report.json` fields: `person`, `range`, `sources`, `deck[]` (ordered insight
ids), `insights[]`, `eras[]`, `stats{}`, and `run{cost_usd, tokens, turns,
messages_shared, pct_shared, model, version}`.

---

## 9. Models and cost

Prices are from the current price list (per 1M tokens, input/output):
Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50. Cache reads cost much less
than fresh input.

| Role | Default model | Notes |
|---|---|---|
| Discovery agent | `claude-opus-5-5` | Effort tuned via evals |
| Critic | `claude-opus-5-5` | Starts on the strongest model to set the quality bar. Later, eval `claude-sonnet-5-5`/`claude-haiku-5-5` as the critic, and switch only if agreement with the Opus critic and with human ratings holds |
| LLM judge (evals only) | `claude-opus-5-5` | Batch API for eval runs (50% cheaper) |

**Rough estimate for one run** (to be measured, not trusted). A 60-turn agent loop
with ~40k cached prefix plus growing tool results comes to about 2–3M input tokens,
mostly cache reads, plus ~60k output. Add ~30 critic calls. **That likely lands at
$1–3 on Opus**, inside budget.

**Budget guard:** `--max-cost` defaults to $5. The run aborts cleanly and writes a
partial report once the projected spend crosses it. The cost-per-run is recorded in
`run_log` so evals can track cost as well as quality.

---

## 10. Evals (LOOP 3)

### 10.1 Synthetic ground truth

- A **generator** (Claude) writes synthetic relationship histories from a
  scenario spec: personas, duration, eras, and **planted facts**.
  - Planted facts include: first "ily" on date X by person A, pet name "bean" from
    month 4, an inside joke "the goose incident" that originates on day Y, a
    long-distance era, and a busiest day.
  - Generation happens in chunks (era by era) to reach realistic sizes (5k–50k
    messages). A filler model adds mundane chatter, because a realistic signal-to-noise
    ratio matters.
  - Output goes into the same normalized schema through a `synthetic` connector, so the
    full pipeline runs unchanged.
- **Metrics:**
  - Recall of planted facts (did an insight capture it?)
  - Exactness: date, sender, and value correct
  - Hallucination rate: insights with claims that contradict the data
  - Cost and turns per run
- These are cheap, repeatable, and **never use your real messages**, so they can run in CI.

### 10.2 LLM-as-judge

- A rubric score from 1–5 on accuracy, specificity, delight, tone, and
  non-obviousness, run on every insight in a report.
- The judge is a **separate prompt and rubric from the pipeline critic** (§7.6). That
  way the eval doesn't share the critic's blind spots and doesn't just grade the
  pipeline against itself.
- Pairwise mode for comparing two pipeline versions: "which deck is better?",
  with the order randomized.

### 10.3 Human ratings (real data, you)

- A small CLI rater shows each insight with its evidence and asks for 1–5 ratings
  plus 👍/👎 on "would I want to see this?"
- Ratings are stored locally. They're used to **calibrate the judge** (agreement
  rate between judge and human; refine the rubric until agreement is acceptable).

### 10.4 Iteration workflow

Change a prompt, tool, or effort setting → run the synthetic suite → compare against the
baseline on recall, hallucination rate, judge score, and cost → spot-check real data
with human rating → keep or revert. Every run's config and results are versioned in
`evals/runs/`.

---

## 11. Interface (v1 CLI)

```
sincehello connect                 # list connectors and their status
sincehello connect imessage        # check access, ingest
sincehello people                  # list people, by message count
sincehello merge <person> <handle> # manual identity merge
sincehello analyze <person> [--max-cost 5] [--deck-size 15]
                                   # the "one click": sync all connected sources → analyze
sincehello report <run-id>         # print report.md
sincehello rate <run-id>           # human rating pass
sincehello eval [--suite synthetic]
```

The later TypeScript UI is a front end for these same commands and the
`report.json` contract.

---

## 12. Privacy, safety, tone

- **Local-first:** the database, the derived data, and the reports all stay on disk
  under `~/.sincehello/`. Only tool-returned snippets go to the Claude API.
- **Transparency:** every run reports how many messages were shared and which ones
  (`egress_log`), and the user can inspect it.
- **API key:** stored in an env var or keychain, never written to the repo.
- **Tone** (system prompt plus critic rubric): honest and specific. Fights, makeups,
  hard stretches, and breakups-and-reunions are **included** with no softening in
  v1. Tone is a single prompt/rubric section so it's easy to adjust later.
  Third parties (exes, friends) appear only when they are part of the
  story between the two of you.
- **Other people's privacy:** this person never consented to analysis. That's fine
  for personal use. For the "others use it" future, the README should frame it as a
  gift-making tool for your own conversations.
- **Multi-user future:** the local-first design means a hosted version would need an
  explicit rethink (storage, consent, retention). That's out of scope here.

---

## 13. Repo layout (proposed)

```
sincehello/
  connectors/      base.py, imessage.py, synthetic.py, (instagram.py, whatsapp.py later)
  store/           schema.sql, store.py, identity.py
  analysis/        stats.py, eras.py, miners.py
  agent/           loop.py, tools.py, prompts/, budget.py
  verify/          checks.py, critic.py
  curate/          rank.py
  report/          schema.py (pydantic), render_md.py
  cli.py
evals/
  scenarios/       synthetic scenario specs with planted facts
  generator/       synthetic history generation
  judge/           rubric + judge
  runs/            versioned results
tests/
web/               (later) TypeScript UI
```

Key libraries: `anthropic`, `pydantic`, `typer`, `ruptures`, `sqlite3`/FTS5, `pytest`.

---

## 14. Milestones

1. **M1 — Data:** iMessage connector (including the `attributedBody` decoder), normalized
   store, people list, identity merge. *Done when:* `sincehello people` shows the right
   counts for my top contacts.
2. **M2 — Local analysis:** stats engine, era detection, miners, and a stats-only
   report (no LLM yet).
3. **M3 — Synthetic data plus eval skeleton:** generator, scenarios, recall metrics.
   This comes before the agent so the agent is built against evals from day 1.
4. **M4 — Discovery agent (LOOP 1):** tools, manual loop, budget guard, egress log.
5. **M5 — Verification (LOOP 2):** deterministic checks plus critic, and the revise loop.
6. **M6 — Curation plus final report;** human rater; LLM judge calibrated against my ratings.
7. **M7 — Iterate:** tune prompts, effort, and tools against the evals.
8. **Later:** guided connectors (Instagram, Messenger, WhatsApp), TypeScript story-card UI,
   image export, bulk "deep mode", reactions and images, year-in-review mode.

---

## 15. Open questions

1. **"Me" in the report:** should insights be written as "you" and "Alex", or with both
   names in the third person ("<your name> and Alex"), which is better for sharing later?
2. **Timezones:** if either of you moved or traveled, local time for late-night stats
   is ambiguous. Use the Mac's current timezone, or let you set timezone periods?
3. **Language:** English only, or should the lexicons and stopwords handle other
   languages or mixed-language texting?
4. **Re-runs:** should a second run on the same person be deterministic, or is it fine
   (even fun) if the agent finds different insights each time?
