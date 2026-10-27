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
| Content types | **Text only** in v1. No images or voice memos. Reactions are excluded from text analysis, but each one is stored with a link to the message it reacts to and its kind (love, laugh, ...), so insights like "most-hearted message" are possible. |
| Scope of one analysis | **One person, all time**, across every connected app. **1:1 conversations only**; group chats come later. |
| Sensitive moments | **Included**: fights, makeups and hard stretches are fair game, with no softening for now. We can tune the tone later. |
| Agent loop | **Hand-written manual loop.** |
| Report voice | **Second person**: "you" and the other person's name, e.g. "You and Alex…". |
| Run-to-run variation | **Fine for runs to differ.** We can experiment with determinism later. |
| Timezones | **Relative, per sender** (§6.1). Periods are inferred and suggested for the user to confirm, and nobody has to remember dates. The default is the Mac's timezone. |
| Languages | **English only** in v1. Mixed-language support is a later phase. |
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
    def ingest(self, store: Store) -> IngestReport: ...
        # idempotent (re-running never duplicates); incremental via an opaque
        # per-connector cursor: store.get_cursor(id) / store.set_cursor(id, ...)
```

Every connector writes into the same normalized schema (§5), and nothing downstream
knows which app a message came from except through the `source` field.

### 4.2 iMessage connector (v1)

- **Location:** `~/Library/Messages/chat.db` (override with `SINCEHELLO_IMESSAGE_DB`, e.g. a
  `chat.db` from an old Mac or `sms.db` from an iPhone backup). Snapshot it into a temp
  directory with SQLite's backup API (`sqlite3.Connection.backup`) and read only the
  snapshot. Messages writes heavily during an iCloud sync, so a plain copy of the db/`-wal`/
  `-shm` files can catch it mid-write. The live file is opened only read-only
  (`mode=ro`) as the backup source.
- **Permission:** the terminal or app needs Full Disk Access. `check()` detects the
  permission error and explains the fix.
- **Tables used:** `message`, `handle`, `chat`, `chat_message_join`, `chat_handle_join`.
- **Gotchas the connector must handle:**
  - `message.date` counts **nanoseconds since 2001-01-01 UTC** (the Apple epoch).
    Convert it and store **UTC only**. `chat.db` has no record of either sender's
    timezone, so local time is derived later from the timezone timeline (§6.1).
  - On newer macOS, `message.text` is often NULL and the text lives in the
    `attributedBody` blob (an NSAttributedString typedstream), so we need a decoder.
    This is the riskiest part of the connector and needs tests against real samples.
  - **Tapbacks** (`associated_message_type` 2000–2007 and 3000–3007; 2006 is an emoji
    reaction and 2007 a sticker tapback on newer OSes) are stored as
    messages like `Loved "…"`. Since v1 is text-only, store them flagged
    `is_reaction=1` and **exclude them from text analysis**, or "Loved" becomes
    the top word.
  - Each tapback also records **what it reacts to and how**. `reacts_to` is the target's
    `guid`, taken from `associated_message_guid` after stripping its `p:N/` or `bp:` prefix.
    `reaction_kind` maps 2000–2006 to love, like, dislike, laugh, emphasize, question,
    emoji (verify these against the real DB). The 3000–3006 codes *remove* an earlier tapback:
    don't emit the removal, and drop the reaction it cancels.
  - Edited and unsent messages (macOS 13+): keep the final text and set an `edited` flag.
  - **Group vs. 1:1:** `chat.style` 45 = 1:1 and 43 = group, verified on a real DB (every
    style-45 chat had exactly one other member; style-43 chats had 1–N, since a group can
    shrink). Unknown styles fall back to member count > 1.
  - Rows with `item_type != 0` (renames, member changes) are system events and are skipped.
  - **1:1 only (v1):** ingest every conversation, but analysis uses only conversations
    where `conversation.is_group = 0`. Group chats are stored for later use.
  - `is_from_me` gives the sender direction. In 1:1 chats the other sender comes from `handle_id`.
  - One person often has **several handles** (a phone number plus an Apple ID email).
    These get merged in identity resolution (§6).
  - Attachments-only messages (U+FFFC placeholder) are counted for stats but carry no text.
- **Contact names (planned, deferred from M1):** read the macOS AddressBook database
  (`~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb`, also
  behind Full Disk Access; snapshot it like `chat.db`) to map handles to names for the
  person picker and to feed automatic merges (§6). If that's unavailable, fall back to the
  raw handle. On the first real ingest, a top contact's phone and iCloud email showed up as
  two separate people in `sincehello people`; a shared contact card would have merged them.
- **Ids:** `source_msg_id` is `message.guid` and `source_conv_id` is `chat.guid`, never
  `ROWID`. iCloud sync can re-insert or merge rows under new ROWIDs, and dedupe has to
  survive that.
- **Incremental sync:** remember the max `ROWID` ingested and only pull newer rows next time.
  The ROWID is only a cursor; dedupe relies on the guid. ROWIDs are only comparable within one
  database, so the cursor is a JSON map of `{database path: max ROWID}`; ingesting an old
  backup after this Mac's `chat.db` reads all of it, and guids dedupe the overlap.

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
  sent_at_utc,
  text, is_reaction, has_attachment, edited,
  reacts_to_source_msg_id, reaction_kind,   -- reactions only; no FK, target may arrive later
  UNIQUE(source, source_msg_id)
)
message_fts  -- FTS5 virtual table over message.text (for the search tool)
tz_period(person_id, start_utc, end_utc NULL, iana_tz, source)  -- source: default | user | inferred_confirmed
tz_suggestion(person_id, kind, start_utc, end_utc NULL, utc_offset_hours, iana_tz, confidence,
              score, place, hint_message_id, hint_quote, details, status)
                                         -- inferred periods (§6.1); status: pending | confirmed | ignored
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
- The user's own handles are marked as `is_me` (`sincehello me <handle>` moves a handle to
  the `is_me` person, so it drops out of `sincehello people`).
- Phone normalization is a small built-in normalizer (`store/identity.py`), not
  libphonenumber: `+<country><number>` keeps its digits, and a bare 10-digit number (or 11
  digits starting with 1) is read as US/NANP (`+1`). Other bare numbers and short codes are
  never auto-merged.
- Automatic merges run after every ingest (`write_batch`). A manual merge moves a handle
  along with any handles that share its E.164 number, and deletes the person it leaves empty.
  A person's message count covers every 1:1 conversation with any of their handles.

### 6.1 Timezone timeline (per person)

Each person (you **and** them) gets their own timeline of timezone periods. Every
message is viewed in **the sender's local time**, and the other person's local time
is still available for cross-timezone insights.

**Zero-input by default.** Nobody is asked to remember where they were. Everyone
starts in the Mac's current timezone, and the tool only asks a question when it
detects something.

**Inference: suggest periods, then the user confirms them.** Recognizing a
suggestion is easier than recalling dates.
1. *Sleep-window detection (local):* for each person, compute the daily quiet window
   over rolling 14-day spans from a circular histogram of send hours. The midpoint
   of that window is roughly the middle of their night.
2. *Shift detection:* a midpoint shift of ≥ 3h that lasts ≥ 7 days becomes a candidate
   period, with offset ≈ shift. Shorter trips are ignored.
3. *Naming (agent):* the agent searches messages near each period's boundaries for
   place and travel mentions ("landed", "flight", city names) and attaches the best guess.
4. *Confirm:* a single prompt per candidate, e.g. "Looks like Alex was about 9h ahead
   from Jun 3 to Aug 20, 2022 (they mentioned 'Tokyo' on Jun 3). Correct / edit /
   ignore?" Periods the user ignores are dropped. Unconfirmed suggestions are used only
   if their confidence is high, and they are labeled as inferred in the report.

Known failure modes: shift work, exam all-nighters, and sparse data. Each of these
gives low confidence, and low-confidence suggestions are never used silently.

Implementation notes (M7):

- **Detection** (`sincehello/analysis/tz_infer.py`, no LLM, no wall clock). Send times are
  read on the default timezone's clock in half-hour bins; your timeline uses everything
  you sent in 1:1 chats, theirs what they sent. Each day's 14-day span gives a night: its
  quietest 5 hours, *strictly*. When one of you is far away, the other also goes quiet
  while they sleep, so a long-distance span has two quiet stretches; your own night is
  silent, theirs only sparse (a "good morning" waiting for them), and the strict minimum
  picks yours. The most common midpoint is the usual night. Each day's shift becomes an
  absolute UTC offset, so a stay somewhere without daylight saving doesn't split when the
  default zone changes clocks.
- **Shift detection.** A candidate is a run of ≥ 7 daily midpoints (the spec's "lasts ≥ 7
  days") ≥ 3h off, at an offset that stays within 3h, bridging up to 7 days of stray
  estimates. Boundaries move up to 10 days, to the split that leaves the fewest sends in
  the wrong night (6-hour nights): the earliest such start and the latest such end. The
  usual night is a weak witness while away (the person at home sleeps then too), but the
  hours that are night away and day at home are busy until the traveller leaves and from
  the moment they're back. The shift is then re-read from where the new night is silent
  over the period (the far edge of the silent stretch, less 30 min): with nights that
  overlap, the rolling midpoints understate it. Periods under 7 days or 3h are dropped,
  pieces of one stay that touch are merged, and a stretch that only echoes the other
  person's at the same offset is dropped unless both are well supported (a trip
  together).
- **Home.** If their usual night sits ≥ 5h from yours, they live elsewhere: a `home`
  suggestion covers their whole history (high confidence only at ≥ 7h, since a night
  owl can look like a timezone).
- **Confidence.** From days, messages per day, coverage (share of the period's days whose
  own estimate agreed) and silence (how much quieter the new night's hours got than
  usual, 1 = no sends): high needs ≥ 14 days, ≥ 3 messages/day, coverage ≥ 0.7, silence
  ≥ 0.8; low is < 1 message/day, coverage < 0.5 or silence < 0.5.
- **Suggestions** carry the UTC offset, a fixed-offset zone (`Etc/GMT-9` = UTC+9) until
  named, and a hint: the travel mention ("landed", "flight", "time difference", "i'm
  home", ...) nearest the start (else the end) within 3 days, the traveller's own first.
  `sync` stores them in `tz_suggestion` idempotently: a re-detected pending suggestion is
  updated in place, stale pending ones are removed, and confirmed or ignored ones are
  never asked again. Anything a `user` or `inferred_confirmed` period already covers is
  skipped.
- **Naming** (`sincehello/agent/tz_namer.py`, opt-in `sincehello tz infer --name`): one
  structured-output call per suggestion rather than an agent loop, since the search is
  fixed: the messages within 36h of each boundary (up to 30 each) plus travel mentions
  within 7 days, shown through `ToolContext.show` (egress tool `tz_namer`, written to
  `~/.sincehello/tz/`). It returns `{notes, place, iana_tz, evidence_message_id, quote}`;
  code keeps the zone only if it exists and its offset during the period is within 1.5h
  of the inferred one, and the evidence only if the quote is in a message it was shown.
  `prompts/tz_namer.md` is the cached prefix; costs go to a `Budget` under `--max-cost`
  (default $0.25). A named zone survives later re-detection.
- **Confirm** (`sincehello tz confirm <person>`): one prompt per pending suggestion,
  medium confidence and up (`--all` adds low), e.g. "Looks like Alex Chen was about 14h
  ahead of you from Jan 8, 2022 to Apr 7, 2022, UTC+09:00 (Alex Chen said “just landed in
  tokyo!! …” on Jan 9, 2022). Suggested zone: Asia/Tokyo (Tokyo). Confidence: high."
  Correct writes a `tz_period` with source `inferred_confirmed`; edit asks for the zone,
  start and end (default-zone clock) and writes source `user`; ignore marks it ignored.
- **Unconfirmed use.** `TzResolver` treats a pending high-confidence suggestion as a
  period with source `inferred` (priority `user` > `inferred_confirmed` > `inferred` >
  `default`). `analyze` runs `sync` first and says how many periods wait for review.
  `report.json` `timezones` and `get_overview` list each person's periods with their
  source, and the `cross_timezone` stat marks windows resting on one as `inferred`.
- **Recall** (`evals/tz_recall.py`, `sincehello eval tz`, free): on the committed
  bean-goose sample the Tokyo era is found at UTC+9, high confidence, start 0.2 days
  early and end 4 days late, with no other suggestion. Over 16 generated histories
  (bean-goose and noodle-summit, seeds 1–5, 3k–8k lines) every planted stretch is found
  with boundaries within 14 days (12 of 17 within 7, the end usually the looser one) and
  the offset within 1h; the 4-day Dubai trip is never suggested. One medium-confidence
  false positive: a two-week, 3h "shift" around noodle-summit's 58-hour fight silence,
  which moves the quiet window (seed 4, 6k lines). Known limit: a move of
  ~5h or less, whose night overlaps the other person's, isn't separable from 1:1 sends
  when the traveller rarely texts while the other sleeps; the midpoints shift by under
  3h and nothing is suggested.

**Cross-timezone insights only fire when a period differs by ≥ 3h for ≥ 4 weeks.**
For most pairs who live in the same place, this whole feature stays invisible. It
just keeps the late-night stats correct.

Examples of what it unlocks:
- "Alex stayed up past 2am their time 47 times to talk to you."
- "Your good-morning texts landed at 11pm for Alex during the Tokyo year."
- "Your overlap window: 7–10am for you, 9pm–midnight for them."
- "The reunion": the day both timelines converge again, and what you texted.
- "You texted from 4 timezones."

---

## 7. Analysis pipeline

### 7.1 Local stats engine (deterministic, no LLM)

These are computed over all messages with the chosen person. Exact numbers always
come from here, never from the LLM.

- Message totals per person, per year and month, and per weekday × hour heatmap
  (in each sender's local time)
- First message ever (and who sent it), plus "since hello" duration
- Busiest day ever, longest silence, longest daily streak
- **Who texts first**: the share of conversation-starts after a gap of ≥ N hours, for each person
- Reply-time distributions (median, by person, by era)
- Late-night share (00:00–04:00 in the **sender's** local time)
- Cross-timezone stats for periods where your timezones differ: overlap window,
  messages sent during the other person's night, "waiting for them to wake up" gaps
- Top words (stopwords removed), top bigrams and trigrams, top emoji, by person
- **Distinctive phrases**: words and n-grams that are frequent here but rare in a
  baseline. The baseline is the user's other conversations or a general English
  frequency list. Distinctive phrases are prime candidates for inside jokes.
- Message-length stats and the "double-texter" score

Implementation notes (`sincehello/analysis/`, M2):

- `sincehello stats <person> [--json]` prints every stat. Each result is
  `{query_id, stat, person_id, params, value, flags}`. `params` holds every parameter
  with defaults filled in (including `default_tz` for stats that depend on timezones), and
  `query_id` = `q_<stat>_<hash of stat, person, params, STATS_VERSION>`, so
  `recompute(store, result)` reproduces it exactly (§7.6). Bump `STATS_VERSION` when a
  definition changes.
- Every stat takes optional `start_utc` / `end_utc` (ms) windows, so era-level stats
  (e.g. reply time by era) reuse the same code once §7.2 exists.
- Parameter names are shared across stats where they mean the same thing: every
  "how many to return" parameter is `top_n` (`busiest_day`, `top_words`, `top_ngrams`,
  `top_emoji`, `distinctive_phrases`). An unknown parameter is an error that lists the
  ones the stat does take.
- Definitions, where the bullets above leave room:
  - Calendar-day stats (per year/month, busiest day, streak) count each message on its
    **sender's** local date, like time-of-day stats.
  - "Since hello" runs to `as_of_utc`, or to the last message when unset, so results never
    depend on the wall clock.
  - A conversation start is the first message, or any message ≥ `gap_hours` (default 6)
    after the previous one. A reply is a sender change < `max_reply_hours` (default 6)
    after the previous message. p90 uses the nearest-rank method.
  - Double text: a message sent ≥ 20 minutes after the same sender's previous message with
    no reply in between. Score = double texts / turns (a turn is a run of one sender's
    messages).
  - Words and n-grams use a bundled English stopword list; n-grams never cross sentence
    punctuation and skip grams made only of stopwords. Emoji are counted with
    text/emoji presentation (U+FE0F) merged.
  - Distinctive phrases: 1–3-grams that neither start nor end with a stopword, seen ≥ 3
    times, with a frequency ratio ≥ 5 versus my 1:1 chats with everyone else. With fewer
    than 500 baseline tokens, they fall back to a bundled common-English list (a phrase
    must contain a word outside it) and carry the `baseline_general_english` flag. A
    phrase is dropped when a longer kept phrase explains ≥ 80% of its uses.
  - Cross-timezone windows: runs of ≥ 28 days where the two people's UTC offsets (sampled
    daily at noon UTC) differ by ≥ 3h. Each reports the waking-hours overlap (08–23 local),
    messages sent during the recipient's night (23–07), the median wait for a reply to
    those night messages, the first message after it (`reunion_message_id`), each zone's
    `sources`, and `inferred` when one is an unconfirmed suggestion (flag
    `inferred_timezones`). `zones` counts each sender's messages by the zone they were
    sent from ("you texted from 4 timezones"). `STATS_VERSION` 3.
- Timezones resolve through `tz_period` plus pending high-confidence `tz_suggestion`s
  (source `inferred`, §6.1): when periods overlap, `user` beats `inferred_confirmed`
  beats `inferred` beats `default`, then the later start wins. With no period, the
  default is `SINCEHELLO_TZ`, else the machine's timezone, else UTC.

### 7.2 Era detection (local)

This splits the relationship into chapters without sending everything to Claude.

- Signals per week: message volume, reply-time median, late-night share,
  vocabulary drift (cosine distance between rolling word distributions), and the
  share of affectionate terms (from a small local lexicon).
- Algorithm: change-point detection, e.g. PELT via `ruptures` on the normalized
  signal vector, with a minimum era length (~6 weeks) and a maximum of ~8 eras.
- Output: `era(id, start, end, signal_summary)`. Naming and summarizing the eras is
  the agent's job: it calls `sample_era` and reads a sampled window from each.

Implementation notes (`sincehello/analysis/eras.py`, M2):

- `detect_eras(store, person_id, *, min_weeks=6, max_eras=8, default_tz=None)` returns
  `Era(id, start_utc, end_utc, message_count, signals)`; `sincehello eras <person> [--json]`
  prints them. Weeks run Monday 00:00 UTC to Monday; middle boundaries fall on week starts,
  the first era starts at the first message, and the last ends just after the last one.
- Weekly signals: log messages/week (partial first and last weeks scaled up), log reply-time
  median (same reply definition as §7.1), late-night share (00:00–03:59 sender-local),
  vocabulary drift (cosine distance between the week's content words and the previous four
  weeks'), affection share (messages with a term from the bundled `lexicon/affection.txt` or
  a heart emoji), the timezone gap between the two people (hours, from `tz_period`), and
  the weekly share of each of the 30 most frequent content words. The per-word shares
  matter: on synthetic histories topic shifts are what separate most eras, and adding them
  raised boundary recall from ~0.75 to ~0.9 at the same precision.
- Each signal is scaled by its week-to-week noise (mean absolute difference between
  consecutive weeks, floored at 10% of its spread), so clean steps like a timezone move
  stand out and noisy signals don't invent eras.
- Segmentation: the exact least-squares segmentation (the objective PELT optimizes) by
  dynamic programming, with every era ≥ `min_weeks` and at most `max_eras`; the era count
  minimizes cost + 0.7 · log(weeks) · (live signal dimensions) per extra era. We don't use
  `ruptures`: weekly data is small (ten years ≈ 520 weeks, ~1 s in pure Python), the exact
  DP enforces both limits directly, and it avoids a numpy/scipy dependency.
- Histories shorter than two minimum-length eras are one era.
- `signals` per era: `weeks`, `messages_per_week`, `volume_vs_overall`, `share_me`,
  `late_night_share`, `affection_share`, `tz_gap_hours` (median daily gap), plus
  `reply_median_s` when there are replies and `vocab_shift` (cosine distance from the
  previous era's words) from the second era on. All floats; naming is the agent's job.
- `sample_era(store, person_id, era, *, n=40, strategy, seed=0, default_tz=None)` returns
  message rows (plus a `sender` column) in time order: `random` (seeded uniform),
  `spike_days` (busiest sender-local days first, the densest run of up to n // 3 messages
  from each), `longest_threads` (threads split at 1h gaps, longest first, the opening
  n // 3 messages of each).
- Accuracy on synthetic histories (boundary within 14 days of a planted one): the committed
  bean-goose sample finds all 5 boundaries with no spurious ones, including the Tokyo
  stretch (start off by 1 day; the end is 6 days early because the last era is shorter
  than `min_weeks`). On 6 held-out histories (bean-goose and noodle-summit, other seeds,
  3k–10k messages): recall 0.90, precision 0.90 (measured before noodle-summit gained its
  San Francisco era in M7).

### 7.3 Candidate miners (local, cheap)

These give the agent leads to check. They don't decide anything.

- Firsts: the earliest match for curated pattern families such as "i love you / ily /
  love u", "goodnight / gn", "miss you", "babe / baby / ...", and "happy birthday".
- Pet-name candidates: short tokens that appear at message start or end, that one
  person uses a lot more than the other, and that aren't in a standard dictionary.
- Spikes: days with volume > 3σ above the mean (often big moments).
- Conflicts and makeups: long gaps or sharp drops in tone, followed by apology terms.
  These are included as insights (see §12).

Implementation notes (`sincehello/analysis/miners.py`, M2):

- `mine(store, person_id, kind=None, *, limit=20, default_tz=None)` returns
  `Candidate(kind, label, message_ids, score, details)` best first; `sincehello candidates
  <person> [--kind K] [--json]` prints them. `message_ids` are store `message.id` values.
  Scores share a rough 0–1 scale so a mixed list ranks sensibly.
- Firsts: one candidate per family with any match, `message_ids[0]` is the earliest
  match, followed by the other person's reply (if within 6h) and the other person's own
  first match. Each family is a list of phrases matched fuzzily (`miners.fuzzy_phrase`):
  every letter may repeat ("i loooove youuu", "ilyyy", "gnnn"), spaces may be any
  whitespace run, and apostrophes are optional, with word boundaries on both ends so
  near-misses like "love your", "lovely" or "i missed your call" don't count. Matching is
  case-insensitive `re.search` with curly apostrophes straightened; the compiled pattern is
  in `details.pattern` so a verifier can re-run it. Families: i love you (love you/u/ya,
  luv, lov, ily, ilysm, ilu, "in love with you", "i ❤️ you" / "i <3 u"; score 1.0), miss
  you (0.85), babe/baby/bae/honey/sweetheart/sweetie/darling (0.8), goodnight (good
  night/nite, gnight, gn, night night, nighty night; 0.7), happy birthday (bday, b-day,
  birfday, hbd; 0.7). Intensifiers need no rule: "i love you sooo much" and "i really love
  you" contain "love you". Details carry the matched text, the sender's local time, reply
  delay, and counts per sender.
- Pet names: tokens used in ≥ 5 messages, alphabetic, 2–12 letters, ≥ 50% of uses as a
  message's first or last word, a per-message rate ≥ 2x higher for one person, not a
  name of anyone in the store, not in `common_english.txt` or the chat-slang list
  (`lexicon/chat_slang.txt`, matched after squeezing repeated letters), and ≥ 3x more
  frequent here than in my other 1:1 chats. Known terms of endearment
  (`lexicon/pet_names.txt`) skip the dictionary checks and get a boost. Score grows with
  asymmetry, edge share, how many different messages and neighboring words it appears
  with (templates like "have you eaten" repeat), and use count; candidates below 0.25 are
  dropped.
- Spikes: sender-local days with ≥ 10 messages and z > 3 against the days within ±28 days
  (σ floored at 1). A local baseline keeps busy eras from flagging every day.
- Conflicts: a gap ≥ max(12h, 1.5 × p95 gap) followed within 6 messages / 6h by an
  apology, or (tone drop) a real apology preceded within 6h by ≥ 2 tense messages ("you
  said", "whatever", "i need space", "i'm so upset", "?!", ...). Apologies are tiered
  (heavy "i was wrong / i'm sorry about"; "i'm sorry / my bad"; bare "sorry"), and
  logistics ("sorry running late") don't count. The fight's start is the first tense
  message in the exchange before the silence. Overlapping candidates collapse to the
  best-scoring one, so a sarcastic "i said i'm sorry" mid-fight isn't its own conflict.
- On the bean-goose sample every targeted planted fact (first "i love you" with its
  traps, pet name "bean", moving day, the fight and makeup) is its kind's top candidate;
  same on 13 other generated histories. Precision: pet names and conflicts 1/1, spikes
  1/5 (ordinary busy days at z 3–5 alongside the planted z ≈ 18).

### 7.4 Agent tools (the only window Claude has into the messages)

| Tool | Returns | Notes |
|---|---|---|
| `get_overview()` | Person names, date range, totals, era list, top-level stats, and each stat's summary, parameters and defaults | Called first; a cheap orientation |
| `get_stats(name, start?, end?, params?)` | One stat block from §7.1; `params` is a list of `{name, value}` pairs (strict schemas allow only 24 optional parameters across all tools, so the name enum is every stat's parameters; a wrong one gets an error naming the right ones) | Numbers only, no message text |
| `search_messages(query, mode, sender?, start?, end?, order?, limit≤25)` | Matching messages (id, ts, sender, text) | `mode`: keyword (FTS5) or regex. An empty `query` matches everything, so with `start`/`end` it reads a time window in order |
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
- **Agenda:** orient with `get_overview`, then call `list_candidates` right away and
  treat the miner output as leads to verify (`get_context`, `first_occurrence`) rather
  than guessing search patterns. Read each notable era with `sample_era` before writing
  story or era insights. Then work through the seed categories and use the remaining
  time for "anything else delightful". Aim for breadth: one insight per storyline, with
  no near-duplicates (fold a second angle on the same moment into the existing insight).
  It ends with `finish`, or when it hits the turn cap or token budget. Why: in the first
  live runs (synthetic and real) the agent never called `list_candidates` or
  `sample_era`, found firsts with regex guesses that hold up poorly on noisy history,
  never read inside the eras, and proposed overlapping insights.
- **Stopping and guards:**
  - Max turns, e.g. 60.
  - Per-run token budget via `task_budget` (beta) so the agent paces itself, plus a
    **hard dollar cap** enforced in our own code from `response.usage`.
  - At 80% of budget, a mid-conversation system message tells it to wrap up.
- **Context management:** prompt caching on the system prompt and tools. Tool-result
  clearing (context editing) for old search results once insights have been proposed.
- **Refusals:** check `stop_reason == "refusal"` and enable server-side fallbacks.
  Intimate content can trip classifiers, so log refusals and continue with other categories.

Implementation notes (`sincehello/agent/`, M4):

- `tools.py` holds the §7.4 tools as plain functions over a `ToolContext` (`context.py`),
  with `strict: true` schemas in a fixed order. Tool errors, including failed
  `propose_insight` checks, return as `is_error` results with the reason. `sample_era` and
  `list_candidates` register only when `analysis/eras.py` / `analysis/miners.py` exist.
  `count` groups by sender, year or month; grouping by era waits on the `Era` object
  exposing its time range.
- Message ids are `source:source_msg_id`. Every message the tools return goes through
  one function that truncates text to 500 characters and writes the egress log. Ids that
  go out without text (inside stat results) are logged with `with_text: false` and don't
  count toward `messages_shared`.
- `loop.py` is the manual loop: streaming `client.beta.messages`, adaptive thinking
  (`display: "summarized"`), explicit `effort` (default `high`), `tool_choice` auto,
  `fallbacks: "default"`, a `task_budget` sized to run out at the 80% wrap-up point
  (min 20k tokens), cache breakpoints on the system prompt and the history.
  The history is append-only (thinking blocks are bound to their prefix): wrap-up and
  refusal notes are appended `role: "system"` messages. A refused turn's partial output is
  dropped; three refusals in a row end the run. Parallel tool calls run sequentially
  and are answered in one user message. Wrap-up fires at 80% of the budget or 3 turns
  before the cap.
- `task_budget` only paces the model; the dollar cap is the hard limit. The budget counts
  output and tool results, not the re-read history, which is mostly cheap cache reads, so
  one budget token costs well under the output rate. It's calibrated from live runs
  (`SPEND_PER_BUDGET_TOKEN`, about 0.62x the output price): pricing it at the full output
  rate gave a $1 cap a 30k budget, and the agent paced itself to stop near $0.37.
- `budget.py` prices `usage` per model (per attempt from `usage.iterations` after a
  fallback). Before each request it stops if `spent + max(last turn, average turn)`
  would cross `--max-cost`, so a run can only overshoot by one turn's misestimate.
- Run output: `~/.sincehello/runs/<run_id>/run_log.jsonl` (static request once, then
  each request's new messages, each full response with usage and cost, each tool
  call), `report.json` (§8.1, `deck` empty until curation), `egress.json`.
- `propose_insight` runs the §7.6 step 1 checks in `verify/checks.py`, then the M5
  critic (`ToolContext.critic`, a `tools.Critic`; the real one is `verify/critic.py`).

### 7.6 LOOP 2 — verification

Each proposed insight passes through:

1. **Deterministic checks**, run in `propose_insight` itself:
   - Every cited `message_id` exists and belongs to this person's conversations.
   - Quoted text appears verbatim in the cited message. Whitespace runs, curly vs.
     straight quotes, and literal `\uXXXX` escapes in the quote (the model sometimes
     writes non-ASCII that way) don't count as differences.
   - `first_occurrence` insights: re-run `first_occurrence` with the same pattern; the
     cited id must match. Other kinds aren't checked against `first`.
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

Implementation notes (`sincehello/verify/critic.py`, M5):

- **Hook:** `LLMCritic` is the `tools.Critic` the M4 hook expected, `(insight, ctx) ->
  Verdict`, unchanged. It is on by default in `analyze` and `eval agent`
  (`--no-critic`, `--critic-model`, `--critic-effort`). Three additions around it:
  `ToolContext` carries the run's `budget` and `log` (set by `run_agent`) so the critic
  can charge and record itself; a critic that can't give a verdict raises
  `CriticUnavailable`; and `propose_insight` takes an optional `revises` draft id.
- **Own context:** each call is a fresh one-message request. It never sees the agent's
  conversation, only: the people (`me`/`them` names), the card (title, headline, body,
  evidence, `first`, era), the metric with its query arguments and the value recomputed
  by code, every cited message with 5 messages each side (overlapping windows merged,
  cited ones marked), and on a resubmission its own notes from the previous round.
  Messages are read through `ToolContext.around` / `show`, the same path as
  `get_context`, so they are in the egress log under tool `critic`.
- **Output:** structured outputs (`output_config.format`, a strict JSON schema), not
  forced tool use, which Opus 5.5 rejects. Field order is `issues`, `scores`, `verdict`
  so the verdict is written after its reasons. Scores are integer enums 1–5 (strict
  schemas have no `minimum`/`maximum`) and are re-validated client-side; a `revise` or
  `reject` without issues counts as malformed.
- **Model and effort:** `claude-opus-5-5`, adaptive thinking (`display: "summarized"`,
  logged for calibration), effort **`medium`**: each call is a bounded reading task over
  ~10–40 short messages, not an open search, and a run makes ~20–40 of them, so medium
  buys careful reading of tone and attribution without high's thinking spend. It is the
  first knob to turn if calibration shows misses. `fallbacks: "default"` as in the loop.
- **Prompt and caching:** `verify/critic.md` is the whole system prompt and is
  byte-stable (~1.6k tokens, above the cache minimum): the role, what the input
  contains and what code already checked (so the critic judges meaning, not ids), a
  read-the-excerpts-first procedure, the six misreadings it must catch (sarcasm read as
  sincere; affection aimed at someone else, including "love you guys", quotes and
  "love your …"; wrong attribution of a line, joke or pet name; claims the excerpts
  don't support; numbers with no metric or a metric that measures something else; dates
  off the sender's local time), the §12 tone rules, verdict definitions, and anchored
  1–5 rubrics for accuracy, specificity, delight and tone. The cache breakpoint is on
  the system block; all per-insight text is in the user message after it.
- **Verdicts:** `pass` when every claim is supported (bland but accurate cards pass with
  low specificity/delight; curation ranks them down). `revise` for a fixable problem.
  `reject` only when the core claim is false or rests on a misreading no rewording can
  fix, or the card is mean. Never for being sensitive: fights, silences and hard
  stretches pass when accurate. On a resubmission the critic checks its previous issues
  and doesn't raise new minor ones.
- **Revise loop** (`tools.propose_insight`): the first critic round opens a draft
  (`draft_001`, …). A revise comes back as a tool error with the issues and the draft id;
  the agent resubmits with `revises=<id>`. A resubmission without `revises` that has the
  same kind and shares a cited message with an open or rejected draft counts as one too,
  so the cap can't be sidestepped. A third non-pass (after 2 revisions) becomes a reject,
  and the error says so. Rejected drafts are closed: resubmitting one is refused without
  a critic call. `verification.revisions` is the number of rounds before the pass.
- **No verdict:** a refusal from the whole fallback chain (no retry), malformed output
  twice (one retry), an API error, or no budget for another call raise
  `CriticUnavailable`. The insight is kept with `critic: "pending"` and the reason in
  `verification.critic_notes`, and the agent is told it was kept unverified. Dropping it
  would turn a classifier refusal on an intimate moment into a rejection for
  sensitivity; curation still keeps only `pass`.
- **Budget and logs:** critic responses are charged to the run's `Budget` as kind
  `critic`: they count toward `spent` and `--max-cost`, but not toward the agent's
  next-turn projection. Each call is guarded by its own estimate (the larger of the last
  and average critic call; $0.06 before the first). The run log has `critic_*` events
  (`critic_request_static` once, then `critic_request`, `critic_response` with cost and
  run total, `critic_verdict`, or the failure events); `report.json` `run` carries
  `critic_calls` and `critic_cost_usd`.
- **Calibration:** `evals/critic_calibration.py` has 11 hand-labelled insights on the
  bean-goose sample (correct, subtly wrong, bland), all passing step 1.
  `sincehello eval critic --max-cost 0.5` runs each once and reports agreement.

### 7.7 Curation

- Dedupe overlapping insights (e.g. "pet name: bub" vs. "you call each other bub").
- Score = critic delight × specificity × confidence. Make sure categories are spread
  out (§8).
- Order the deck like Wrapped: opener (since hello) → stats → language → milestones
  → eras story → closer.
- The target deck size is 12–20 cards (configurable).

Implementation notes (`sincehello/curate/rank.py`, M6):

- **Where it runs:** `RunRecorder.write_outputs` curates every report before writing
  `report.json`, errors and budget stops included. `sincehello curate <run_dir>
  [--deck-size N]` re-curates an existing run in place. It reads only `report.json`, so it
  is deterministic and offline: no LLM, no store, no wall clock. `CurationConfig` holds
  `min_cards` (12), `max_cards` (20), `min_score` (0.2), `spread` (0.75) and
  `category_cap` (0.4). `--deck-size N` (on `analyze` too) sets `max_cards = N` and
  `min_cards = min(12, N)`.
- **Eligible:** deterministic `pass` and critic `pass`. Critic `pending` with no
  `critic_notes` means no critic ever saw it (`--no-critic`): those go in, and
  `curation.verified` is false. `pending` with notes means the critic ran but couldn't
  give a verdict (§7.6): it stays out. The deck is never padded with unverified cards
  in a critic run.
- **Score:** `delight/5 × specificity/5 × accuracy/5`. Confidence is the critic's accuracy
  score. A missing score counts as 3, the rubric midpoint, so a run without a critic
  scores every card 0.216 and breaks ties by insight id.
- **Dedupe:** walk the insights best score first. A card is dropped as a duplicate of an
  already-kept one if it tells the same storyline: the same `first.pattern` (case and
  whitespace ignored), the same metric `query_id` and `path`, or a story card for the same
  `era_id`. Within one category it is also a duplicate if at least half of the smaller
  evidence set is shared, or if title-plus-headline content words have Jaccard ≥ 0.5.
- **Selection:** the milestone whose earliest cited message is the first message of the
  history ("since hello") is picked first when eligible. After that, pick greedily by
  `score × spread^(cards already picked from that category)`. No category may fill more
  than `ceil(category_cap × max_cards)` cards (at least 2). The cap relaxes only while the
  deck is under `min_cards`. Past `min_cards`, cards under `min_score` stop the deck
  (3×3×3 = 0.216 clears it, so bland-but-fine cards fill out a short deck). Never more
  than `max_cards`. If fewer cards are eligible than `min_cards`, the deck is all of them.
- **Order:** opener (the earliest-dated milestone, which is "since hello" when it is in)
  → rhythm/stats → language → free discoveries → the other milestones by date → story
  cards in era order (`report.eras`; unknown eras last) → closer (the best free
  discovery). Rhythm, language and free are each sorted best first. Dates come from
  `Insight.at_utc`, the earliest cited message's time, set when the report is written.
- **Record:** `report.curation` has `verified`, the sizes used, every eligible
  insight's score, and `dropped` (insight id → reason: critic verdict, duplicate of …,
  score under the floor, category full, deck full).

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
  "verification": { "deterministic": "pass", "critic": "pass", "revisions": 0, "critic_notes": [] }
}
```

Top-level `report.json` fields: `person`, `range`, `sources`, `deck[]` (ordered insight
ids, filled by curation, §7.7), `curation{verified, min_cards, max_cards, scores{},
dropped{}}`, `insights[]`, `eras[]`, `stats{}`, `summary` (the agent's `finish` text),
`timezones{me, them}` (each person's periods beyond the default, with `source`;
`inferred` = unconfirmed, §6.1), and
`run{run_id, status, cost_usd, tokens, turns, messages_shared, pct_shared, model, effort,
version, critic_calls, critic_cost_usd}` (critic spend is included in `cost_usd`). Pydantic models live in `sincehello/report/schema.py`.

Two additions to the insight, so its claims can be re-checked (§7.6): `metric.path` is a
dotted path into the query's value (`"share.them"`, `"top.0.date"`) and `metric.value`
is the value there; `first: {pattern, sender}` is required for `kind:
"first_occurrence"` and dropped on other kinds. `count` and `first_occurrence` return their own query ids
(`q_count_…`, `q_first_…`), recomputable within a run. Stats, counts and `rhythm`
insights must carry a metric. The pipeline also sets `at_utc`, the earliest cited message's time (ISO UTC),
so curation can date milestones from `report.json` alone.

---

## 9. Models and cost

Prices are from the current price list (per 1M tokens, input/output):
Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50. Cache reads cost much less
than fresh input.

| Role | Default model | Notes |
|---|---|---|
| Discovery agent | `claude-opus-5-5` | Effort tuned via evals |
| Critic | `claude-opus-5-5`, effort `medium` | Starts on the strongest model to set the quality bar. Later, eval `claude-sonnet-5-5`/`claude-haiku-5-5` as the critic, and switch only if agreement with the Opus critic and with human ratings holds |
| LLM judge (evals only) | `claude-opus-5-5`, effort `medium` | Streaming calls under a hard `--max-cost` for now; the Batch API (50% cheaper) is a later option (§10.2) |

**Critic call (estimate, not yet measured live):** a ~1.6k-token cached system prompt
(written once at $5/M, then read at $0.20/M), ~1–2k fresh input tokens (the card plus
11–40 messages), and ~0.7–1.5k output tokens at `medium` effort, thinking included.
That is about **$0.02–0.04 per call**, dominated by output, so ~30 calls add roughly
$0.6–1.2 to a run. Critic spend counts toward `--max-cost`.

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
- **Implementation (M3):** an offline, seeded generator builds the full history
  (timestamps, senders, planted facts, filler with per-person sleep and reply rhythms).
  The Claude generator is an optional second pass that rewrites only filler text era
  by era, with planted lines locked, so ground truth is identical either way and tests
  never need an API key. Scenarios are JSON in `evals/scenarios/`; each history is a
  directory with `messages.jsonl` and `manifest.json` (planted facts with exact message
  ids, dates, senders, counts). Both scenarios plant a long-distance era (bean-goose: 12
  weeks in Tokyo; noodle-summit: 7 weeks in San Francisco) and noodle-summit a 4-day trip
  too short to count; `sincehello eval tz` scores timezone inference against them (§6.1).
  Histories include group-chat and reaction traps (an
  earlier "love you" only in the group chat, reactions quoting planted phrases) and
  near-miss decoys. Format and definitions: `evals/README.md`.
- **Metrics:**
  - Recall of planted facts (did an insight capture it?)
  - Exactness: date, sender, and value correct
  - Hallucination rate: insights with claims that contradict the data
  - Cost and turns per run
- These are cheap, repeatable, and **never use your real messages**, so they can run in CI.

### 10.2 LLM-as-judge

- A rubric score from 1–5 on accuracy, specificity, delight, tone, and
  non-obviousness, run on every insight in a report, plus `keep` (would the reader want
  this card in their deck?), the judge's side of the human 👍/👎 (§10.3).
- The judge is a **separate prompt and rubric from the pipeline critic** (§7.6). That
  way the eval doesn't share the critic's blind spots and doesn't just grade the
  pipeline against itself.
- Pairwise mode for comparing two pipeline versions: "which deck is better?",
  with the order randomized.

Implementation notes (`evals/judge/`, M6):

- **Prompts:** `judge.md` (per insight, ~1.4k tokens) and `pairwise.md` are each the
  whole system prompt of their mode, byte-stable, with the cache breakpoint on the
  system block and everything per-item in the one user message. The rubric anchors every
  level of every dimension and ties `keep` to accuracy ≥ 4. `prompt_version()` (a hash of
  both) is recorded with results; `eval compare` warns when two runs were judged with
  different prompts.
- **Input:** the same evidence the critic reads (the card, every cited message with 5
  messages each side, `local_time` and `sender`), shown through `ToolContext.show` under
  egress tool `judge`, and the metric with the query behind it. It never sees the
  insight's id, the critic's verdict or its scores. After a run the query definitions
  come from `report.stats` (stats) and the run log's `tool` events (counts, firsts); an
  unrecorded query is shown as "unknown query". Eval runs on a synthetic sample (an
  `-eval-<sample>` run id) re-ingest the sample into a throwaway store; other runs read
  the local store.
- **Output:** structured outputs, `{notes, scores, keep}` (notes first) or `{reasons,
  winner}`, re-validated client-side; malformed output is retried once.
- **Pairwise:** cards only (title, headline, body, quotes), no excerpts. The deck is the
  curated `deck`, or before curation every critic-`pass` insight. A seeded or random
  coin picks which run is `deck_1`; the result records which was shown first.
- **Model and cost:** `claude-opus-5-5`, effort `medium`, adaptive thinking, fallbacks,
  like the critic; about the same per call (~$0.02–0.04). Calls go through the same
  `Budget` as the critic (one-shot calls guarded by their own estimate), so every
  paid eval command has a hard `--max-cost`. The Batch API (§9) would halve this but
  can't stop between calls at a cap; it's the next step if eval spend matters.
- **Run-level numbers** (`judge.json`): per-dimension means and their average
  (`overall`), `keep_rate`, and `hallucination_rate`: the share of cards the judge
  scored accuracy ≤ 2, the §10.1 metric for runs without planted ground truth.

### 10.3 Human ratings (real data, you)

- A small CLI rater shows each insight with its evidence and asks for 1–5 ratings
  plus 👍/👎 on "would I want to see this?"
- Ratings are stored locally. They're used to **calibrate the judge** (agreement
  rate between judge and human; refine the rubric until agreement is acceptable).

Implementation notes (M6):

- `sincehello eval rate <run>` shows each card (the deck in order, or every insight
  before curation or with `--all`) with its quotes, but not the critic's verdict or
  scores. It takes the five judge dimensions in one line (`5 4 4 5 3` or `54453`), then
  👍/👎 and an optional note. `s` skips, `q` quits; it saves after every card and resumes
  where it stopped (`--redo` asks again).
- Ratings go to `~/.sincehello/runs/<run>/ratings.json`, never the repo (`evals/ratings.py`).
- **Agreement** (`evals/judge/agreement.py`): per dimension, exact, within-one, mean
  absolute difference and bias (judge minus reference); `keep` agreement. Only pairs
  where both sides have a value count. `sincehello eval agreement <run>...` pools runs;
  no API calls.
- **Labelled calibration set** (`evals/judge_calibration.py`): the critic's 11 invented
  bean-goose cases plus an accurate-but-obvious card and a mean one, each labelled with
  `keep` and the scores a careful reader would give (only the dimensions that aren't
  debatable). `sincehello eval judge-calibration --max-cost 1` scores each once and
  reports agreement. Target, to refine the rubric against: `keep` agreement and
  within-one accuracy ≥ 0.8.

### 10.4 Iteration workflow

Change a prompt, tool, or effort setting → run the synthetic suite → compare against the
baseline on recall, hallucination rate, judge score, and cost → spot-check real data
with human rating → keep or revert.

In commands: `sincehello eval agent` (recall, `eval.json`) → `sincehello eval judge
<run>` (`judge.json`) → `sincehello eval compare <baseline> <candidate> [--pairwise]`,
which prints both runs side by side (model/effort, status, cost, turns, insights and
critic passes, recall, judge overall / keep / hallucination, human keep rate) and
optionally asks the pairwise judge (`pairwise-vs-<baseline>.json` in the candidate's
run dir). Every run's config and results stay in its run directory under
`~/.sincehello/runs/` (run config in `report.json` `run` and the run log's
`request_static`, the judge's model and prompt version in `judge.json`), not in the
repo, since real runs hold message excerpts.

---

## 11. Interface (v1 CLI)

```
sincehello connect                 # list connectors and their status
sincehello connect imessage        # check access, ingest
sincehello people                  # list people, by message count
sincehello stats <person> [--json] # local stats only (§7.1), no LLM
sincehello merge <person> <handle> # manual identity merge (person id or one of their handles)
sincehello me <handle>             # mark a handle as your own
sincehello analyze <person> [--max-cost 5] [--deck-size 20]
                                   # the "one click": sync all connected sources → analyze
                                   # (--model, --effort, --max-turns; no sync yet)
sincehello curate <run-dir> [--deck-size 20]  # re-curate a run's report.json (§7.7)
sincehello report <run-id>         # print report.md
sincehello eval [--suite synthetic]
sincehello eval agent --sample bean-goose   # M4 hook: agent recall of planted facts
sincehello eval critic [--max-cost 0.5]     # M5: critic agreement with labelled insights
                                   # analyze / eval agent run the critic by default;
                                   # --no-critic, --critic-model, --critic-effort
sincehello eval judge <run> [--max-cost 2]    # M6: LLM judge scores every insight
sincehello eval rate <run> [--all]          # M6: human rating pass (ratings.json)
sincehello eval agreement <run>...          # M6: judge vs. your ratings, no API calls
sincehello eval judge-calibration [--max-cost 1]  # M6: judge vs. labelled insights
sincehello eval compare <a> <b> [--pairwise]      # M6: two runs side by side
sincehello tz show <person>                 # M7: timezone periods and pending suggestions
sincehello tz infer <person> [--name --max-cost 0.25] [--json]
                                   # sleep-window shift detection (local); --name asks
                                   # Claude to name each place
sincehello tz confirm <person> [--all]      # correct / edit / ignore each suggestion
sincehello eval tz [--sample S | --scenario X --seed N]  # M7: recall of planted periods
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
  curate/          rank.py (dedupe, score, spread, order; §7.7)
  report/          schema.py (pydantic), render_md.py
  cli.py
evals/
  scenarios/       synthetic scenario specs with planted facts
  generator/       synthetic history generation
  judge/           judge.md, pairwise.md, judge.py, agreement.py
  judge_calibration.py, ratings.py, runs.py
tests/
web/               (later) TypeScript UI
```

Key libraries: `anthropic`, `pydantic`, `typer`, `sqlite3`/FTS5, `pytest` (era detection
is pure Python; see §7.2 for why not `ruptures`).

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
   Curation is done (§7.7): dedupe, score, category spread and Wrapped ordering fill
   `report.json` `deck` on every run; `sincehello curate` re-curates a run.
   *Eval side built:* judge (per insight and pairwise), `eval rate`, agreement against
   ratings and a labelled set, `eval compare`. *Still to do:* rate real runs and tune
   `judge.md` until agreement holds (§10.3).
7. **M7 — Timezone inference:** sleep-window shift detection, agent naming, confirm
   flow, cross-timezone insights. Synthetic scenarios get a planted long-distance era.
   *Done* (§6.1): `tz_infer` detection and `tz_suggestion` (schema v3), `sincehello tz
   infer / confirm / show`, opt-in naming with one Claude call per period, pending
   high-confidence periods used as `inferred` and labeled in `report.json`, the overview
   and `cross_timezone`; noodle-summit gained a San Francisco era and a short-trip trap,
   and `sincehello eval tz` scores recall. *Still to do:* run naming on real data, and
   decide whether moves of ≤ 5h need another signal (§6.1, known limit).
8. **M8 — Iterate:** tune prompts, effort, and tools against the evals.
9. **Later:** AddressBook contacts (names in `sincehello people`, merges for handles on the
   same contact card; §4.2), guided connectors (Instagram, Messenger, WhatsApp), TypeScript story-card UI,
   image export, bulk "deep mode", reactions and images, year-in-review mode,
   group chats, mixed-language support (stopwords, lexicons, CJK segmentation, trigram FTS).

---

## 15. Open questions

None blocking right now. New ones get added here as they come up during the build.
