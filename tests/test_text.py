from __future__ import annotations

from sincehello.analysis import text


def test_segments_split_on_punctuation_and_drop_urls():
    assert text.segments("Ok. See you at https://x.com/a, tomorrow!") == [
        ["ok"],
        ["see", "you", "at"],
        ["tomorrow"],
    ]


def test_curly_apostrophes_normalized():
    assert list(text.words(text.segments("I\u2019m sure it\u2019s Sam\u2019s"))) == [
        "sure",
        "sam's",
    ]


def test_words_drop_stopwords_and_numbers():
    assert list(text.words(text.segments("the 5 best 2am snacks"))) == ["best", "2am", "snacks"]


def test_ngrams_skip_all_stopword_grams():
    assert list(text.ngrams(text.segments("love you so much"), 2)) == [
        ("love", "you"),
        ("so", "much"),
    ]
    assert list(text.ngrams(text.segments("you are the"), 2)) == []


def test_emoji_sequences():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    thumbs = "\U0001f44d\U0001f3fd"
    flag = "\U0001f1ef\U0001f1f5"
    assert list(text.emoji(f"hi {family}{thumbs} {flag} \u2764 \u2764\ufe0f")) == [
        family,
        thumbs,
        flag,
        "\u2764\ufe0f",
        "\u2764\ufe0f",
    ]
