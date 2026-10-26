# People

`sincehello people` lists the people you talk to, by 1:1 message count, with their id and
handles. Every other analysis command takes an id from this list. `merge` moves a handle to
another person, and `me` marks a handle as yours so it drops out of the list.

## Sub-features

- `people-list` lists people with id, 1:1 count, name, and handles.
- `people-limit` caps the rows with `--limit`.
- `people-empty` tells a new user to connect first.
- `merge` moves a handle (raw value or id) to a person (id or any of their handles).
- `me` marks a handle as your own.
- `merge-unknown` rejects an unknown person or handle.

## How to get to it (user POV)

- Run `sincehello people` after `sincehello connect <connector>`.
- Run `sincehello merge <person> <handle>` when one person shows up twice.
- Run `sincehello me <handle>` when one of your own numbers shows up as a person.

## Driving it with control.py

Preconditions:

- Baseline from `features/README.md`.

- **List.** Run `$C cli --name people -- people`. Exit `0`; first row
  `2      2735  Alex Chen  (+12125550142)`, then Jules Okafor (3) and Priya Nair (4) with `0`.
- **Limit.** Run `$C cli --name people-limit -- people --limit 1`. Only the
  Alex Chen row.
- **Empty store.** Run `$C cleanup`, then
  `$C cli --name people-empty -- people`. Exit `0`;
  `No messages yet. Run \`sincehello connect <connector>\` first.` Relaunch afterwards.
- **Merge (mutates the store).** Run `$C cli --name merge -- merge 2 +12125550177`. Exit `0`;
  `Moved +12125550177 from person 3 to person 2.` Then `$C cli --name people-after-merge -- people`
  shows `Alex Chen  (+12125550142, +12125550177)` and no Jules Okafor row.
- **Me (mutates the store).** Run `$C cli --name me -- me +12125550188`. Exit `0`;
  `Marked +12125550188 as yours.` Then `people` has no Priya Nair row. Relaunch afterwards.
- **Unknown.** Run `$C cli --name merge-bad -- merge 99 +12125550177`. Exit `1`;
  `no person or handle matches '99'`. `merge 2 +10000000000` exits `1` with
  `no handle matches '+10000000000'`.

## Gotchas

- Group-only members list with `0`: analysis counts 1:1 conversations only, by design.
- Merging a group-only member into Alex leaves Alex's count at `2735`: the count is 1:1
  messages, and that handle has none. Merge proof is the handle list, not the count.
- Names come from the sample's manifest. Real iMessage handles have no names until the
  AddressBook work lands.
