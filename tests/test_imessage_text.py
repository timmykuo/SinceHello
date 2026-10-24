import json
from pathlib import Path

import pytest

from sincehello.connectors.imessage_text import DecodeError, decode_attributed_body, try_decode

FIXTURES = Path(__file__).parent / "fixtures" / "imessage"
EXPECTED: dict[str, str] = json.loads((FIXTURES / "expected.json").read_text())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_decodes_synthetic_fixture(name):
    blob = (FIXTURES / f"{name}.typedstream").read_bytes()
    assert decode_attributed_body(blob) == EXPECTED[name]


def test_fixture_set_covers_edge_cases():
    for name in ("emoji", "multiline", "non_ascii", "long_128", "long_32768", "attachment_only"):
        assert name in EXPECTED


def test_attachment_placeholder_is_preserved():
    blob = (FIXTURES / "attachment_only.typedstream").read_bytes()
    assert decode_attributed_body(blob) == "￼"


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"not a typedstream",
        b"\x04\x0bstreamtyped\x81\xe8\x03",
        (FIXTURES / "ascii.typedstream").read_bytes()[:80],  # truncated mid-string
    ],
)
def test_bad_input_raises(blob):
    with pytest.raises(DecodeError):
        decode_attributed_body(blob)


def test_try_decode_returns_none_on_failure():
    assert try_decode(None) is None
    assert try_decode(b"garbage") is None
    assert try_decode((FIXTURES / "emoji.typedstream").read_bytes()) == EXPECTED["emoji"]
