"""Offline, deterministic history generator (SPEC.md §10.1). No API calls.

The generator works in stages, each with its own seeded RNG so changing one stage does
not reshuffle the others:

1. Planted blocks: short scripted exchanges for each fact, placed at exact times.
   Each block gets a blackout window so filler never crowds it.
2. Filler 1:1 sessions, day by day. Each person texts only while awake in their own
   local timezone; replies follow the replier's reply-time distribution and wait for
   them to wake up. Volume is calibrated to ``target_messages``.
3. Group-chat filler and the group-chat traps from the scenario.
4. Injections into filler: pet-name uses, inside-joke callbacks, later "love you"s.
5. Flags (attachment-only, edited) and iMessage-style reactions.
6. Timestamps are made unique, ids assigned in time order, and the manifest is
   computed from the final messages and validated against the scenario.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from evals.generator import text
from evals.generator.clock import Clock
from evals.generator.scenario import Era, Scenario

FILLER = "filler"
BLACKOUT_MARGIN = timedelta(minutes=45)
SESSION_SIZE = 8.0  # minimum typical messages per session; grows with volume
MAX_REPLY = timedelta(hours=4)

# Relative chance of starting a conversation in each local hour (if awake).
HOUR_WEIGHT = [
    0.8,
    0.6,
    0.4,
    0.3,
    0.2,
    0.2,
    0.3,
    0.6,
    0.7,
    0.6,
    0.5,
    0.5,
    0.8,
    0.7,
    0.5,
    0.5,
    0.6,
    0.8,
    0.9,
    1.0,
    1.1,
    1.2,
    1.2,
    1.0,
]


@dataclass
class Msg:
    ts: datetime
    conv: str  # "dm" or "group"
    sender: str  # "me", "them", or a group friend key
    text: str | None
    fact: str | None = None  # planted fact id, None for filler
    role: str | None = None  # role inside the fact, e.g. "key", "reply", "callback"
    kind: str = ""  # filler kind: opener, closer_late, closer_day, body
    is_reaction: bool = False
    reacts_to: Msg | None = None
    has_attachment: bool = False
    edited: bool = False
    id: str = ""

    @property
    def is_filler(self) -> bool:
        return self.fact is None and not self.is_reaction

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conv": self.conv,
            "ts": self.ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sender": self.sender,
            "text": self.text,
            "is_reaction": self.is_reaction,
            "reacts_to": self.reacts_to.id if self.reacts_to else None,
            "has_attachment": self.has_attachment,
            "edited": self.edited,
            "fact": self.fact,
            "role": self.role,
        }


@dataclass
class Plan:
    """Planted blocks plus the windows where filler must stay out of the 1:1 chat."""

    msgs: list[Msg] = field(default_factory=list)
    blackouts: list[tuple[datetime, datetime, str]] = field(default_factory=list)
    first_ts: datetime | None = None

    def block(self, msgs: list[Msg], until: datetime | None = None, blackout: bool = True) -> None:
        self.msgs.extend(msgs)
        if blackout and msgs[0].conv == "dm":
            end = until or msgs[-1].ts
            self.blackouts.append(
                (msgs[0].ts - BLACKOUT_MARGIN, end + BLACKOUT_MARGIN, msgs[0].fact or "")
            )

    def blocked(self, ts: datetime) -> bool:
        return any(a <= ts <= b for a, b, _ in self.blackouts)


def _rng(seed: int, stage: str) -> random.Random:
    return random.Random(f"{seed}:{stage}")


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def lognormal(rng: random.Random, median: float, sigma: float) -> float:
    return median * math.exp(rng.gauss(0.0, sigma))


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def other(who: str) -> str:
    return "them" if who == "me" else "me"


class Generator:
    def __init__(self, scenario: Scenario, seed: int, target_messages: int | None = None):
        self.sc = scenario
        self.seed = seed
        self.target = target_messages or scenario.target_messages
        self.clock = Clock(scenario)
        self.end_day = scenario.start + timedelta(days=scenario.duration_days)
        self.t_end = self.clock.local_midnight_utc("me", self.end_day)
        text.check_corpus(scenario.reserved_patterns())

    # --- helpers ------------------------------------------------------------

    def era_for(self, day: date) -> Era:
        current = self.sc.eras[0]
        for e in self.sc.eras:
            if e.start <= day:
                current = e
        return current

    def resolve_at(self, at: str | dict[str, Any], sender: str, rng: random.Random) -> datetime:
        if isinstance(at, str):
            return self.clock.to_utc(sender, datetime.fromisoformat(at))
        first_day = add_months(self.sc.start, int(at["month"]) - 1)
        lo, hi = at.get("day_range", [1, 1])
        day = first_day + timedelta(days=rng.randint(lo, hi) - 1)
        return self.clock.to_utc(sender, datetime.combine(day, time.fromisoformat(at["time"])))

    def script(
        self,
        fact_id: str,
        start: datetime,
        lines: list[list[Any]],
        rng: random.Random,
        conv: str = "dm",
        roles: dict[int, str] | None = None,
    ) -> list[Msg]:
        out, t = [], start.replace(microsecond=0)
        for i, line in enumerate(lines):
            sender, body = line[0], line[1]
            if i:
                t += timedelta(seconds=line[2] if len(line) > 2 else rng.randint(20, 180))
            role = (roles or {}).get(i, "context")
            out.append(Msg(t, conv, sender, body, fact=fact_id, role=role))
        return out

    # --- stage 1: planted blocks ---------------------------------------------

    def plan(self) -> Plan:
        rng = _rng(self.seed, "plan")
        plan = Plan()
        for f in self.sc.facts:
            handler = getattr(self, f"_plan_{f['type']}", None)
            if handler:
                handler(f, plan, rng)
        for g in self.sc.group.script:
            sender = g["script"][0][0]
            start = self.resolve_at(g["at"], sender, rng)
            plan.block(self.script("group_trap", start, g["script"], rng, conv="group"))
        spans = sorted(plan.blackouts)
        for (_, a1, fa), (b0, _, fb) in zip(spans, spans[1:], strict=False):
            if b0 < a1:
                raise ValueError(f"planted facts {fa!r} and {fb!r} are too close together")
        return plan

    def _key_line(self, lines: list[list[Any]], pattern: str) -> int:
        rx = re.compile(pattern, re.IGNORECASE)
        for i, line in enumerate(lines):
            if rx.search(line[1]):
                return i
        raise ValueError(f"no script line matches {pattern!r}")

    def _plan_first_message(self, f: dict[str, Any], plan: Plan, rng: random.Random) -> None:
        start = self.resolve_at(f["at"], f["script"][0][0], rng)
        msgs = self.script(f["id"], start, f["script"], rng, roles={0: "key"})
        plan.block(msgs)
        plan.first_ts = start

    def _plan_first_phrase(self, f: dict[str, Any], plan: Plan, rng: random.Random) -> None:
        key = self._key_line(f["script"], f["pattern"])
        start = self.resolve_at(f["at"], f["script"][0][0], rng)
        roles = {key: "key", key + 1: "reply"} if key + 1 < len(f["script"]) else {key: "key"}
        msgs = self.script(f["id"], start, f["script"], rng, roles=roles)
        for r in f.get("reactions", []):
            target = msgs[r["line"]]
            msgs.append(
                Msg(
                    target.ts + timedelta(seconds=r["delay_s"]),
                    "dm",
                    r["sender"],
                    f"{r['verb']} “{target.text}”",
                    fact=f["id"],
                    role="reaction",
                    is_reaction=True,
                    reacts_to=target,
                )
            )
        plan.block(msgs, until=max(m.ts for m in msgs))
        for d in f.get("decoys", []):
            dstart = self.resolve_at(d["at"], d["script"][0][0], rng)
            plan.block(self.script(f["id"], dstart, d["script"], rng, roles={0: "decoy"}))

    def _plan_pet_name(self, f: dict[str, Any], plan: Plan, rng: random.Random) -> None:
        first = f["first"]
        start = self.resolve_at(first["at"], first["script"][0][0], rng)
        plan.block(self.script(f["id"], start, first["script"], rng, roles={0: "key"}))

    def _plan_inside_joke(self, f: dict[str, Any], plan: Plan, rng: random.Random) -> None:
        key = self._key_line(f["script"], f["pattern"])
        start = self.resolve_at(f["at"], f["script"][0][0], rng)
        roles = {0: "origin_context", key: "key"}
        plan.block(self.script(f["id"], start, f["script"], rng, roles=roles))

    def _plan_busiest_day(self, f: dict[str, Any], plan: Plan, rng: random.Random) -> None:
        day = date.fromisoformat(f["date"])
        lo = self.clock.to_utc("me", datetime.combine(day, time.fromisoformat(f["window"][0])))
        hi = self.clock.to_utc("me", datetime.combine(day, time.fromisoformat(f["window"][1])))
        mean_daily = self.target / self.sc.duration_days
        n = int(min(f.get("max_messages", 10**6), max(f["min_messages"], f["factor"] * mean_daily)))
        msgs = self.script(f["id"], lo, f["script"], rng, roles={0: "key"})
        pool = text.TOPICS[f["topic"]]
        script_end = msgs[-1].ts
        span = (hi - script_end).total_seconds()
        times = sorted(rng.uniform(60, span) for _ in range(n - len(msgs)))
        who = "them"
        for s in times:
            if rng.random() > self.sc.personas[who].style.double_text:
                who = other(who)
            body = rng.choice(pool) if rng.random() < 0.7 else rng.choice(text.SHORTS)
            body = text.styled(text.fill(body, rng), self.sc.personas[who], rng)
            msgs.append(
                Msg(
                    script_end + timedelta(seconds=int(s)),
                    "dm",
                    who,
                    body,
                    fact=f["id"],
                    role="burst",
                )
            )
        plan.block(msgs, blackout=False)
        day_start = self.clock.local_midnight_utc("me", day)
        day_end = self.clock.local_midnight_utc("me", day + timedelta(days=1))
        plan.blackouts.append((day_start, day_end, f["id"]))

    def _plan_conflict_makeup(self, f: dict[str, Any], plan: Plan, rng: random.Random) -> None:
        start = self.resolve_at(f["at"], f["script"][0][0], rng)
        fight = self.script(f["id"], start, f["script"], rng, roles={0: "key"})
        fight[-1].role = "last_before_silence"
        apology_ts = fight[-1].ts + timedelta(hours=f["silence_hours"])
        makeup = self.script(f["id"], apology_ts, f["makeup"], rng, roles={0: "apology"})
        plan.block(fight + makeup)

    def _plan_long_distance(self, f: dict[str, Any], plan: Plan, rng: random.Random) -> None:
        periods = self.sc.timezones[f["person"]]
        i = int(f["period"])
        if not 0 < i < len(periods) - 1:
            raise ValueError("long_distance.period must be an inner timezone period")
        for part, boundary in (
            ("landed", periods[i].start_utc),
            ("reunion", periods[i + 1].start_utc),
        ):
            spec = f[part]
            sender = spec["script"][0][0]
            start = boundary + timedelta(hours=spec["offset_h"])
            start = self.clock.awake_at_or_after(sender, start)
            plan.block(self.script(f["id"], start, spec["script"], rng, roles={0: part}))

    # --- stage 2: 1:1 filler -------------------------------------------------

    def pick_start(
        self, rng: random.Random, who: str, lo: datetime, hi: datetime
    ) -> datetime | None:
        span = (hi - lo).total_seconds()
        for _ in range(60):
            t = lo + timedelta(seconds=rng.uniform(0, span))
            if self.clock.asleep(who, t):
                continue
            w = HOUR_WEIGHT[self.clock.local(who, t).hour]
            if self.clock.asleep(other(who), t):
                w *= 0.3
            if rng.random() < w / 1.2:
                return t.replace(microsecond=0)
        return None

    def filler_text(
        self, rng: random.Random, who: str, ts: datetime, era: Era, first: bool, last: bool
    ) -> tuple[str, str]:
        hour = self.clock.local(who, ts).hour
        bucket = text.time_bucket(hour)
        if first:
            kind, body = "opener", rng.choice(text.OPENERS[bucket])
        elif last and bucket == "late" and rng.random() < 0.75:
            kind, body = "closer_late", rng.choice(text.CLOSERS["late"])
        elif last and rng.random() < 0.25:
            kind, body = "closer_day", rng.choice(text.CLOSERS["day"])
        elif rng.random() < 0.3:
            kind, body = "body", rng.choice(text.SHORTS)
        else:
            kind, body = "body", rng.choice(text.TOPICS[rng.choice(era.topics)])
        return kind, text.styled(text.fill(body, rng), self.sc.personas[who], rng)

    def session(
        self,
        rng: random.Random,
        start: datetime,
        who: str,
        mean: float,
        era: Era,
        pace: float = 1.0,
    ) -> list[Msg]:
        """One back-and-forth. ``pace`` < 1 shortens replies on very chatty days."""
        n = max(1, int(rng.gammavariate(2.0, mean / 2.0) + 0.5))
        msgs: list[Msg] = []
        t = start
        while len(msgs) < n:
            style = self.sc.personas[who].style
            burst = 1 + (rng.random() < style.double_text) * (1 + (rng.random() < 0.3))
            for _ in range(burst):
                if len(msgs) >= n:
                    break
                kind, body = self.filler_text(rng, who, t, era, not msgs, len(msgs) == n - 1)
                msgs.append(Msg(t, "dm", who, body, kind=kind))
                t += timedelta(seconds=int(rng.uniform(4, 45)))
            replier = other(who)
            delay = timedelta(
                seconds=min(
                    MAX_REPLY.total_seconds(),
                    max(3.0, lognormal(rng, pace * self.sc.personas[replier].reply_median_s, 1.1)),
                )
            )
            t_reply = msgs[-1].ts + delay
            if self.clock.asleep(replier, t_reply):
                break  # they reply tomorrow, as a new conversation
            t, who = t_reply.replace(microsecond=0), replier
        return msgs

    def dm_filler(self, plan: Plan, scale: float) -> list[Msg]:
        rng = _rng(self.seed, "dm")
        assert plan.first_ts is not None
        out: list[Msg] = []
        cursor = plan.first_ts
        day = self.sc.start
        while day < self.end_day:
            era = self.era_for(day)
            rate = scale * era.volume  # messages per day
            sessions = max(1.0, rate / max(SESSION_SIZE, 3.5 * math.sqrt(rate)))
            lo = self.clock.local_midnight_utc("me", day)
            hi = self.clock.local_midnight_utc("me", day + timedelta(days=1))
            # The day's first session follows the overnight gap, so it decides "who texts
            # first"; later sessions start after it with either person initiating.
            starts = []
            for _ in range(1 + poisson(rng, sessions - 1)):
                who = "them" if rng.random() < self.sc.initiator_share_them else "me"
                s = self.pick_start(rng, who, starts[0][0] if starts else lo, hi)
                if s is not None:
                    starts.append((s, who))
                elif not starts:
                    break
            for s, who in sorted(starts):
                s = max(s, cursor + timedelta(minutes=rng.randint(5, 40)))
                if s >= hi or s >= self.t_end or plan.blocked(s):
                    continue
                pace = min(1.0, max(0.25, math.sqrt(30.0 / rate)))
                for m in self.session(rng, s, who, rate / sessions, era, pace):
                    if plan.blocked(m.ts) or m.ts >= self.t_end:
                        break
                    out.append(m)
                    cursor = m.ts
            day += timedelta(days=1)
        return out

    # --- stage 3: group chat -------------------------------------------------

    def group_filler(self, plan: Plan, n_target: int) -> list[Msg]:
        rng = _rng(self.seed, "group")
        assert plan.first_ts is not None
        members = ["me", "them", *(f.key for f in self.sc.group.friends)]
        out: list[Msg] = []
        first_day = self.clock.local("me", plan.first_ts).date() + timedelta(days=1)
        days = (self.end_day - first_day).days
        while len(out) < n_target and days > 0:
            day = first_day + timedelta(days=rng.randrange(days))
            local = datetime.combine(day, time(18)) + timedelta(minutes=rng.randint(0, 270))
            t = self.clock.to_utc("me", local)
            for _ in range(rng.randint(3, 9)):
                who = rng.choice(members)
                body = rng.choice(text.GROUP_LINES)
                persona = self.sc.personas.get(who)
                if persona:
                    body = text.styled(body, persona, rng)
                out.append(Msg(t, "group", who, body))
                t += timedelta(seconds=int(lognormal(rng, 90, 1.0)) + 2)
        return out[:n_target]

    # --- stage 4/5: injections, flags, reactions -------------------------------

    def inject(self, msgs: list[Msg]) -> None:
        rng = _rng(self.seed, "inject")
        planted_key = {(m.fact, m.role): m for m in msgs if m.fact and m.role in ("key", "apology")}
        dm_filler = [m for m in msgs if m.conv == "dm" and m.is_filler and m.text]
        for f in self.sc.facts:
            key = planted_key.get((f["id"], "key"))
            if f["type"] == "first_phrase" and "afterwards" in f:
                after = key.ts + timedelta(days=1)
                for m in dm_filler:
                    if (
                        m.fact is None
                        and m.kind == "closer_late"
                        and m.ts > after
                        and rng.random() < f["afterwards"]["rate"]
                    ):
                        m.text = rng.choice([f"{m.text} love you", f"love you, {m.text}"])
                        m.fact, m.role = f["id"], "after"
            elif f["type"] == "pet_name":
                start = key.ts + timedelta(hours=2)
                for m in dm_filler:
                    rate = f["rates"].get(m.sender, 0.0)
                    if m.fact is None and m.ts > start and rng.random() < rate:
                        m.text = rng.choice(f["forms"]).format(text=m.text)
                        m.fact, m.role = f["id"], "use"
            elif f["type"] == "inside_joke":
                cb = f["callbacks"]
                start = key.ts + timedelta(days=cb["min_days_after"])
                pool = [m for m in dm_filler if m.fact is None and m.ts > start]
                for m in rng.sample(pool, min(cb["count"], len(pool))):
                    m.text = rng.choice(cb["templates"])
                    m.fact, m.role = f["id"], "callback"

    def flags(self, msgs: list[Msg]) -> None:
        rng = _rng(self.seed, "flags")
        for m in msgs:
            if not m.is_filler or m.kind == "opener":
                continue
            r = rng.random()
            if r < self.sc.attachment_share:
                m.text, m.has_attachment = None, True
            elif r < self.sc.attachment_share + self.sc.edited_share:
                m.edited = True

    def reactions(self, msgs: list[Msg], plan: Plan, n: int) -> list[Msg]:
        rng = _rng(self.seed, "react")
        pool = [
            m
            for m in msgs
            if m.conv == "dm"
            and not m.is_reaction
            and m.text
            and m.role not in ("key", "reply", "apology", "last_before_silence")
        ]
        out = []
        for target in rng.sample(pool, min(n, len(pool))):
            ts = target.ts + timedelta(seconds=int(min(600, lognormal(rng, 30, 1.0))) + 1)
            if plan.blocked(ts) and target.fact is None:
                continue
            if target.fact and target.fact in {
                f["id"] for f in self.sc.facts if f["type"] == "conflict_makeup"
            }:
                continue
            verb = rng.choice(text.REACTION_VERBS)
            out.append(
                Msg(
                    ts,
                    "dm",
                    other(target.sender),
                    f"{verb} “{target.text}”",
                    role="reaction",
                    is_reaction=True,
                    reacts_to=target,
                )
            )
        return out

    # --- assembly ---------------------------------------------------------------

    def generate(self) -> tuple[list[Msg], Plan]:
        plan = self.plan()
        planted = len(plan.msgs)
        n_group = round(self.sc.group.share * self.target)
        n_react = round(self.sc.reaction_share * self.target)
        need = max(100, self.target - planted - n_group - n_react)
        days = sum(
            self.era_for(self.sc.start + timedelta(days=d)).volume
            for d in range(self.sc.duration_days)
        )
        scale = need / days
        dm = self.dm_filler(plan, scale)
        for _ in range(5):  # calibrate volume to the target; deterministic per seed
            if abs(len(dm) - need) <= 0.02 * need:
                break
            scale *= need / max(1, len(dm))
            dm = self.dm_filler(plan, scale)
        msgs = (
            plan.msgs
            + dm
            + self.group_filler(plan, n_group - sum(m.conv == "group" for m in plan.msgs))
        )
        self.inject(msgs)
        self.flags(msgs)
        msgs += self.reactions(msgs, plan, n_react)
        msgs = [m for m in msgs if m.ts < self.t_end]
        self.finalize(msgs)
        return msgs, plan

    def finalize(self, msgs: list[Msg]) -> None:
        """Whole-second, unique timestamps (planted ones never move), then ids in time order."""
        used: set[int] = set()
        for m in msgs:
            m.ts = m.ts.astimezone(UTC).replace(microsecond=0)
        for m in sorted(msgs, key=lambda m: (m.fact is None, m.ts)):
            s = int(m.ts.timestamp())
            while s in used:
                s += 1
            used.add(s)
            m.ts = datetime.fromtimestamp(s, UTC)
        msgs.sort(key=lambda m: m.ts)
        history_id = history_id_for(self.sc.id, self.seed)
        for i, m in enumerate(msgs, 1):
            m.id = f"{history_id}-{i:06d}"


def history_id_for(scenario_id: str, seed: int) -> str:
    return f"{scenario_id}-s{seed}"
