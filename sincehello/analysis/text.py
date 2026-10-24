"""English tokenization, n-grams, and emoji extraction for the language stats."""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import cache
from importlib import resources

_URL = re.compile(r"https?://\S+|www\.\S+")
_BREAK = re.compile(r"[.!?;:,()\[\]\"\n]+")  # n-grams never cross these
_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)*")

_EMOJI_BASE = (
    "\U0001f300-\U0001faff"  # pictographs, emoticons, transport, supplemental symbols
    "\u2600-\u27bf"  # misc symbols and dingbats (☀ ❤ ✨)
    "\u2b50\u2b55\u2b1b\u2b1c\u231a\u231b\u23e9-\u23f3\u23f8-\u23fa"
)
_EMOJI_MOD = "[\U0001f3fb-\U0001f3ff\ufe0f]*"  # skin tones, emoji presentation selector
_EMOJI = re.compile(
    "[\U0001f1e6-\U0001f1ff]{2}"  # flags
    f"|[{_EMOJI_BASE}]{_EMOJI_MOD}(?:\u200d[{_EMOJI_BASE}]{_EMOJI_MOD})*"
)


@cache
def _lexicon(name: str) -> frozenset[str]:
    text = resources.files("sincehello.analysis.lexicon").joinpath(name).read_text()
    lines = (line for line in text.splitlines() if not line.startswith("#"))
    return frozenset(word for line in lines for word in line.split())


def stopwords() -> frozenset[str]:
    return _lexicon("stopwords.txt")


def common_english() -> frozenset[str]:
    return _lexicon("common_english.txt") | stopwords()


def segments(text: str) -> list[list[str]]:
    """Lowercased tokens, split into runs that n-grams may span (no URLs, no emoji)."""
    text = _URL.sub(".", text.lower().replace("\u2019", "'").replace("\u2018", "'"))
    return [toks for part in _BREAK.split(text) if (toks := _TOKEN.findall(part))]


def is_word(token: str) -> bool:
    # Tokens are [a-z0-9'] only, so "has a letter" is "not all digits".
    return not token.isdigit()


def words(segs: list[list[str]]) -> Iterator[str]:
    """Content words from segments(): no stopwords, no bare numbers."""
    stop = stopwords()
    for seg in segs:
        for tok in seg:
            if is_word(tok) and tok not in stop:
                yield tok


def ngrams(segs: list[list[str]], n: int) -> Iterator[tuple[str, ...]]:
    """n-grams within each segment, skipping ones with bare numbers or only stopwords."""
    stop = stopwords()
    for seg in segs:
        for i in range(len(seg) - n + 1):
            gram = tuple(seg[i : i + n])
            if all(is_word(t) for t in gram) and not all(t in stop for t in gram):
                yield gram


def emoji(text: str) -> Iterator[str]:
    for match in _EMOJI.finditer(text):
        e = match.group().replace("\ufe0f", "")
        # Count text-style and emoji-style hearts as one: single BMP symbols get U+FE0F.
        yield e + "\ufe0f" if len(e) == 1 and ord(e) < 0x10000 else e


def affection_terms() -> frozenset[str]:
    return _lexicon("affection.txt")


def pet_name_terms() -> frozenset[str]:
    return _lexicon("pet_names.txt")


def chat_slang() -> frozenset[str]:
    return _lexicon("chat_slang.txt")


_REPEATS = re.compile(r"(.)\1{2,}")


def squeeze(token: str) -> str:
    """Collapse runs of 3+ repeated characters to 2 ("yesss" -> "yess")."""
    return _REPEATS.sub(r"\1\1", token)
