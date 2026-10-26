"""Stats engine tests against hand-written fixtures with hand-computed answers."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime

import pytest
from conftest import FakeConnector, ScriptedConnector

from sincehello.analysis import STATS, compute, compute_all, recompute, resolve_params
from sincehello.analysis.tz import TzResolver
from sincehello.connectors import ConversationRecord, HandleRecord, MessageRecord

ALEX = "+15550001111"
SAM = "+15550003333"
HANDLES = [HandleRecord("fake", ALEX, "Alex"), HandleRecord("fake", SAM, "Sam")]
CONVS = [
    ConversationRecord("fake", "c-alex", False, (ALEX,)),
    ConversationRecord("fake", "c-sam", False, (SAM,)),
    ConversationRecord("fake", "c-group", True, (ALEX, SAM)),
]
_ids = itertools.count(1)


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def m(when: datetime, who: str, text: str, conv: str = "c-alex", **kw) -> MessageRecord:
    """who: "me", or the raw handle of the sender."""
    return MessageRecord(
        source="fake",
        source_msg_id=str(next(_ids)),
        source_conv_id=conv,
        is_from_me=who == "me",
        sent_at_utc=when,
        text=text,
        sender=None if who == "me" else who,
        **kw,
    )


def load(store, messages: list[MessageRecord]) -> int:
    """Ingest and return Alex's person id."""
    ScriptedConnector(messages, HANDLES, CONVS).ingest(store)
    return next(p.person_id for p in store.people() if p.display_name == "Alex")


def basic_messages() -> list[MessageRecord]:
    return [
        m(at(2021, 3, 1, 10, 0), ALEX, "hey is this sam's friend? \U0001f600"),  # Mon
        m(at(2021, 3, 1, 10, 2), "me", "yes! hi \U0001f600\U0001f600"),
        m(at(2021, 3, 1, 10, 5), ALEX, "pickle time"),
        m(at(2021, 3, 1, 10, 30), ALEX, "pickle time again"),  # double text (25 min)
        m(at(2021, 3, 2, 2, 0), "me", "cant sleep pickle time"),  # late night
        m(at(2021, 3, 2, 2, 1), ALEX, "go to sleep \u2764\ufe0f"),  # late night
        m(at(2021, 3, 2, 9, 0), "me", "morning"),
        m(at(2021, 3, 3, 20, 0), ALEX, "pickle time forever \u2764"),
        m(at(2021, 3, 5, 12, 0), "me", "hello?"),  # after the longest silence (40h)
        m(at(2021, 3, 5, 12, 10), ALEX, "hi"),
        # Excluded: a reaction, a group message, and another person's 1:1 chat.
        m(at(2021, 3, 5, 12, 11), "me", 'Loved "hi"', is_reaction=True),
        m(at(2021, 3, 5, 13, 0), ALEX, "group pickle pickle pickle", conv="c-group"),
        m(at(2021, 3, 5, 14, 0), SAM, "pickle", conv="c-sam"),
    ]


@pytest.fixture
def basic(store):
    return store, load(store, basic_messages())


def value(store, person_id, name, **params):
    if STATS[name].local_time:
        params.setdefault("default_tz", "UTC")
    return compute(store, person_id, name, params).value


# --- volume and rhythm -------------------------------------------------------


def test_totals(basic):
    v = value(*basic, "totals")
    assert v["total"] == 10
    assert v["by_sender"] == {"me": 4, "them": 6}
    assert v["share"] == {"me": 0.4, "them": 0.6}
    assert v["by_year"] == {"2021": {"me": 4, "them": 6}}
    assert v["by_month"] == {"2021-03": {"me": 4, "them": 6}}


def test_first_message_and_since_hello(basic):
    v = value(*basic, "first_message")
    assert v["sender"] == "them"
    assert v["sent_at_utc"] == "2021-03-01T10:00:00Z"
    assert v["as_of_utc"] == "2021-03-05T12:10:00Z"
    assert v["since_hello_days"] == 4
    assert v["since_hello"] == {"years": 0, "months": 0, "days": 4}
    as_of = int(at(2024, 4, 15).timestamp() * 1000)
    later = value(*basic, "first_message", as_of_utc=as_of)
    assert later["since_hello"] == {"years": 3, "months": 1, "days": 14}


def test_busiest_day_silence_streak(basic):
    b = value(*basic, "busiest_day")
    assert (b["date"], b["count"], b["by_sender"]) == ("2021-03-01", 4, {"me": 1, "them": 3})
    assert [d["date"] for d in b["top"]] == ["2021-03-01", "2021-03-02", "2021-03-05", "2021-03-03"]

    s = value(*basic, "longest_silence")
    assert (s["hours"], s["broken_by"]) == (40.0, "me")
    assert (s["start_utc"], s["end_utc"]) == ("2021-03-03T20:00:00Z", "2021-03-05T12:00:00Z")

    k = value(*basic, "longest_streak")
    assert k == {"days": 3, "start_date": "2021-03-01", "end_date": "2021-03-03", "active_days": 4}


def test_who_texts_first(basic):
    v = value(*basic, "who_texts_first")
    assert v == {"starts": 5, "by_sender": {"me": 3, "them": 2}, "share": {"me": 0.6, "them": 0.4}}
    v24 = value(*basic, "who_texts_first", gap_hours=24)
    assert v24["by_sender"] == {"me": 1, "them": 2}


def test_reply_times(basic):
    v = value(*basic, "reply_times")
    assert v["me"] == {"replies": 1, "median_seconds": 120.0, "p90_seconds": 120.0}
    assert v["them"] == {"replies": 3, "median_seconds": 180.0, "p90_seconds": 600.0}


def test_late_night(basic):
    v = value(*basic, "late_night")
    assert v["me"] == {"late": 1, "total": 4, "share": 0.25}
    assert v["them"] == {"late": 1, "total": 6, "share": 0.1667}
    assert v["overall"] == {"late": 2, "total": 10, "share": 0.2}


def test_heatmap(basic):
    v = value(*basic, "heatmap")
    assert v["grid"]["them"][0][10] == 3  # Monday 10:00
    assert sum(map(sum, v["grid"]["total"])) == 10
    assert v["peak"]["them"] == {"weekday": "Mon", "hour": 10, "count": 3}
    assert v["peak"]["me"] == {"weekday": "Mon", "hour": 10, "count": 1}  # tie: earliest


def test_message_length_and_double_texting(basic):
    v = value(*basic, "message_length")
    assert v["me"]["messages"] == 4
    assert v["me"]["mean_chars"] == 11.25  # 10 + 22 + 7 + 6
    assert v["me"]["median_chars"] == 8.5
    assert v["me"]["mean_words"] == 2.25
    assert v["me"]["longest"]["chars"] == 22

    d = value(*basic, "double_texting")
    assert d["them"] == {
        "messages": 6,
        "turns": 5,
        "messages_per_turn": 1.2,
        "double_texts": 1,
        "double_text_rate": 0.2,
    }
    assert d["me"]["double_texts"] == 0
    assert d["double_texter"] == "them"


# --- language ----------------------------------------------------------------


def test_top_words(basic):
    v = value(*basic, "top_words", k=4)
    assert v["them"] == [["pickle", 3], ["time", 3], ["forever", 1], ["friend", 1]]
    assert v["me"] == [["hello", 1], ["hi", 1], ["morning", 1], ["pickle", 1]]
    assert v["both"][:2] == [["pickle", 4], ["time", 4]]


def test_top_ngrams(basic):
    v = value(*basic, "top_ngrams")
    assert v["bigrams"]["them"] == [["pickle time", 3]]
    assert v["bigrams"]["me"] == []  # below min_count
    assert v["bigrams"]["both"] == [["pickle time", 4]]
    assert v["trigrams"]["both"] == []


def test_top_emoji_normalizes_presentation(basic):
    v = value(*basic, "top_emoji")
    assert v["them"] == [["\u2764\ufe0f", 2], ["\U0001f600", 1]]
    assert v["me"] == [["\U0001f600", 2]]


def test_distinctive_phrases_general_english_fallback(basic):
    r = compute(*basic, "distinctive_phrases")
    assert r.value["baseline"] == "general_english"
    assert "baseline_general_english" in r.flags
    # "pickle" alone is folded into "pickle time", which explains all its uses.
    assert r.value["phrases"] == [
        {
            "phrase": "pickle time",
            "n": 2,
            "count": 4,
            "by_sender": {"me": 1, "them": 3},
            "baseline_count": None,
            "ratio": None,
        }
    ]


def test_distinctive_phrases_against_other_conversations(store):
    sam_chat = [
        m(at(2020, 1, 1, 12, i), SAM, "what time is good for dinner tonight", "c-sam")
        for i in range(50)
    ]
    sam_chat += [
        m(at(2020, 1, 2, 12, i), "me", "maybe dinner at seven works", "c-sam") for i in range(30)
    ]
    person = load(store, basic_messages() + sam_chat)
    r = compute(store, person, "distinctive_phrases")
    assert r.value["baseline"] == "other_conversations"
    assert r.value["baseline_tokens"] == 50 * 7 + 30 * 5 + 1  # Sam's one "pickle" counts too
    assert r.flags == ()
    phrases = {p["phrase"]: p for p in r.value["phrases"]}
    assert "time" not in phrases  # common in the baseline
    assert phrases["pickle time"]["baseline_count"] == 0
    assert r.value["phrases"][0]["phrase"] == "pickle time"


# --- exclusions and empties --------------------------------------------------


def test_group_chats_and_reactions_excluded(store):
    FakeConnector().ingest(store)
    people = {p.handles[0]: p.person_id for p in store.people()}
    v = value(store, people[ALEX], "totals")
    assert v["by_sender"] == {"me": 1, "them": 2}  # the reaction and group message are out
    group_only = compute(store, people["+15550002222"], "totals", {"default_tz": "UTC"})
    assert group_only.value["total"] == 0
    assert "no_messages" in group_only.flags


def test_empty_conversation(store):
    person = load(store, [])
    results = compute_all(store, person, default_tz="UTC")
    assert set(results) == set(STATS)
    for r in results.values():
        assert "no_messages" in r.flags
        json.dumps(r.to_dict())
    assert results["totals"].value["total"] == 0
    assert results["first_message"].value["message_id"] is None
    assert results["longest_silence"].value["hours"] is None
    assert results["longest_streak"].value["days"] == 0
    assert results["reply_times"].value["me"]["median_seconds"] is None
    assert results["heatmap"].value["peak"]["them"] is None
    assert results["double_texting"].value["double_texter"] is None


def test_unknown_person(store):
    with pytest.raises(ValueError, match="unknown person"):
        compute(store, 999, "totals")
    with pytest.raises(ValueError, match="unknown person"):
        compute(store, 1, "totals")  # me


# --- timezones ---------------------------------------------------------------


def test_dst_transitions(store):
    person = load(
        store,
        [
            m(at(2021, 3, 14, 6, 30), ALEX, "a"),  # 01:30 EST, just before spring forward
            m(at(2021, 3, 14, 7, 30), "me", "b"),  # 03:30 EDT
            m(at(2021, 3, 14, 8, 30), "me", "c"),  # 04:30 EDT: not late night
            m(at(2021, 11, 7, 5, 30), ALEX, "d"),  # 01:30 EDT, before falling back
            m(at(2021, 11, 7, 6, 30), "me", "e"),  # 01:30 EST, an hour later in UTC
        ],
    )
    tz = {"default_tz": "America/New_York"}
    grid = compute(store, person, "heatmap", tz).value["grid"]
    assert grid["them"][6][1] == 2  # Sunday 01:00, both sides of the year
    assert grid["me"][6][1] == 1 and grid["me"][6][3] == 1 and grid["me"][6][4] == 1
    late = compute(store, person, "late_night", tz).value
    assert late["me"] == {"late": 2, "total": 3, "share": 0.6667}
    assert late["them"] == {"late": 2, "total": 2, "share": 1.0}
    assert compute(store, person, "busiest_day", tz).value["date"] == "2021-03-14"


def tokyo_fixture(store) -> int:
    person = load(
        store,
        [
            m(at(2022, 5, 20, 5, 0), ALEX, "a"),  # NY 01:00 Fri
            m(at(2022, 6, 10, 15, 0), ALEX, "b"),  # Tokyo 00:00 Sat
            m(at(2022, 6, 10, 15, 5), "me", "c"),  # NY 11:05 Fri; Tokyo 00:05 for Alex
            m(at(2022, 6, 10, 22, 5), ALEX, "d"),  # Tokyo 07:05 Sat, 7h later
            m(at(2022, 9, 10, 15, 0), ALEX, "e"),  # NY 11:00 Sat, back home
        ],
    )
    # A low-priority default period for the whole range; the user period wins.
    store.add_tz_period(person, at(2020, 1, 1), None, "America/New_York", "default")
    store.add_tz_period(person, at(2022, 6, 1), at(2022, 9, 1), "Asia/Tokyo", "user")
    return person


def test_sender_in_other_timezone_period(store):
    person = tokyo_fixture(store)
    tz = {"default_tz": "America/New_York"}
    late = compute(store, person, "late_night", tz).value
    assert late["them"] == {"late": 2, "total": 4, "share": 0.5}
    assert late["me"] == {"late": 0, "total": 1, "share": 0.0}
    grid = compute(store, person, "heatmap", tz).value["grid"]
    assert grid["them"][5][0] == 1 and grid["them"][5][7] == 1  # Sat 00h, 07h Tokyo
    assert grid["them"][4][1] == 1 and grid["them"][5][11] == 1  # NY before and after
    assert grid["me"][4][11] == 1


def test_resolver_priority_and_bounds(store):
    person = tokyo_fixture(store)
    tz = TzResolver(store, "UTC")
    ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731
    assert str(tz.zone(person, ms(at(2022, 6, 1)))) == "Asia/Tokyo"
    assert str(tz.zone(person, ms(at(2022, 9, 1)))) == "America/New_York"  # end exclusive
    assert str(tz.zone(person, ms(at(2019, 1, 1)))) == "UTC"  # before any period
    assert str(tz.zone(1, ms(at(2022, 6, 1)))) == "UTC"  # me: no periods


def test_cross_timezone(store):
    person = tokyo_fixture(store)
    r = compute(store, person, "cross_timezone", {"default_tz": "America/New_York"})
    assert r.flags == ()
    (w,) = r.value["windows"]
    assert (w["start_date"], w["end_date"], w["days"]) == ("2022-06-01", "2022-08-31", 92)
    assert (w["me_tz"], w["them_tz"], w["them_ahead_hours"]) == (
        "America/New_York",
        "Asia/Tokyo",
        13.0,
    )
    assert w["messages"] == 3
    assert w["overlap"] == [
        {"me": "08:00-10:00", "them": "21:00-23:00", "hours": 2},  # your morning
        {"me": "19:00-23:00", "them": "08:00-12:00", "hours": 4},  # their morning
    ]
    assert w["sent_during_recipient_night"] == {"me": 1, "them": 0}
    assert w["night_reply_wait_hours_median"] == {"me": 7.0, "them": None}


def test_cross_timezone_needs_four_weeks(store):
    person = load(store, [m(at(2022, 6, 1), ALEX, "a"), m(at(2022, 8, 1), "me", "b")])
    store.add_tz_period(person, at(2022, 6, 10), at(2022, 7, 1), "Asia/Tokyo", "user")  # 3 weeks
    r = compute(store, person, "cross_timezone", {"default_tz": "America/New_York"})
    assert r.value["windows"] == []
    assert "no_cross_timezone_periods" in r.flags


def test_default_tz_from_env(basic, monkeypatch):
    monkeypatch.setenv("SINCEHELLO_TZ", "Asia/Tokyo")
    r = compute(*basic, "late_night")
    assert r.params["default_tz"] == "Asia/Tokyo"
    # Tokyo is UTC+9: the 02:00 UTC messages land at 11:00, the 20:00 one at 05:00.
    assert r.value["overall"]["late"] == 0


# --- query ids and serialization ---------------------------------------------


def test_query_ids_are_stable_and_recomputable(basic):
    store, person = basic
    a = compute(store, person, "who_texts_first")
    b = compute(store, person, "who_texts_first", {"gap_hours": 6.0})
    c = compute(store, person, "who_texts_first", {"gap_hours": 12})
    assert a.query_id == b.query_id != c.query_id
    assert a.query_id.startswith("q_who_texts_first_")
    assert a.params == {"end_utc": None, "gap_hours": 6.0, "start_utc": None}
    again = recompute(store, json.loads(c.to_json()))
    assert again.value == c.value


def test_window_params_limit_messages(basic):
    store, person = basic
    end = int(at(2021, 3, 2).timestamp() * 1000)
    r = compute(store, person, "totals", {"end_utc": end, "default_tz": "UTC"})
    assert r.value["total"] == 4


def test_bad_params_rejected():
    with pytest.raises(ValueError, match="does not take"):
        resolve_params("totals", {"gap_hours": 3})
    with pytest.raises(ValueError, match="unknown stat"):
        resolve_params("nope")


def test_compute_all_shares_params(basic):
    results = compute_all(*basic, default_tz="UTC", gap_hours=24)
    assert results["who_texts_first"].params["gap_hours"] == 24
    assert "gap_hours" not in results["totals"].params
    assert "default_tz" not in results["reply_times"].params
    json.dumps({k: r.to_dict() for k, r in results.items()}, ensure_ascii=False)
