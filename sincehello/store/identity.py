"""Identity resolution (SPEC.md §6): which handles belong to the same person.

Every new handle starts as its own person (`Store.upsert_handle`). This module merges them:

- automatically, when two handles are the same phone number after E.164 normalization;
- manually, when the user says a handle belongs to a person (`sincehello merge`);
- for the user's own handles, which move to the `is_me` person (`sincehello me`).

A person left with no handles after a merge is deleted.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from sincehello.store.store import ME_PERSON_ID, Store

# Numbers without a country code are read as this region's (North American Numbering Plan).
DEFAULT_COUNTRY_CODE = "1"
_PHONE_CHARS = set("0123456789+-(). ")


def normalize_phone(raw: str) -> str | None:
    """E.164 form of a phone handle, or None for emails, short codes, and anything unclear.

    Deliberately small instead of a full libphonenumber port: `+<country><number>` keeps its
    digits, and a bare 10-digit number (or 11 digits starting with 1) is read as a US/NANP
    number. Bare numbers of any other length are left alone rather than guessed at, since a
    wrong merge is worse than a missed one (SPEC.md §6).
    """
    value = raw.strip()
    if not value or not set(value) <= _PHONE_CHARS or value.count("+") > 1:
        return None
    if "+" in value and not value.startswith("+"):
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if value.startswith("+"):
        return f"+{digits}" if 8 <= len(digits) <= 15 and digits[0] != "0" else None
    if len(digits) == 10 and digits[0] not in "01":
        return f"+{DEFAULT_COUNTRY_CODE}{digits}"
    if len(digits) == 11 and digits.startswith(DEFAULT_COUNTRY_CODE) and digits[1] not in "01":
        return f"+{digits}"
    return None


@dataclass(frozen=True)
class Handle:
    id: int
    person_id: int
    source: str
    raw_value: str


class IdentityError(ValueError):
    pass


def find_handle(store: Store, ref: str) -> Handle:
    """Look up a handle by raw value (exact, then E.164-normalized) or by handle id."""
    rows = _handles(store.conn, "raw_value = ?", (ref,))
    if not rows and (e164 := normalize_phone(ref)):
        rows = [h for h in _handles(store.conn) if normalize_phone(h.raw_value) == e164]
    if not rows and ref.isdigit():
        rows = _handles(store.conn, "id = ?", (int(ref),))
    if not rows:
        raise IdentityError(f"no handle matches {ref!r}")
    if len({h.person_id for h in rows}) > 1:
        ids = ", ".join(f"{h.id} ({h.source})" for h in rows)
        raise IdentityError(f"{ref!r} matches several handles: {ids}. Pass a handle id instead.")
    return rows[0]


def find_person(store: Store, ref: str) -> int:
    """A person id, or the person a handle (raw value or handle id) belongs to.

    A number that is an existing person id is read as that person.
    """
    if ref.isdigit():
        row = store.conn.execute("SELECT id FROM person WHERE id = ?", (int(ref),)).fetchone()
        if row:
            return row["id"]
    try:
        return find_handle(store, ref).person_id
    except IdentityError:
        raise IdentityError(f"no person or handle matches {ref!r}") from None


def merge_handle(store: Store, person_id: int, handle_id: int) -> int | None:
    """Move a handle to a person. Returns the id of the person it left, or None if no move.

    Other handles with the same E.164 number move along, so the automatic merge can't
    split them back apart on the next ingest.
    """
    if not store.conn.execute("SELECT 1 FROM person WHERE id = ?", (person_id,)).fetchone():
        raise IdentityError(f"no person {person_id}")
    (handle,) = _handles(store.conn, "id = ?", (handle_id,)) or [None]
    if handle is None:
        raise IdentityError(f"no handle {handle_id}")
    if handle.person_id == person_id:
        return None
    moving = [handle.id]
    if e164 := normalize_phone(handle.raw_value):
        moving += [
            h.id
            for h in _handles(store.conn, "person_id = ?", (handle.person_id,))
            if h.id != handle.id and normalize_phone(h.raw_value) == e164
        ]
    with store.conn:
        _reassign(store.conn, person_id, moving)
    return handle.person_id


def mark_me(store: Store, handle_id: int) -> int | None:
    """Mark a handle as the user's own by moving it to the `is_me` person."""
    return merge_handle(store, ME_PERSON_ID, handle_id)


def auto_merge(store: Store) -> int:
    """Merge handles that share an E.164 phone number. Returns how many handles moved.

    The surviving person is the user's own if any of the handles is, else the oldest.
    """
    by_number: dict[str, list[Handle]] = defaultdict(list)
    for h in _handles(store.conn):
        if e164 := normalize_phone(h.raw_value):
            by_number[e164].append(h)
    me = {r["id"] for r in store.conn.execute("SELECT id FROM person WHERE is_me = 1")}
    moved = 0
    with store.conn:
        for group in by_number.values():
            people = {h.person_id for h in group}
            if len(people) < 2:
                continue
            target = min(people & me or people)
            ids = [h.id for h in group if h.person_id != target]
            _reassign(store.conn, target, ids)
            moved += len(ids)
    return moved


def _handles(conn: sqlite3.Connection, where: str = "1", params: tuple = ()) -> list[Handle]:
    rows = conn.execute(
        f"SELECT id, person_id, source, raw_value FROM handle WHERE {where} ORDER BY id", params
    )
    return [Handle(r["id"], r["person_id"], r["source"], r["raw_value"]) for r in rows]


def _reassign(conn: sqlite3.Connection, person_id: int, handle_ids: list[int]) -> None:
    """Point handles at a person and delete the people they leave empty. No commit."""
    marks = ",".join("?" * len(handle_ids))
    old = [
        r["person_id"]
        for r in conn.execute(
            f"SELECT DISTINCT person_id FROM handle WHERE id IN ({marks})", handle_ids
        )
    ]
    conn.execute(f"UPDATE handle SET person_id = ? WHERE id IN ({marks})", [person_id, *handle_ids])
    for pid in old:
        if (
            pid == person_id
            or conn.execute("SELECT 1 FROM handle WHERE person_id = ?", (pid,)).fetchone()
        ):
            continue
        row = conn.execute("SELECT display_name, is_me FROM person WHERE id = ?", (pid,)).fetchone()
        if row is None or row["is_me"]:
            continue
        if row["display_name"]:
            conn.execute(
                "UPDATE person SET display_name = ? WHERE id = ? AND display_name IS NULL",
                (row["display_name"], person_id),
            )
        conn.execute("UPDATE tz_period SET person_id = ? WHERE person_id = ?", (person_id, pid))
        conn.execute("UPDATE tz_suggestion SET person_id = ? WHERE person_id = ?", (person_id, pid))
        conn.execute("DELETE FROM person WHERE id = ?", (pid,))
