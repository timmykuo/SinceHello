"""Decode message text from iMessage `attributedBody` blobs.

On newer macOS, `message.text` is often NULL and the text lives in `attributedBody`: an
NSAttributedString archived by NSArchiver as a typedstream ("streamtyped"). The layout we
rely on (see tests/fixtures/imessage/ for synthetic samples):

    04 0b "streamtyped" 81 e8 03         header (version 1000)
    ... NSAttributedString, NSObject ...
    84 84 84 08 "NSString" 01 94         the string object's class (or NSMutableString)
    84 01 2b                             type encoding "+" (a C byte string follows)
    <length> <UTF-8 bytes>               length uses the typedstream integer encoding

The attribute runs that follow aren't needed: we only want the plain string. This is not a
general typedstream parser.
"""

from __future__ import annotations

import struct

HEADER = b"\x04\x0bstreamtyped"
STRING_CLASSES = (b"NSString", b"NSMutableString")
STRING_TYPE = b"\x84\x01+"

# Typedstream integer tags: a value byte in -110..127 is the value itself.
_INT16 = 0x81
_INT32 = 0x82


class DecodeError(ValueError):
    pass


def decode_attributed_body(blob: bytes) -> str:
    """Return the plain text of an archived NSAttributedString. Raises DecodeError."""
    if not blob.startswith(HEADER):
        raise DecodeError("not a typedstream")
    starts = [i for i in (blob.find(c, len(HEADER)) for c in STRING_CLASSES) if i >= 0]
    if not starts:
        raise DecodeError("no NSString in stream")
    pos = blob.find(STRING_TYPE, min(starts))
    if pos < 0:
        raise DecodeError("no string payload after NSString")
    length, pos = _read_length(blob, pos + len(STRING_TYPE))
    end = pos + length
    if end > len(blob):
        raise DecodeError(f"string length {length} runs past end of blob")
    try:
        return blob[pos:end].decode("utf-8")
    except UnicodeDecodeError as e:
        raise DecodeError(f"invalid UTF-8: {e}") from None


def try_decode(blob: bytes | None) -> str | None:
    """Like decode_attributed_body, but returns None for missing or undecodable blobs."""
    if not blob:
        return None
    try:
        return decode_attributed_body(blob)
    except DecodeError:
        return None


def _read_length(blob: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(blob):
        raise DecodeError("truncated length")
    tag = blob[pos]
    if tag == _INT16:
        size, fmt = 2, "<h"
    elif tag == _INT32:
        size, fmt = 4, "<i"
    elif tag < 0x80:
        return tag, pos + 1
    else:
        raise DecodeError(f"unexpected length tag 0x{tag:02x}")
    raw = blob[pos + 1 : pos + 1 + size]
    if len(raw) != size:
        raise DecodeError("truncated length")
    (value,) = struct.unpack(fmt, raw)
    if value < 0:
        raise DecodeError(f"negative length {value}")
    return value, pos + 1 + size
