"""Plain-text rendering of stat results for the CLI."""

from __future__ import annotations

from typing import Any

from sincehello.analysis.stats import StatResult


def _pair(d: dict[str, Any], fmt: str = "{}") -> str:
    return " / ".join("-" if d.get(s) is None else fmt.format(d[s]) for s in ("me", "them"))


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _terms(items: list[list[Any]], n: int = 8) -> str:
    return ", ".join(f"{t} ({c})" for t, c in items[:n]) or "-"


def render(results: dict[str, StatResult], name: str) -> list[str]:
    v = {k: r.value for k, r in results.items()}
    lines = [f"Stats for {name}  (me / them)", ""]

    def add(label: str, text: str, key: str) -> None:
        lines.append(f"{label:<22} {text}  [{results[key].query_id}]")

    if "totals" in v:
        t = v["totals"]
        add("Messages", f"{t['total']}  ({_pair(t['by_sender'])})", "totals")
        years = ", ".join(f"{y}: {c['me'] + c['them']}" for y, c in t["by_year"].items())
        lines.append(f"{'  by year':<22} {years or '-'}")
    if "first_message" in v and v["first_message"]["message_id"]:
        f = v["first_message"]
        since = f["since_hello"]
        add(
            "First message",
            f"{f['sender']} at {f['sent_at_local']}; {since['years']}y {since['months']}m"
            f" {since['days']}d since hello",
            "first_message",
        )
    if "busiest_day" in v and v["busiest_day"]["date"]:
        b = v["busiest_day"]
        add("Busiest day", f"{b['date']} ({b['count']} messages)", "busiest_day")
    if "longest_silence" in v and v["longest_silence"]["days"] is not None:
        s = v["longest_silence"]
        add(
            "Longest silence",
            f"{s['days']:.1f} days, broken by {s['broken_by']}",
            "longest_silence",
        )
    if "longest_streak" in v and v["longest_streak"]["days"]:
        s = v["longest_streak"]
        add(
            "Longest streak",
            f"{s['days']} days ({s['start_date']} to {s['end_date']})",
            "longest_streak",
        )
    if "who_texts_first" in v:
        w = v["who_texts_first"]
        add(
            "Texts first",
            f"{_pair(w['share'], '{:.0%}')} of {w['starts']} starts",
            "who_texts_first",
        )
    if "reply_times" in v:
        r = v["reply_times"]
        med = " / ".join(_duration(r[s]["median_seconds"]) for s in ("me", "them"))
        p90 = " / ".join(_duration(r[s]["p90_seconds"]) for s in ("me", "them"))
        add("Reply time", f"median {med}, p90 {p90}", "reply_times")
    if "late_night" in v:
        n = v["late_night"]
        shares = {s: n[s]["share"] for s in ("me", "them")}
        add("Late night (0-4am)", _pair(shares, "{:.1%}"), "late_night")
    if "message_length" in v:
        m = v["message_length"]
        add(
            "Avg length (chars)",
            _pair({s: m[s]["mean_chars"] for s in ("me", "them")}, "{:.1f}"),
            "message_length",
        )
    if "double_texting" in v:
        d = v["double_texting"]
        rates = {s: d[s]["double_text_rate"] for s in ("me", "them")}
        add(
            "Double-text rate",
            f"{_pair(rates, '{:.2f}')} (texter: {d['double_texter'] or '-'})",
            "double_texting",
        )
    if "heatmap" in v:
        p = v["heatmap"]["peak"]
        peaks = {
            s: f"{p[s]['weekday']} {p[s]['hour']:02d}h" if p[s] else None for s in ("me", "them")
        }
        add("Peak hour", _pair(peaks), "heatmap")
    for key, label in (("top_words", "Top words"), ("top_emoji", "Top emoji")):
        if key in v:
            add(label, "", key)
            for s in ("me", "them"):
                lines.append(f"{'  ' + s:<22} {_terms(v[key][s])}")
    if "top_ngrams" in v:
        add("Top phrases", "", "top_ngrams")
        for s in ("me", "them"):
            grams = v["top_ngrams"]["bigrams"][s][:4] + v["top_ngrams"]["trigrams"][s][:4]
            lines.append(f"{'  ' + s:<22} {_terms(grams)}")
    if "distinctive_phrases" in v:
        d = v["distinctive_phrases"]
        phrases = [[p["phrase"], p["count"]] for p in d["phrases"]]
        add(f"Distinctive ({d['baseline']})", _terms(phrases, 10), "distinctive_phrases")
    if "cross_timezone" in v:
        windows = v["cross_timezone"]["windows"]
        if not windows:
            add("Cross-timezone", "no stretches apart", "cross_timezone")
        for w in windows:
            add(
                "Cross-timezone",
                f"{w['start_date']} to {w['end_date']}: them {w['them_tz']}"
                f" ({w['them_ahead_hours']:+g}h){' inferred' if w['inferred'] else ''}",
                "cross_timezone",
            )
    return lines
