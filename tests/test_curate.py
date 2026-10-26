"""Curation (SPEC.md §7.7) on hand-written insights."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sincehello.cli import app
from sincehello.curate import CurationConfig, curate, eligible, score
from sincehello.report.schema import Insight, Report, RunInfo

FIRST_UTC = "2021-01-01T10:00:00Z"
_n = 0
WORDS = "apple banjo cactus dingo ember falcon gecko harbor igloo jasper kettle lemon mango".split()


def t(i: int) -> str:
    """A title sharing no words with t(j) for j != i, so word overlap never dedupes."""
    return f"{WORDS[i % 13]} {WORDS[i // 13]}{i}x"


def ins(category, title, *, msgs=None, critic="pass", notes=(), s=(4, 4, 4), **extra) -> Insight:
    """One insight. `s` is (delight, specificity, accuracy); `msgs` its cited ids."""
    global _n
    _n += 1
    iid = extra.pop("id", f"ins_{_n:03d}")
    kind = extra.pop("kind", "observation")
    delight, specificity, accuracy = s
    return Insight.model_validate(
        {
            "id": iid,
            "category": category,
            "kind": kind,
            "title": title,
            "headline": extra.pop("headline", title),
            "body": "",
            "evidence": [{"message_id": m, "quote": "x"} for m in (msgs or [f"t:{iid}"])],
            "metric": extra.pop("metric", None)
            or ({"query_id": f"q_{iid}"} if category == "rhythm" else None),
            "scores": {"delight": delight, "specificity": specificity, "accuracy": accuracy},
            "verification": {"deterministic": "pass", "critic": critic, "critic_notes": notes},
            **extra,
        }
    )


def report(insights, eras=()) -> Report:
    run = RunInfo(
        run_id="r",
        status="finished",
        cost_usd=0,
        tokens={},
        turns=1,
        messages_shared=0,
        pct_shared=0,
        model="m",
        effort="high",
        version="0",
    )
    return Report(
        person={"id": 1},
        range={"first_utc": FIRST_UTC, "last_utc": None},
        sources=["t"],
        deck=[],
        insights=list(insights),
        eras=[{"id": e} for e in eras],
        stats={},
        summary=None,
        run=run,
    )


def deck_of(rep: Report, **config) -> list[str]:
    return curate(rep, CurationConfig(**config) if config else None).deck


def test_score_is_delight_specificity_accuracy_with_neutral_fallback():
    assert score(ins("free", "card a", s=(5, 5, 5))) == 1.0
    assert score(ins("free", "card b", s=(5, 4, 2))) == pytest.approx(0.32)
    assert score(ins("free", "card c", s=(None, None, None))) == pytest.approx(0.216)


def test_only_critic_pass_or_never_reviewed_are_eligible():
    assert eligible(ins("free", "card a")) is None
    assert eligible(ins("free", "no critic", critic="pending")) is None  # --no-critic
    unavailable = ins("free", "card b", critic="pending", notes=["critic unavailable: refusal"])
    assert "unavailable" in eligible(unavailable)
    rep = curate(report([unavailable, ins("free", "card c", id="keep")]))
    assert rep.deck == ["keep"] and rep.curation.verified
    assert rep.curation.dropped[unavailable.id].startswith("critic pending")


def test_no_critic_run_still_builds_an_unverified_deck():
    items = [ins("language", t(i), critic="pending", s=(None,) * 3) for i in range(3)]
    rep = curate(report(items))
    assert sorted(rep.deck) == sorted(i.id for i in items)
    assert not rep.curation.verified


def test_dedupe_keeps_the_better_card():
    bub = ins("language", "Pet name: bub", msgs=["t:1", "t:2"], s=(5, 5, 5), id="bub")
    retold = ins("language", "You call each other bub", msgs=["t:2", "t:3"], id="retold")
    same_words = ins("language", "Pet name bub forever", msgs=["t:9"], s=(3, 3, 3), id="w")
    ily = {"pattern": "i love you", "sender": "any"}
    first_a = ins("milestone", "First ily", kind="first_occurrence", first=ily, id="fa")
    first_b = ins(
        "milestone",
        "Who said it first",
        kind="first_occurrence",
        first={"pattern": " I  Love You", "sender": "me"},
        s=(3, 3, 3),
        id="fb",
    )
    era_a = ins("story", "The 2am era", era_id="era_1", id="ea")
    era_b = ins("story", "Night owls", era_id="era_1", s=(3, 3, 3), id="eb")
    other = ins("language", "Goose incident", msgs=["t:4"], id="goose")
    rep = curate(report([retold, bub, same_words, first_a, first_b, era_a, era_b, other]))
    assert set(rep.deck) == {"bub", "fa", "ea", "goose"}
    d = rep.curation.dropped
    assert d == {
        "retold": "duplicate of bub",
        "w": "duplicate of bub",
        "fb": "duplicate of fa",
        "eb": "duplicate of ea",
    }


def test_same_metric_is_a_duplicate_across_categories():
    a = ins("rhythm", "Total messages", metric={"query_id": "q_total", "path": "n"}, id="a")
    b = ins("free", "So many texts", metric={"query_id": "q_total", "path": "n"}, s=(2, 2, 2))
    assert curate(report([a, b])).deck == ["a"]


def test_wrapped_order():
    eras = ["era_1", "era_2"]
    items = [
        ins("story", "Moving out west", era_id="era_2", id="s2"),
        ins("free", "Karaoke duets", s=(5, 5, 5), id="f1"),
        ins("free", "Weather reports", s=(3, 3, 3), id="f2"),
        ins("milestone", "First goodnight", at_utc="2021-03-01T00:00:00Z", id="m2"),
        ins("language", "Top emoji", s=(3, 4, 4), id="l2"),
        ins("rhythm", "Total", s=(4, 4, 4), id="r1"),
        ins("milestone", "Hello", at_utc=FIRST_UTC, s=(2, 2, 2), id="m1"),
        ins("language", "Pet names", s=(5, 4, 4), id="l1"),
        ins("story", "Dorm nights", era_id="era_1", id="s1"),
    ]
    assert curate(report(items, eras)).deck == [
        "m1",  # opener: since hello
        "r1",
        "l1",
        "l2",
        "f2",
        "m2",
        "s1",
        "s2",
        "f1",  # closer: best free discovery
    ]


def test_since_hello_is_always_picked():
    items = [ins("language", t(i), s=(5, 5, 5)) for i in range(5)]
    hello = ins("milestone", "Hi there", at_utc=FIRST_UTC, s=(1, 1, 1), id="hello")
    deck = deck_of(report([*items, hello]), min_cards=2, max_cards=3)
    assert deck[0] == "hello" and len(deck) == 3


def test_size_floor_and_cap():
    strong = [ins("language", t(i), s=(5, 5, 5)) for i in range(6)]
    weak = [ins("rhythm", t(20 + i), s=(2, 2, 2)) for i in range(6)]
    rep = curate(report(strong + weak), CurationConfig(min_cards=8, max_cards=10))
    # Weak cards fill up to min_cards, then the 0.2 floor stops the deck.
    assert len(rep.deck) == 8
    assert any("under" in r for r in rep.curation.dropped.values())
    # max_cards is a hard cap.
    big = [
        ins(c, t(10 * n + i))
        for n, c in enumerate(("language", "rhythm", "free", "story"))
        for i in range(8)
    ]
    rep = curate(report(big), CurationConfig(min_cards=4, max_cards=6))
    assert len(rep.deck) == 6 and "deck full" in rep.curation.dropped.values()


def test_categories_spread_out():
    lang = [ins("language", t(i), s=(5, 5, 5)) for i in range(10)]
    rhythm = [ins("rhythm", t(20 + i), s=(4, 4, 4)) for i in range(4)]
    milestone = [ins("milestone", t(40 + i), s=(4, 4, 3)) for i in range(4)]
    rep = curate(report(lang + rhythm + milestone), CurationConfig(min_cards=10, max_cards=10))
    by_id = {i.id: i.category for i in lang + rhythm + milestone}
    cats = [by_id[i] for i in rep.deck]
    assert cats.count("language") <= 4  # cap: 40% of 10
    assert cats.count("rhythm") >= 3 and cats.count("milestone") >= 2


def test_cap_relaxes_to_reach_min_cards():
    only = [ins("story", t(i), era_id=f"era_{i}") for i in range(10)]
    assert len(deck_of(report(only), min_cards=8, max_cards=10)) == 8


def test_curation_is_deterministic_and_idempotent():
    items = [
        ins(c, t(10 * n + i), s=(i % 5 + 1, 4, 4))
        for n, c in enumerate(("free", "language"))
        for i in range(9)
    ]
    rep = report(items)
    once = curate(rep)
    assert curate(rep) == once and curate(once) == once


def test_cli_curate_rewrites_report(tmp_path):
    items = [ins("language", "Pet names", id="l1"), ins("rhythm", "Total", id="r1")]
    (tmp_path / "report.json").write_text(report(items).model_dump_json())
    out = CliRunner().invoke(app, ["curate", str(tmp_path), "--deck-size", "5"])
    assert out.exit_code == 0, out.output
    assert "deck: 2 cards" in out.output
    saved = Report.model_validate_json((tmp_path / "report.json").read_text())
    assert saved.deck == ["r1", "l1"] and saved.curation.max_cards == 5
    missing = CliRunner().invoke(app, ["curate", str(tmp_path / "nope")])
    assert missing.exit_code == 1
