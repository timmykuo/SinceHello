"""Candidate miners (SPEC.md §7.3) against the bean-goose planted facts and small fixtures."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ScriptedConnector

from sincehello.analysis.miners import FIRST_FAMILIES, KINDS, mine
from sincehello.connectors import ConversationRecord, HandleRecord, MessageRecord
from sincehello.connectors.synthetic import SAMPLE_DIR, SyntheticConnector
from sincehello.store import Store

TZ = "America/New_York"


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    manifest = json.loads((SAMPLE_DIR / "manifest.json").read_text())
    with Store(tmp_path_factory.mktemp("sample") / "s.db") as store:
        SyntheticConnector(SAMPLE_DIR).ingest(store)
        person = next(p.person_id for p in store.people() if p.display_name == "Alex Chen")
        ids = {
            r["source_msg_id"]: r["id"]
            for r in store.conn.execute("SELECT id, source_msg_id FROM message")
        }
        found = {k: mine(store, person, k, limit=100, default_tz=TZ) for k in KINDS}
        yield store, person, manifest, ids, found


def _fact(manifest, type_: str) -> dict:
    return next(f for f in manifest["facts"] if f["type"] == type_)


def test_first_i_love_you(sample):
    _, _, manifest, ids, found = sample
    fact = _fact(manifest, "first_phrase")
    c = next(c for c in found["first"] if c.details["family"] == "i love you")
    assert c.label == 'first "i love you"'
    assert c.message_ids[0] == ids[fact["message_id"]]
    assert c.details["sender"] == fact["sender"]
    assert c.details["sender_local_time"].startswith(fact["sender_local_time"])
    assert c.details["reply"]["message_id"] == ids[fact["reply_message_id"]]
    assert c.details["reply"]["sender"] == fact["reply_sender"]
    assert c.details["reply"]["delay_s"] == fact["reply_delay_s"]
    assert c.details["reply"]["matches"]
    assert c.details["count_by_sender"] == fact["count_by_sender"]
    assert c.score == 1.0


def test_first_traps(sample):
    """Group-chat 'love you', reactions quoting it, and 'love your' are never firsts."""
    _, _, manifest, ids, found = sample
    traps = _fact(manifest, "first_phrase")["traps"]
    decoys = _fact(manifest, "first_phrase")["decoy_message_ids"]
    cited = {i for k in KINDS for c in found[k] for i in c.message_ids}
    trap_ids = {
        ids[traps["earliest_group_match_id"]],
        *(ids[r] for r in traps["reaction_match_ids"]),
    }
    assert not cited & trap_ids
    assert not cited & {ids[d] for d in decoys}
    # The traps are earlier than the real first, so a miner that saw them would cite them.
    c = next(c for c in found["first"] if c.details["family"] == "i love you")
    assert ids[traps["earliest_group_match_id"]] < c.message_ids[0]


def test_pet_name_bean(sample):
    _, _, manifest, ids, found = sample
    fact = _fact(manifest, "pet_name")
    top = found["pet_name"][0]
    assert top.label == f'pet name "{fact["term"]}"'
    d = top.details
    assert d["first_message_id"] == ids[fact["first_message_id"]] == top.message_ids[0]
    assert d["first_sender"] == fact["first_sender"]
    assert d["main_user"] == "me"
    assert d["count_by_sender"] == fact["count_by_sender"]
    assert d["relationship_month"] == fact["relationship_month"]


def test_spike_moving_day(sample):
    _, _, manifest, _, found = sample
    fact = _fact(manifest, "busiest_day")
    top = found["spike"][0]
    assert top.details["date"] == fact["date"]
    assert top.details["count"] == fact["count"]
    assert top.details["is_busiest_day"]
    assert top.details["z"] > 3
    assert all(c.details["z"] > 3 for c in found["spike"])


def test_conflict_and_makeup(sample):
    _, _, manifest, ids, found = sample
    fact = _fact(manifest, "conflict_makeup")
    assert len(found["conflict"]) == 1  # "running 5 min late sorry" lines don't count
    d = found["conflict"][0].details
    assert d["apology_message_id"] == ids[fact["apology_message_id"]]
    assert d["apology_sender"] == fact["apology_sender"]
    assert d["apology_text"] == fact["apology_text"]
    assert d["last_before_silence_id"] == ids[fact["last_before_silence_id"]]
    assert d["fight_start_message_id"] == ids[fact["fight_start_message_id"]]
    assert d["fight_starter"] == fact["fight_starter"]
    assert d["silence_hours"] == fact["silence_hours"]
    assert d["is_longest_silence"] is fact["is_longest_silence"]


def test_recall_and_precision(sample):
    """Every planted fact a miner targets is its kind's top candidate.

    Precision (planted candidates / all candidates of the kind) on this sample: pet names
    1/1, conflicts 1/1, spikes 1/5 (the other four are ordinary busy days at z 3-5; the
    planted one is z ~18 and ranks first). Firsts are exact, not ranked.
    """
    _, _, manifest, ids, found = sample
    planted = {
        "pet_name": [c.details["term"] for c in found["pet_name"]],
        "spike": [c.details["date"] for c in found["spike"]],
        "conflict": [c.details["apology_message_id"] for c in found["conflict"]],
    }
    assert planted["pet_name"] == [_fact(manifest, "pet_name")["term"]]
    assert planted["conflict"] == [ids[_fact(manifest, "conflict_makeup")["apology_message_id"]]]
    assert planted["spike"][0] == _fact(manifest, "busiest_day")["date"]
    assert len(planted["spike"]) <= 5


def test_mine_all_kinds(sample):
    store, person, _, _, _ = sample
    top = mine(store, person, default_tz=TZ)
    assert len(top) <= 20
    assert {c.kind for c in top[:4]} == set(KINDS)
    assert [c.score for c in top] == sorted((c.score for c in top), reverse=True)
    assert len(mine(store, person, limit=3, default_tz=TZ)) == 3
    assert mine(store, person, default_tz=TZ) == top  # deterministic
    json.dumps([c.to_dict() for c in top])
    with pytest.raises(ValueError):
        mine(store, person, "nope")


# --- hand-written fixtures -----------------------------------------------------

ALEX, JO = "+15550001111", "+15550002222"
HANDLES = [HandleRecord("fake", ALEX, "Alex"), HandleRecord("fake", JO, "Jo")]
CONVS = [
    ConversationRecord("fake", "c-alex", False, (ALEX,)),
    ConversationRecord("fake", "c-jo", False, (JO,)),
    ConversationRecord("fake", "c-group", True, (ALEX, JO)),
]
T0 = datetime(2023, 1, 2, 15, tzinfo=UTC)


def _load(store, script: list[tuple[float, str, str] | tuple[float, str, str, str]]) -> int:
    """script: (hours after T0, "me" | "alex" | "jo", text[, conv])."""
    messages = []
    for i, (hours, who, text, *conv) in enumerate(script, 1):
        messages.append(
            MessageRecord(
                source="fake",
                source_msg_id=str(i),
                source_conv_id=conv[0] if conv else "c-alex",
                is_from_me=who == "me",
                sent_at_utc=T0 + timedelta(hours=hours),
                text=text,
                sender={"alex": ALEX, "jo": JO}.get(who),
            )
        )
    ScriptedConnector(messages, HANDLES, CONVS).ingest(store)
    return next(p.person_id for p in store.people() if p.display_name == "Alex")


def test_firsts_skip_group_chats_and_near_misses(store):
    person = _load(
        store,
        [
            (0, "alex", "love you all", "c-group"),
            (1, "me", "i love your dog"),
            (2, "alex", "LOVE U"),
            (2.01, "me", "love you too"),
            (3, "me", "gn!"),
        ],
    )
    firsts = {c.details["family"]: c for c in mine(store, person, "first", default_tz="UTC")}
    ily = firsts["i love you"]
    assert ily.details["text"] == "LOVE U" and ily.details["sender"] == "them"
    assert ily.details["count_by_sender"] == {"me": 1, "them": 1}
    assert ily.details["reply"]["delay_s"] == 36
    assert firsts["goodnight"].details["sender"] == "me"
    assert "miss you" not in firsts


@pytest.mark.parametrize(
    ("family", "text", "matches"),
    [
        ("i love you", "i love you sooooo much", True),
        ("i love you", "i loooove you", True),
        ("i love you", "I LOVE YOUUUU", True),
        ("i love you", "ilyyyy", True),
        ("i love you", "i luv uuu", True),
        ("i love you", "love  you", True),
        ("i love you", "i\u2019m so in love with you", True),
        ("i love you", "i \u2764\ufe0f you", True),
        ("i love you", "i <3 u", True),
        ("i love you", "ok i love your playlist taste", False),
        ("i love you", "i loved your dog", False),
        ("i love you", "lovely to see you", False),
        ("i love you", "Loved \u201ci love you\u201d", True),  # reactions are excluded upstream
        ("miss you", "missss youuu", True),
        ("miss you", "i missed your call", False),
        ("goodnight", "goooodnight!!", True),
        ("goodnight", "gnnn", True),
        ("goodnight", "good nite", True),
        ("goodnight", "oh my god night shift again", False),
        ("happy birthday", "happy bdayyyy", True),
        ("babe", "babyyyy", True),
        ("babe", "babel fish", False),
    ],
)
def test_first_families_are_fuzzy(family, text, matches):
    pattern = dict((name, p) for name, p, _ in FIRST_FAMILIES)[family]
    assert bool(re.search(pattern, text.replace("\u2019", "'"), re.IGNORECASE)) is matches


def test_stretched_phrase_counts_as_first(store):
    person = _load(
        store,
        [
            (0, "alex", "i loooove you"),
            (0.01, "me", "ilyyy"),
            (5, "me", "i love you"),
        ],
    )
    ily = next(
        c
        for c in mine(store, person, "first", default_tz="UTC")
        if c.label.endswith('"i love you"')
    )
    assert ily.details["text"] == "i loooove you"
    assert ily.details["match"] == "i loooove you"
    assert ily.details["count_by_sender"] == {"me": 2, "them": 1}
    assert ily.details["reply"]["matches"]


def test_conflict_needs_a_real_apology(store):
    def filler(hours):
        return [(h * 1.0, "me" if h % 2 else "alex", f"filler {h}") for h in hours]

    script = filler(range(40))
    script += [(60, "alex", "sorry running late")]  # after a 21h gap, but logistics
    script += filler(range(61, 100))
    script += [
        (100, "alex", "you said you would call"),
        (100.02, "me", "whatever"),
        (100.05, "me", "i'm so upset right now"),
        (100.5, "me", "i'm sorry. i shouldn't have said that"),  # tone drop, no long gap
        (100.55, "alex", "it's ok"),
    ]
    person = _load(store, script)
    conflicts = mine(store, person, "conflict", default_tz="UTC")
    assert len(conflicts) == 1
    d = conflicts[0].details
    assert d["trigger"] == "tone_drop"
    assert d["apology_text"].startswith("i'm sorry. i shouldn't")
    assert d["apology_reply"]["sender"] == "them"


def test_pet_name_excludes_names_and_dictionary_words(store):
    openers = ["night", "morning", "thanks", "see you soon", "good luck", "sure", "yes"]
    script = []
    for d in range(30):
        script += [
            (d * 24, "me", f"{openers[d % 7]} {d} pickle"),  # mine only, at the end
            (d * 24 + 0.1, "alex", f"jo says hi {d % 5}"),  # a name, not a pet name
            (d * 24 + 0.2, "me", f"{d % 3} more coffee"),  # a dictionary word
            (d * 24 + 0.3, "alex", "ok"),
        ]
    person = _load(store, script)
    terms = [c.details["term"] for c in mine(store, person, "pet_name", default_tz="UTC")]
    assert terms == ["pickle"]


def test_empty_person(store):
    person = _load(store, [(0, "alex", "group only", "c-group")])
    assert mine(store, person, default_tz="UTC") == []
