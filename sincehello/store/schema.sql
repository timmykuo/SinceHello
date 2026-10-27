-- Normalized message store (SPEC.md §5). Every connector writes into these tables.
-- Timestamps are integer milliseconds since the Unix epoch, UTC.

CREATE TABLE person (
    id           INTEGER PRIMARY KEY,
    display_name TEXT,
    is_me        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE handle (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER REFERENCES person(id),
    source       TEXT NOT NULL,            -- connector id, e.g. "imessage"
    raw_value    TEXT NOT NULL,            -- phone, email, username, as the source gives it
    display_name TEXT,
    UNIQUE (source, raw_value)
);

CREATE TABLE conversation (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    source_conv_id TEXT NOT NULL,
    is_group       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source, source_conv_id)
);

CREATE TABLE conversation_member (
    conversation_id INTEGER NOT NULL REFERENCES conversation(id),
    handle_id       INTEGER NOT NULL REFERENCES handle(id),
    PRIMARY KEY (conversation_id, handle_id)
);

CREATE TABLE message (
    id               INTEGER PRIMARY KEY,
    source           TEXT NOT NULL,
    source_msg_id    TEXT NOT NULL,
    conversation_id  INTEGER NOT NULL REFERENCES conversation(id),
    sender_handle_id INTEGER REFERENCES handle(id),   -- NULL when is_from_me
    is_from_me       INTEGER NOT NULL,
    sent_at_utc      INTEGER NOT NULL,
    text             TEXT,
    is_reaction      INTEGER NOT NULL DEFAULT 0,
    has_attachment   INTEGER NOT NULL DEFAULT 0,
    edited           INTEGER NOT NULL DEFAULT 0,
    reacts_to_source_msg_id TEXT,              -- reactions: target's source_msg_id (no FK;
                                               -- the target may arrive later or never)
    reaction_kind    TEXT,                     -- love | like | dislike | laugh | ...
    UNIQUE (source, source_msg_id)
);

CREATE INDEX message_conv_time ON message (conversation_id, sent_at_utc);
CREATE INDEX message_reacts_to ON message (source, reacts_to_source_msg_id);

-- Full-text search over message text, kept in sync by triggers.
CREATE VIRTUAL TABLE message_fts USING fts5 (
    text, content='message', content_rowid='id'
);

CREATE TRIGGER message_ai AFTER INSERT ON message BEGIN
    INSERT INTO message_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER message_ad AFTER DELETE ON message BEGIN
    INSERT INTO message_fts (message_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER message_au AFTER UPDATE OF text ON message BEGIN
    INSERT INTO message_fts (message_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO message_fts (rowid, text) VALUES (new.id, new.text);
END;

-- Per-person timezone periods (SPEC.md §6.1).
CREATE TABLE tz_period (
    id        INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    start_utc INTEGER NOT NULL,
    end_utc   INTEGER,                     -- NULL = open-ended
    iana_tz   TEXT NOT NULL,
    source    TEXT NOT NULL                -- default | user | inferred_confirmed
);

-- Inferred timezone periods waiting for the user (SPEC.md §6.1). Confirmed ones are
-- copied into tz_period; ignored ones stay here so they aren't suggested again.
CREATE TABLE tz_suggestion (
    id               INTEGER PRIMARY KEY,
    person_id        INTEGER NOT NULL REFERENCES person(id),
    kind             TEXT NOT NULL,        -- shift | home
    start_utc        INTEGER NOT NULL,
    end_utc          INTEGER,              -- NULL = open-ended
    utc_offset_hours REAL NOT NULL,        -- the inferred offset
    iana_tz          TEXT NOT NULL,        -- best guess: a named zone, else Etc/GMT-N
    confidence       TEXT NOT NULL,        -- high | medium | low
    score            REAL NOT NULL,
    place            TEXT,                 -- from naming (sincehello tz infer --name)
    hint_message_id  TEXT,                 -- "source:source_msg_id" of a travel mention
    hint_quote       TEXT,
    details          TEXT NOT NULL DEFAULT '{}',  -- JSON: how it was inferred
    status           TEXT NOT NULL DEFAULT 'pending'  -- pending | confirmed | ignored
);

-- Opaque per-connector cursor for incremental ingest (e.g. max chat.db ROWID).
CREATE TABLE sync_state (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at INTEGER NOT NULL
);

INSERT INTO person (id, display_name, is_me) VALUES (1, 'Me', 1);
