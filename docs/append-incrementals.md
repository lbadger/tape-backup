# Appending incrementals and on-tape metadata

Status: included in v2.0.0; not included in v1.0.0. Automated checks use
file-backed media and a cartridge simulator.
Physical-drive qualification remains outstanding. See the [README](../README.md)
for commands and diagrams.

## Behavior and compatibility

Append an incremental to the final cartridge of its completed parent:

```bash
./tape-backup backup --source /opt --level incremental \
  --base PREVIOUS_BACKUP_ID --device /dev/nst0
```

The base must be the latest completed backup at the destination's recorded end,
with matching source path and SSH identity. Continue a linear chain by passing
the newest successful ID to the next invocation. A backup ID identifies an archive;
its volume numbers are independent of physical cartridge labels.

Every incremental appends automatically. `--append` remains an optional
compatibility flag; omitting it cannot select an overwrite path. The current
executable reports `2.0.0` to distinguish it from earlier local builds.

Recorded continuation cartridges are refused before writing, and the same volume
is requested again while the source and recovery buffers remain live. Both manual
prompts and loader commands can supply another cartridge or cancel. There is no
automatic erase, and header write or commit failures are not treated as a
rejected-cartridge retry. Process exit still loses the RAM-only continuation state.
Initialize previously used continuation cartridges ahead of time with the explicit
`wipe` command. It uses the drive lock, erases only after confirmation (or `--yes`),
verifies blankness, and leaves the tape loaded. Default initialization uses short
erase; long erase requires `--long`. Waiting at a media prompt suspends progress
and child-process logging until the prompt is answered.

| Physical cartridge | Segments in recording order |
| --- | --- |
| A | Full; metadata; incremental 1; metadata; start of incremental 2 |
| B | Rest of incremental 2; metadata; incremental 3; metadata |

A full created by v1.0.0 stays readable. Each appended backup is an independent
format-3 stream, with its own header, sequence, checksums, parent ID, snapshot,
and completion marker. Existing headers and archive records are not rewritten.
Use the updated reader to select later backups by ID.

No MAM storage, persistent local snapshot, external catalog, or disk archive is
required. Queues and recovery data stay bounded; snapshots use Linux memory-backed
files. GNU tar selects incremental changes from its snapshot's directory entries
and timestamps. There is no content-hash manifest for file selection.

## Preparing a safe append

1. Request the base's final cartridge using the loader action `append BASE_ID 0 DEVICE`.
2. Position to recorded end of data and look for a completed metadata file.
3. Validate its version, catalog/snapshot checksums, trailer, and backup identity.
   Check actual segment headers against cached locations and header checksums.
   Require clean, unchanged EOD immediately after the metadata file.
4. Without a usable final metadata file, scan the requested base's volumes to
   recover its snapshot and validate completion, without hashing archive payloads.
   Inspect the remaining tail; refuse other backups, corrupt records, partial
   metadata, or ambiguous EOD. An old footer never authorizes writing after an
   interrupted append.
5. Validate source and SSH identity. Keep the final device descriptor open while
   preparing the source. Before writing, recheck a known header and recorded EOD.
6. Initialize absolute accepted/durable record counters, then write and commit
   the new backup header at EOD. Continue the existing streaming/replay algorithm.

The drive lock covers this sequence. It coordinates this application's jobs
under the same account, not arbitrary programs or other accounts. Exclusive drive
access remains required. `--volume-size` counts total formatted record bytes
already on the cartridge plus new records, excluding physical filemark overhead.

The navigator treats filemarks separately from recorded EOD and from I/O errors.
Positions obtained through MTIOCPOS are used with MTSEEK; they do not prove
archive durability. Setup enables the Linux driver's `scsi2logical` option so
both operations use logical addresses; the legacy device-dependent position
form is rejected by some drives. Fresh rewound cartridges start at position zero
without querying the drive before their first write. Non-flushing SCSI READ POSITION is used for durability, with
absolute counters including already recorded objects. Unsupported durability
telemetry retains synchronous commits when recovery memory fills. Unsupported
or ambiguous append positioning aborts rather than guessing a destination.

## Metadata file layout and lookup

After committing the archive, snapshot, and completion marker, the writer adds
an independent metadata file on the final cartridge:

| Record(s) | Contents |
| --- | --- |
| Header | Checksummed format-3 envelope, type `metadata`, metadata version 1, latest completion summary, payload sizes, catalog checksum, total used record bytes |
| Catalog | JSON array of segment ID, volume, position, record-byte offset, and header SHA-256 for this cartridge |
| Snapshot | Copy of the latest GNU tar snapshot, bound to its completion summary by size and SHA-256 |
| Trailer | Checksummed `metadata-end`, version, total record count, and metadata-header SHA-256 |
| Filemark | Synchronous commit of the metadata file |

All records are 64 KiB; payloads are padded with zeros. Catalog JSON is limited
to 64 MiB. The snapshot is streamed through a RAM-backed file. Metadata has its
own version, so archive format 3 does not change.

On physical media, lookup positions to EOD and backspaces to the last tape file,
allowing one terminal filemark or an additional empty terminal file. File-backed
media use the fixed-size final trailer to find the start. Footer checks include
confirming that no later record follows it and that EOD has not changed. Header
checks bind cached positions to the actual cartridge. Positioning still takes
physical time even when archive payloads do not need to be read.

The catalog covers backup segments on that cartridge; it is not a replacement
for all cartridges in the chain. If a full spans tapes, its final footer includes
the snapshot needed for the next append, but restore still requires earlier tapes.
A v1.0.0 tape without a footer requires a base scan for its first append.

Metadata is redundant with the archive's original snapshot and completion data.
If a configured volume cap leaves no room for the footer, omit it and use scanning
next time. If the physical footer write or commit fails, warn after the archive
has already been committed. Restore/verify can still use that archive. A partial
footer prevents further appends; preserve those backups for restore and use a
new full on separate media if the chain cannot be continued.
Footer rollover onto a separate cartridge is not implemented.

## Rollover and interrupted writes

Once the new header is committed, end-of-medium and recoverable write/flush
failures use the existing bounded replay window. Close the tape stream, rewind
and eject the cartridge, then request blank media and continue the same backup ID
with its next volume number. Preserve all earlier cartridges and their committed
records. If eject fails, report the error and continue to the prompt or loader
with the buffered data retained. The loader
receives the distinct action `blank`, never the overwrite-authorizing `write`
action. Before writing a continuation, hold the device open for reading/writing,
check that recorded EOD is position zero with no readable record, and rewind that
blank cartridge. Recorded media and ambiguous/read-error states are refused.

If the configured cap leaves insufficient space even to start the new segment,
rewind and eject the base cartridge, request blank media, and start volume 1 there.
The final cartridge stays loaded when the backup completes. An actual header write/commit
failure aborts, without silently skipping a numbered volume or overwriting the base.

An interrupted append leaves earlier completed recovery points available.
Normal append refuses the unfinished tail. There is no truncation of partial
tails, byte-resume checkpoint, or automatic repair. Preserve existing recovery
points and start a new full on separate media when the chain cannot be continued.

## Inspection, verification, and restore

Plain `inspect` lists all backup segments on the loaded cartridge, including
continuation cartridges; `--all` is an optional alias. It reads a valid final
catalog and seeks directly to the indexed headers, without archive reads. If no
usable catalog exists, `inspect --scan` permits scanning physical tape and reports completion-marker
presence. Catalog entries leave that field null because archive completion
records are not read. File-backed listing covers all cartridge files. A corrupt
tail is reported while retaining earlier discovered headers. `inspect --first`
preserves the one-header quick lookup. Listing is not data verification or proof
that all required volumes exist.

Selected-ID lookup uses the final catalog where possible, with a scan fallback.
The reader reuses the loaded cartridge when it contains the next requested
segment. Restore accepts explicit full-plus-incremental IDs in one command, or
just the next incremental IDs when continuing a previous restore. A new restore
publishes its private extraction directory only after the entire requested chain
validates. Continuing an existing restore first verifies all requested increments,
then reads them again to apply them in place without copying the full tree.
A small sibling `.DESTINATION.tape-restore.json` file records each completed
restore and prevents applying increments out of order. An older restore can be
adopted once with `restore --base LAST_RESTORED_ID`. This asserts the existing
tree's baseline; it does not compare its contents with the tape. An interrupted
in-place apply leaves a pending record and requires rebuilding the full chain
in a separate directory before continuing.
A bad trailing append does not prevent restoring an earlier selected recovery
point. `verify --backup ID` checks the selected stream, not every cartridge entry.

`list --backup ID` streams the selected archive into GNU tar's listing mode,
printing names without extraction while verifying all frames and the completion
marker. It reads every required volume, so it is not a metadata-only operation.
Omitting the ID selects the first backup. An incremental listing contains its
archived changes, not the fully reconstructed filesystem.

## Validation and remaining qualification

Automated append tests cover same-cartridge chains, earlier-record preservation,
all recovery points, metadata lookup without archive reads, no-footer scanning,
wrong base/source, incomplete tails, multiple filemarks, nonzero position origins,
position changes before writing, capacity limits, header and final-commit failures,
partial metadata, bases and incrementals spanning cartridges, selected-ID lookup,
continuation-cartridge listing, local/SSH sources, and the standalone executable.
The simulator models destructive overwrite when writing before recorded EOD.
Existing streaming tests exercise short writes, lost buffered tails, replay,
corruption, source failures, and restore publication.

The implementation was also checked against a full created by the source at
the v1.0.0 release tag: two incrementals were appended to its cartridge file,
all three recovery points restored and verified, and original bytes compared
unchanged. Regression coverage also simulates a drive rejecting legacy position
queries and checks startup errors for the failed operation and device. The
optional 10 GiB round trip is separate from the default test suite.

These tests do not qualify a physical drive. Scratch-media qualification should
check MTIOCPOS/MTSEEK and EOD behavior, filemarks across close/reopen, WRITE/READ
POSITION accounting, write protection, device resets, every rollover boundary,
and loss of buffered writes. Compare earlier records and restore each recovery
point before relying on append on a particular drive/loader combination.

See the [mt-st manual](https://manpages.debian.org/bookworm/mt-st/mt.1.en.html)
and [Linux tape driver documentation](https://www.kernel.org/doc/html/latest/scsi/st.html)
for the underlying positioning and filemark interfaces.
