// Generates synthetic iMessage `attributedBody` blobs for decoder tests.
//
// Messages.app stores rich text as an NSAttributedString archived with NSArchiver (a
// "streamtyped" typedstream). This script archives attributed strings built from text we
// choose, so the fixtures contain no real message content.
//
// Run from the repo root:  swift tests/fixtures/imessage/generate_fixtures.swift
// Writes tests/fixtures/imessage/<name>.typedstream and expected.json.

import Foundation

let outDir = URL(fileURLWithPath: "tests/fixtures/imessage")

// Attributes Messages.app attaches to message parts, so the blobs resemble real ones.
let partAttrs: [NSAttributedString.Key: Any] = [
    NSAttributedString.Key("__kIMMessagePartAttributeName"): NSNumber(value: 0)
]
let attachmentAttrs: [NSAttributedString.Key: Any] = [
    NSAttributedString.Key("__kIMMessagePartAttributeName"): NSNumber(value: 0),
    NSAttributedString.Key("__kIMFileTransferGUIDAttributeName"): "at_0_00000000-0000-0000-0000-000000000000",
    NSAttributedString.Key("__kIMFilenameAttributeName"): "IMG_0001.HEIC",
]

func repeated(_ unit: String, bytes: Int) -> String {
    // ASCII unit so UTF-8 byte length == character count.
    String(String(repeating: unit, count: bytes / unit.utf8.count + 1).prefix(bytes))
}

var cases: [(String, NSAttributedString)] = [
    ("ascii", NSAttributedString(string: "see you at the goose pond at 7", attributes: partAttrs)),
    ("emoji", NSAttributedString(string: "omg yes 🥹❤️‍🔥 👩🏽‍💻🎉", attributes: partAttrs)),
    ("multiline", NSAttributedString(string: "line one\nline two\n\nline four\r\nend", attributes: partAttrs)),
    ("non_ascii", NSAttributedString(string: "café naïve über — “quotes” ñ 日本語 привет", attributes: partAttrs)),
    ("single_char", NSAttributedString(string: "k", attributes: partAttrs)),
    ("attachment_only", NSAttributedString(string: "\u{FFFC}", attributes: attachmentAttrs)),
]

// Length-prefix edge cases: typedstream uses one signed byte up to 127, then 0x81 + int16,
// then 0x82 + int32.
for n in [127, 128, 255, 256, 32767, 32768, 70000] {
    cases.append(("long_\(n)", NSAttributedString(string: repeated("abcdefghij ", bytes: n), attributes: partAttrs)))
}

// Mixed: text plus an attachment, with two message parts.
let mixed = NSMutableAttributedString(string: "look at this ", attributes: partAttrs)
mixed.append(NSAttributedString(string: "\u{FFFC}", attributes: attachmentAttrs))
cases.append(("text_and_attachment", mixed))

// Multi-byte text past the 1-byte length limit (length counts UTF-8 bytes, not characters).
cases.append(("long_emoji", NSAttributedString(string: String(repeating: "🦆", count: 40), attributes: partAttrs)))

var expected: [String: String] = [:]
for (name, value) in cases {
    let data = NSArchiver.archivedData(withRootObject: value)
    try data.write(to: outDir.appendingPathComponent("\(name).typedstream"))
    expected[name] = value.string
}
let json = try JSONSerialization.data(withJSONObject: expected, options: [.prettyPrinted, .sortedKeys])
try json.write(to: outDir.appendingPathComponent("expected.json"))
print("wrote \(cases.count) fixtures")
