# Task: M1 — iMessage connector

This task must run **on the user's Mac**, because it reads their real iMessage database.

## Setup
- Start from `scaffold/m0`: `git fetch origin && git checkout scaffold/m0 && git checkout -b m1-imessage`.
  Push to `m1-imessage` and do not open a pull request.
- Read `SPEC.md` (especially §4.1, §4.2, §5, §6) and `CLAUDE.md` first.
- Run `uv sync`. Check that the terminal or app running this session has **Full Disk Access**
  (System Settings → Privacy & Security → Full Disk Access). If it doesn't, tell the user how
  to grant it and wait.

## Goal
Implement `sincehello/connectors/imessage.py`: a `Connector` (protocol in
`sincehello/connectors/base.py`) that reads `~/Library/Messages/chat.db` into the normalized
store, registered in `sincehello/connectors/registry.py`.

## Requirements (SPEC §4.2)
1. **Never open the live DB.** Copy `chat.db` plus its `-wal` and `-shm` files to a temp dir
   and read the copy read-only.
2. `check()` detects the missing-permission case and explains the fix. `setup_instructions()`
   gives the steps.
3. **Tables:** `message`, `handle`, `chat`, `chat_message_join`, `chat_handle_join`. Map each
   `chat` to a `ConversationRecord`. Group vs. 1:1: check the `chat.style` values (believed to
   be 43 = group, 45 = 1:1) and the member count. **Verify these against the real DB.**
4. **Timestamps:** `message.date` is nanoseconds since 2001-01-01 UTC (older DBs may use
   seconds, so detect by magnitude). Emit timezone-aware UTC.
5. **`attributedBody` decoder (the riskiest part, do it first):** on newer macOS
   `message.text` is often NULL, and the text lives in the `attributedBody` blob (an
   NSAttributedString typedstream). Write a pure-Python decoder in its own module, e.g.
   `sincehello/connectors/imessage_text.py`. Measure on the real DB what % of messages have
   NULL `text` but a non-null `attributedBody`, and what % decode successfully. Report those
   numbers.
6. **Test fixtures must be synthetic. Never commit real message content.** Generate typedstream
   blobs on the Mac from text you choose, e.g. a small Swift or PyObjC script using
   `NSArchiver.archivedData(withRootObject: NSAttributedString(...))`. Cover emoji, multi-line
   text, long text (length-prefix edge cases), and non-ASCII. Commit the generator script and
   the generated fixture bytes.
7. **Tapbacks** (`associated_message_type` 2000–2006, 3000–3006) become `is_reaction=True`.
   Edited/unsent messages set `edited`. Attachment-only messages (U+FFFC) set `has_attachment`
   and get `text=None`.
8. **Handles:** emit a `HandleRecord` per `handle.id`. A person often has several (phone plus
   email). Don't merge them here; identity resolution is a separate step.
9. **Incremental:** store the max ingested `message.ROWID` via
   `store.set_cursor("imessage", ...)` and only read newer rows next time. Re-running must add 0
   messages.
10. Contact names (AddressBook DB, SPEC §4.2) are a **stretch goal**. Skip them if the core
    takes the whole session.

## Privacy
Do not print message text to the terminal beyond what's needed for debugging. Never commit
`chat.db` copies, real text, or real phone numbers or emails. Use aggregate counts when
reporting results.

## Checks
Run `uv run ruff format . && uv run ruff check . && uv run pytest` before every push. Then run
`uv run sincehello connect imessage` and `uv run sincehello people`, and ask the user to
confirm the top contacts and rough counts look right. Don't paste the people list anywhere
outside this session.

## Done when
The connector ingests the real DB idempotently, the decoder has synthetic-fixture tests and a
reported decode rate, the user has confirmed `sincehello people` looks right, and everything is
pushed to `m1-imessage`. Finish with a summary: decode rate, `chat.style` findings, schema or
spec changes you'd propose, and anything deferred.
