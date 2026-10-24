"""Hand-written filler corpus and per-persona texting style for the offline generator.

Everything here is synthetic. Filler must never contain a scenario's reserved terms
(planted phrases, pet names, inside jokes); ``check_corpus`` enforces that.
"""

from __future__ import annotations

import random
import re

from evals.generator.scenario import Persona

SLOTS: dict[str, list[str]] = {
    "food": [
        "pad thai",
        "tacos",
        "ramen",
        "pasta",
        "dumplings",
        "pizza",
        "curry",
        "a burrito",
        "pho",
        "grilled cheese",
        "sushi",
        "shakshuka",
    ],
    "place": [
        "the park",
        "that bookstore",
        "the pier",
        "the museum",
        "the beer garden",
        "the climbing gym",
        "the market",
        "the movies",
        "the record store",
    ],
    "show": [
        "the bake off",
        "that heist show",
        "the cooking competition",
        "the dragon show",
        "the true crime one",
        "the space documentary",
    ],
    "day": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    "time": ["7", "7:30", "8", "noon", "6:30", "9", "after work"],
    "friend": ["jules", "priya", "my coworker", "my roommate"],
}

OPENERS: dict[str, list[str]] = {
    "morning": [
        "morning",
        "good morning",
        "gm",
        "morning!! how'd you sleep",
        "ugh my alarm went off 3 times",
        "coffee first then words",
        "woke up before my alarm, who am i",
        "morning, it's freezing",
    ],
    "day": [
        "hey",
        "hiii",
        "quick question",
        "how's your day going",
        "ok so",
        "lunch was a disaster",
        "hello from the most boring meeting",
        "hey you",
    ],
    "evening": [
        "home finally",
        "what are you up to tonight",
        "done with work",
        "dinner thoughts?",
        "how was today",
        "just got back",
        "survived the day",
    ],
    "late": [
        "you up?",
        "can't sleep",
        "still awake?",
        "ok one more episode then bed",
        "why am i still awake",
        "brain won't turn off",
    ],
}

CLOSERS: dict[str, list[str]] = {
    "late": [
        "goodnight",
        "gn",
        "night night",
        "going to sleep, talk tomorrow",
        "sleep well",
        "ok bed for real",
        "night!",
    ],
    "day": [
        "gotta run, talk later",
        "brb meeting",
        "ok ttyl",
        "heading out",
        "talk in a bit",
        "phone dying, later",
    ],
}

TOPICS: dict[str, list[str]] = {
    "work": [
        "my manager scheduled a meeting about meetings",
        "so many emails today",
        "presentation went fine actually",
        "i have 3 deadlines on friday",
        "the printer jammed again",
        "do you have a busy week?",
        "standup ran 40 minutes",
        "i got the project!",
        "can't wait for the weekend",
        "my laptop decided to update mid call",
    ],
    "food": [
        "what should i get for dinner",
        "i made {food} and it was actually good",
        "thinking about {food} tonight",
        "have you eaten?",
        "that {food} place near {place} is open again",
        "we're out of coffee beans again",
        "i'm starving",
        "ate {food} standing over the sink, very glamorous",
        "want to get {food} on {day}?",
    ],
    "weekend": [
        "any plans saturday?",
        "want to go to {place} this weekend",
        "farmers market sunday?",
        "i'm doing nothing this weekend and i'm thrilled",
        "let's do {place} on {day}",
        "{friend} wants to do brunch",
        "rainy weekend, perfect couch weather",
    ],
    "shows": [
        "did you watch the new episode of {show}",
        "no spoilers for {show}!!",
        "ok {show} is so good",
        "what should we watch next",
        "i fell asleep during {show} again",
        "the finale of {show} was wild",
    ],
    "daily": [
        "it's so cold today",
        "it's raining again",
        "my bus was 20 min late",
        "did laundry, feel like an adult",
        "my plant is dying",
        "i need a nap",
        "phone at 4%",
        "walking home now",
        "the neighbor's dog is barking again",
        "bought a new mug, it's very important to me",
        "traffic is a nightmare",
    ],
    "family": [
        "my mom called for an hour",
        "my sister says hi",
        "family dinner on {day}",
        "my dad sent me a 6 minute voice memo",
        "grandma learned emojis, it's chaos",
    ],
    "plans": [
        "what time works for you?",
        "{time}?",
        "i'll pick you up at {time}",
        "should i bring anything",
        "see you soon",
        "on my way",
        "here!",
        "running 5 min late sorry",
        "can't wait to see you",
        "same place as last time?",
    ],
    "gym": [
        "went for a run",
        "legs are destroyed",
        "yoga was hard today",
        "skipping the gym, no regrets",
        "new personal best on the bike",
    ],
    "distance": [
        "what time is it there?",
        "you should be asleep right now",
        "just woke up, you're probably having dinner",
        "the time difference is killing me",
        "send me pics of everything",
        "call when you wake up?",
        "counting the days",
        "miss your face",
        "went to a convenience store at 2am, it was incredible",
        "your morning is my night, it's so weird",
        "only a few more weeks",
    ],
    "moving": [
        "the movers are here",
        "where did we put the tape",
        "box 14 is labeled misc and it's terrifying",
        "the couch does NOT fit",
        "elevator is broken",
        "pizza when we're done?",
        "found your hoodie in a box",
        "which box has the forks",
        "we need more boxes",
        "the landlord is here",
        "ok the bed is assembled",
        "send help",
        "so many stairs",
        "can you grab more trash bags",
        "the cat is hiding in the closet",
        "who packed the toaster with the towels",
        "keys work!",
        "wifi guy comes at 4",
        "i found the forks",
        "where are the screws for the shelf",
        "taking a 5 minute break",
        "this lamp has no box and no plan",
    ],
}

SHORTS = [
    "lol",
    "haha",
    "hahaha",
    "omg",
    "yes",
    "no way",
    "same",
    "ok",
    "true",
    "wait really?",
    "that's amazing",
    "ugh",
    "nice",
    "sounds good",
    "deal",
    "100%",
    "hmm",
    "fair",
    "oh no",
    "i'm dying",
    "stop",
    "yesss",
    "wait",
    "what",
    "right??",
    "ok ok",
    "honestly same",
    "noted",
    "perfect",
    "smart",
]

GROUP_LINES = [
    "trivia this thursday?",
    "who's in for thursday",
    "i can't make it this week",
    "we came in 2nd again",
    "the music round destroyed us",
    "i'll save a table",
    "bringing my coworker if that's ok",
    "who has the team name idea",
    "can we be 'quiz in my pants' again",
    "running late, order me fries",
    "rematch next week",
    "same time?",
    "omg we WON",
    "lol",
    "haha yes",
    "in",
    "count me in",
    "next week for sure",
    "did anyone find my scarf",
]

REACTION_VERBS = ["Loved", "Liked", "Laughed at", "Emphasized"]

_ABBREV = [
    (r"\byou\b", "u"),
    (r"\byour\b", "ur"),
    (r"\bare\b", "r"),
    (r"\btonight\b", "tn"),
    (r"\bthough\b", "tho"),
    (r"\bprobably\b", "prob"),
    (r"\bbecause\b", "bc"),
]


def fill(template: str, rng: random.Random) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: rng.choice(SLOTS[m.group(1)]), template)


def styled(text: str, persona: Persona, rng: random.Random) -> str:
    s = persona.style
    if rng.random() < s.abbrev:
        for pat, rep in _ABBREV:
            text = re.sub(pat, rep, text)
    if rng.random() < s.exclaim and not text.endswith(("?", "!")):
        text = text.rstrip(".") + "!!"
    if rng.random() < s.lowercase:
        text = text.lower()
    if s.emojis and rng.random() < s.emoji_rate:
        text = f"{text} {rng.choice(s.emojis)}"
    return text


def time_bucket(hour: int) -> str:
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "day"
    if 17 <= hour < 22:
        return "evening"
    return "late"


def all_templates() -> list[str]:
    out = [*SHORTS, *GROUP_LINES]
    for pool in (OPENERS, CLOSERS, TOPICS):
        for lines in pool.values():
            out.extend(lines)
    return out


def check_corpus(patterns: list[re.Pattern[str]]) -> None:
    """Fail fast if any filler template or slot value could leak a reserved term."""
    texts = all_templates() + [v for vals in SLOTS.values() for v in vals]
    for t in texts:
        for p in patterns:
            if p.search(t):
                raise ValueError(f"filler {t!r} matches reserved pattern {p.pattern!r}")
