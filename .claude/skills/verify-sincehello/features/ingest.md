# Ingest

`sincehello connect` lists the available connectors and their status, and
`sincehello connect <id>` ingests one into the local store. Re-running an ingest adds nothing.

## Sub-features

- `connect-list` lists each connector with kind, state, and detail.
- `connect-ingest` ingests the synthetic sample and reports new vs. seen counts.
- `connect-idempotent` re-ingests with zero new messages.
- `connect-unknown` rejects an unknown connector id.

## How to get to it (user POV)

- Run `sincehello connect` to see what's available.
- Run `sincehello connect synthetic` (or `imessage` on a real Mac) to ingest.

## Driving it with control.py

Preconditions:

- Baseline from `features/README.md`. `launch` already ran the first ingest.

- **List.** Run `$C cli --name connect-list -- connect`. Exit `0`; a
  `synthetic    guided     ready` line naming `bean-goose-s42`.
- **First ingest.** Proven by `launch`: `.verify/<run>/evidence/01-launch-connect.txt` reads
  `synthetic: 2976 new of 2976 messages, 2 conversations`.
- **Idempotent re-ingest.** Run `$C cli --name connect-again -- connect synthetic`.
  Exit `0`; `synthetic: 0 new of 2976 messages, 2 conversations`. Then
  `$C doctor` still reports `messages: 2976`.
- **Unknown id.** Run `$C cli --name connect-unknown -- connect whatsapp`. Exit
  `1`; stderr `unknown connector 'whatsapp' (known: imessage, synthetic)`.

## Gotchas

- `connect` (list) also shows `imessage` as `ready` when the terminal has Full Disk Access.
  Never ingest it during verification; it reads your real `chat.db`.
- Ingest from a different directory with `SINCEHELLO_SYNTHETIC_DIR=<history dir>` in front
  of the command; the default is the committed sample.
- The 2976 count includes group-chat messages and reactions; the 1:1 figure is 2735.
