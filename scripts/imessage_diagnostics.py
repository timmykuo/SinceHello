"""Aggregate-only diagnostics for a chat.db: decode rate, chat.style, date range.

Prints counts and percentages only, never message text or handles, so the output is safe to
share. Run: uv run python scripts/imessage_diagnostics.py [path/to/chat.db]
"""

from __future__ import annotations

import sys
from collections import Counter

from sincehello.connectors.imessage import IMessageConnector, apple_time
from sincehello.connectors.imessage_text import DecodeError, decode_attributed_body


def pct(n: int, d: int) -> str:
    return f"{n:,} / {d:,} ({100 * n / d:.2f}%)" if d else f"{n:,} / 0"


def main() -> None:
    connector = IMessageConnector(sys.argv[1] if len(sys.argv) > 1 else None)
    status = connector.check()
    if status.state != "ready":
        sys.exit(f"{status.state}: {status.detail}")

    with connector.snapshot() as db:
        total = null_text = decoded = agree = both = 0
        failures: Counter[str] = Counter()
        for text, body in db.execute("SELECT text, attributedBody FROM message"):
            total += 1
            if body is None:
                continue
            try:
                out = decode_attributed_body(body)
            except DecodeError as e:
                out = None
                failures[str(e).split(":")[0]] += 1
            if text is None:
                null_text += 1
                decoded += out is not None
            else:
                both += 1
                agree += out == text

        print("== attributedBody ==")
        print(f"messages:                         {total:,}")
        print(f"text NULL, attributedBody set:    {pct(null_text, total)}")
        print(f"  ...decoded successfully:        {pct(decoded, null_text)}")
        print(f"text and body both set, agreeing: {pct(agree, both)}")
        for reason, n in failures.most_common():
            print(f"  failure: {reason}: {n:,}")
        (no_text,) = db.execute(
            "SELECT count(*) FROM message WHERE text IS NULL AND attributedBody IS NULL"
            " AND item_type = 0 AND cache_has_attachments = 0"
        ).fetchone()
        print(f"no text, no body, no attachment:  {pct(no_text, total)}")

        print("\n== chat.style vs member count ==")
        for style, members, n in db.execute(
            "SELECT style, members, count(*) FROM ("
            "  SELECT c.style, (SELECT count(*) FROM chat_handle_join j"
            "    WHERE j.chat_id = c.ROWID) AS members FROM chat c)"
            " GROUP BY style, min(members, 3) ORDER BY style, members"
        ):
            label = f"{members}" if members < 3 else "3+"
            print(f"style={style}  members={label:<3} chats={n:,}")
        for style, ident, n in db.execute(
            "SELECT style, CASE WHEN chat_identifier LIKE 'chat%' THEN 'chat…'"
            " WHEN chat_identifier LIKE '%@%' THEN 'email' ELSE 'phone/other' END, count(*)"
            " FROM chat GROUP BY 1, 2 ORDER BY 1, 2"
        ):
            print(f"style={style}  chat_identifier={ident:<12} chats={n:,}")

        print("\n== message types ==")
        for t, n in db.execute(
            "SELECT associated_message_type, count(*) FROM message GROUP BY 1 ORDER BY 1"
        ):
            print(f"associated_message_type={t}: {n:,}")
        for t, n in db.execute("SELECT item_type, count(*) FROM message GROUP BY 1 ORDER BY 1"):
            print(f"item_type={t}: {n:,}")

        print("\n== date range (sync progress) ==")
        lo, hi = db.execute("SELECT min(date), max(date) FROM message WHERE date > 0").fetchone()
        if lo is not None:
            print(f"earliest: {apple_time(lo):%Y-%m-%d}   latest: {apple_time(hi):%Y-%m-%d}")
        years: Counter[int] = Counter()
        for (d,) in db.execute("SELECT date FROM message WHERE date > 0"):
            years[apple_time(d).year] += 1
        for year in sorted(years):
            print(f"{year}: {years[year]:,}")


if __name__ == "__main__":
    main()
