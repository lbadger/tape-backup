# Changelog

## 2.1.1 — 2026-09-24

- Carry a bounded ancestor list in new backup headers and catalog summaries.
  Refuse prompted erasure of any known ancestor, including full backups on other
  cartridges. If an older incremental has incomplete ancestry, refuse recorded
  tape wipes at the prompt while keeping append and restore available.
- Reject missing backup IDs/volumes through complete, current catalogs after
  validating their endpoints; avoid traversing archive payload merely to reject
  a wrong tape. Preserve fallback for absent or partial catalogs.
- Include read-back in `backup --verify` and `zfs-backup --verify` total progress,
  retain the drive lock across both passes, and reserve completion for successful
  verification. Preserve the committed result if read-back fails or is interrupted.
- Show standalone verification progress and ETA from unique archive payload
  bytes. Release source snapshot RAM before post-backup verification.
- Add explicit `eject` at manual media prompts. Unlock and unload without ending
  the active operation; preserve the requested volume, buffers, and drive lock.
  The standalone `eject` command also unlocks before unloading.

## 2.1.0 — 2026-09-24

- Add `wipe` at interactive blank-cartridge prompts, followed by a separate
  `WIPE` confirmation. Short-erase and verify the loaded tape under the active
  backup's drive lock, retaining the source stream, volume number, and RAM buffers.
- Refuse in-prompt erasure of active backup/base headers and remembered cartridges
  already used by the job. Recheck the header after confirmation. Cancellation
  and erase failures return to the load prompt; Enter never authorizes an erase.
- Add total progress bars for tar and native ZFS backups/restores, spanning tape
  changes and requested restore chains. Include existing verification passes,
  use verified sizes during apply, label estimates, and reserve 100% for success.
  Redirected logs show percentages; prompts continue to suppress background logs.
- Add multi-cartridge wipe/restore regressions, append-base protection tests,
  confirmation/drive-error tests, progress accounting, and standalone terminal
  checks. Tape formats and the SSH protocol are unchanged.

## 2.0.1 — 2026-09-24

- Share restore destination locks across home directories and accounts, including
  native ZFS targets. Recheck tar restore history before applying an incremental.
- Request continuation cartridges directly instead of rewinding and scanning the
  exhausted tape. Preserve same-cartridge lookup between different backups.
- Match exclusion inventory with GNU tar's escapes, POSIX character classes,
  bracket patterns, literal patterns, and source locale. Preserve existing
  archive selection and exclusion policies.
- Show wrapped terminal progress with readable sizes, separate source/I/O rates,
  buffer occupancy, elapsed time, and a `finalizing` ETA during the final commit.
- Format drive status and backup information as readable terminal details. Add
  `info` as an alias for `inspect`, with `--text`/`--json` on information commands.
  Redirected stdout remains JSON; redirected progress retains compact logs.
- Add regressions for competing processes with different homes, changed restore
  history, continuation read counts, real GNU tar matching, native ZFS locking,
  terminal formatting, and standalone-binary exclusions/information output.

## 2.0.0 — 2026-09-24

- Add native `zfs-backup` and `zfs-restore` for existing filesystem snapshots,
  full/incremental streams, local/SSH sources, and encrypted raw sends. Validate
  snapshot GUIDs and keep received datasets read-only/unmounted. Never force a
  rollback. New ZFS volume headers use format 4; tar format 3 remains readable.
- Add `--exclude` and `--exclude-from`, source-relative wildcard matching,
  inventory pruning, and policy inheritance for local/SSH incrementals. Reject
  policy changes before writing; changing exclusions requires a new full backup.
- Require blank media for full backups as well as continuations. Initialize used
  cartridges explicitly with `wipe`; pressing Enter cannot authorize overwrite.
- Retry wrong read cartridges without losing accepted restore progress. Use a
  shared drive lock across accounts and tape-mode aliases, and clean up on SIGTERM.
- Remove redundant inspection seeks and unneeded RAM snapshot copies. Physical
  inspection requires `--scan` for sequential fallback and returns nonzero for
  incomplete results while retaining discovered backup entries.
- Improve the no-argument menu/help with file and ZFS examples. Add read-only
  `status`/`doctor`, JSON backup outcomes, and optional post-backup `--verify`.
- Version the SSH transport as 3 so old helpers cannot silently ignore new
  options. Update both ends of SSH backups together.
- Add CI for the source, standalone executable, SSH, simulated cartridges, and
  real OpenZFS scratch-pool round trips; retain an actual v1.0.0 format-3 fixture.

- Add `list [--backup ID]` to print archived file names without extraction,
  discover the first backup when no ID is supplied, and follow continuation
  tapes. Verify the complete stream, keep names on stdout, and pause output at
  tape prompts.
- Keep `backup --volume-size` available as a hidden testing option. Document using
  10 GiB limits on physical cartridges and verify planned volume changes and
  complete multi-tape restores without filling the physical media.
- Add `wipe` to initialize the loaded tape with short erase, explicit terminal
  confirmation or `--yes`, and optional `--long`. Verify the tape is blank before
  reporting success, leave it loaded, and honor the drive lock.
- Pause progress and local/SSH child-process logging while waiting for a tape
  change or loader. Resume after the response; keep diagnostic buffering bounded.
- Add `compression [status|on|off]` with current drive-state queries, changes via
  the Linux tape driver, read-back verification, and drive locking. Unsupported
  or unreadable state never silently reports OFF.
- Keep a backup active when a recorded cartridge is inserted for continuation.
  Close the rejected tape and request the same volume again, retaining buffered
  data and the local/SSH source stream. Preserve overwrite protection; never erase
  or eject automatically. Cancellation and unrelated drive failures still stop
  the operation.
- Make `inspect` list every backup segment on the loaded cartridge by default,
  including appended incrementals. Read the final metadata catalog and seek to
  indexed headers without reading archive data; allow a scan when requested.
  Keep `--all` as an alias and add `--first` for the previous one-header behavior.
- Support restoring a full chain in one command or applying subsequent
  incrementals to the same destination in separate commands. Record the last
  successful restore in a small sibling history file; validate parent/source
  ordering and verify requested incrementals before applying in place. Reject
  further applies after an interrupted update. `restore --base ID` adopts a
  directory restored by an older executable without re-extracting the full.
- Retry loaded-cartridge lookup from the beginning when an incomplete trailing
  metadata file would otherwise prevent rereading a completed backup.
- Make every incremental append automatically; `--append` is an optional
  compatibility flag. Fix the destructive default that could overwrite the full
  backup when the base cartridge remained loaded. Require blank continuation
  cartridges and reject recorded/ambiguous media before writing. Loaders receive
  a distinct `blank` request for continuations.
- Enable SCSI logical block addressing for tape catalog position/seek operations.
  Check blankness before starting a full backup's first cartridge, and include
  the device and startup operation in I/O errors. This addresses startup failures
  on drives that reject the legacy device-dependent READ POSITION command form.
- Add `backup --level incremental --base ID` to reuse the base's final
  cartridge. Validate its recorded tail and source identity before writing at
  end of data; continue onto fresh media with the existing replay mechanism.
  Preserve existing format-3 backups and refuse interrupted or ambiguous tails.
- Write a checksummed metadata file after each completed backup, with cartridge
  segment locations and a copy of the latest GNU tar snapshot. Subsequent appends
  can load the snapshot without traversing archive data. Existing tapes without
  metadata use a scan fallback; no MAM or persistent local catalog is required.
- Locate selected backup IDs on shared cartridges for restore, verify, and
  inspect. Add `inspect --all` to list cartridge segments and report partial tails;
  listing does not certify archive integrity. Reuse loaded cartridges in restores.
- Document full and incremental flows with Mermaid diagrams, metadata placement,
  change detection, append commands, and recovery limitations.
- Stop automatically ejecting tapes, including at tape changes and after reading
  an incremental base. Add `eject --device /dev/nst0` to rewind and unload a tape
  explicitly, using the existing drive lock.
- Make `inspect --first` read only the first 64 KiB tape header to discover the backup
  ID and source metadata. Report that archive data and backup completion have
  not been verified; use `verify` for full data checks.
- Preserve format-3 tar tapes and add format-4 native ZFS headers. Remove format-2 header decoding,
  large legacy chunks, single-chunk replay compatibility, and filemark-based
  metadata skipping. Older formats are rejected by inspect, verify, restore,
  and incremental-base loading. Current format-3 backups are unchanged.

## 1.0.0 — 2026-09-23

Major update: continuous streaming replaces per-chunk tape flushes. New backups
use format 3 and require v1.0.0 to read. Format-2 backups remain readable; upgrade
both ends of SSH backups. The default buffer budget is now 1 GiB per buffer.

- Add continuous writes with small frames, a background read-ahead queue, and a
  bounded recovery window. Increase the default buffer budget to 1 GiB and the
  maximum to 10 GiB; each budget applies to both source read-ahead and retained
  recovery data.
- Use non-flushing SCSI READ POSITION reports to release confirmed tape records.
  Remove per-frame filemark commits. Commit at volume boundaries, backup completion,
  or when recovery space is exhausted without enough confirmed data. Fall back
  safely when position tracking is unavailable; disable kernel async writes while
  preserving drive buffering.
- Introduce tape format 3 with recovery of multiple unconfirmed/duplicate frames.
  Continue reading format 2, including large chunks, and support incremental chains
  across versions. Format-3 metadata scans read past data records without filemark
  spacing. New tapes require the updated reader.
- Show buffer budget, queue occupancy, recovery bytes, confirmed archive bytes,
  live source rate, reader state, and the reason for a flush in progress output.
- Propagate source errors and stop tar/SSH and the reader thread on cancellation
  or write failure. Do not stage archive data on disk.
- Use version 2 of the SSH protocol with 64-bit packet lengths. Both ends of an
  SSH backup should be updated; transport frames are now small regardless of the
  tape host's buffer budget.
- Fix manual tape-change prompts on non-seekable terminals and include the
  underlying terminal error when a prompt cannot be opened.

## 0.3.0

- Add `backup --ssh USER@HOST` to stream a remote Linux source to a local tape
  drive, including full and incremental backups, file names, rates, and ETA.
- Use the same executable on the source, with RAM-only incremental snapshots and
  a bounded, versioned SSH stream. No archive or persistent state is staged on
  either machine. Restore requires no access to the original source.
- Support SSH configuration/agents, explicit port/key/config options, and a
  remote executable path. Verify host keys and use batch authentication.
- Reject incomplete SSH streams and remote tar failures without a completion
  marker. Bind incremental chains to the source path and SSH host/account.
- Remove `legacy-restore`, the v0.1 implementation, and its build dependency.
  Keep support for existing v0.2 streaming tapes.
- Test real SSH full/delta restores, tape rollover/replay, disconnected streams,
  remote errors, host-key rejection, and standalone executables at both ends.

SSH sources need GNU tar and the same tape-backup version. Authentication uses
keys or an agent. Interrupted SSH backups restart from the source on fresh tapes;
the previous completed backup remains usable as an incremental base. Physical
tape hardware remains untested.

## 0.2.0

- Stream GNU tar directly from source to tape and from tape into restored files.
  Remove required state directories, disk archive staging, and external catalogs
  from the default backup/restore workflow.
- Store incremental snapshots, identifiers, checksums, and completion markers on
  the tape set. `--base ID` reads the preceding snapshot from tape into RAM.
- Retain bounded chunks in RAM until synchronous filemark flushes succeed; replay
  incomplete or uncertain writes on the next volume and deduplicate on restore.
- Print file names by default, with five-second progress updates, I/O transfer
  rates, average speed, elapsed time, and approximate current-archive ETA.
- Add `--quiet`, `--buffer-size`, `inspect`, and full read-back `verify` commands.
- Preserve v0.1 recovery through `legacy-restore` with the original catalogs.
- Expand validation to 60 passing tests, including the standalone executable and
  streaming restores with no Python installed in the runtime container.

This changes the default CLI and tape format. Start a new full backup when moving
to streaming mode. In-process tape rollover/retry is supported; after interruption
or power loss, restart the backup from the source on fresh tapes. Restore also
restarts from its first tape. No persistent byte-resume checkpoint is kept.
Physical tape hardware remains untested.

## 0.1.0

Initial release of the Linux tape backup and restore tool.

- Full and incremental GNU tar archives across multiple tape volumes.
- Read-back verification and SHA-256 checksums before checkpointing data.
- End-of-tape rollover, short-write recovery, and resumable backups and restores.
- Ordered full-plus-incremental restore with deletion and rename handling.
- Configurable tape device, defaulting to `/dev/nst0`, and media-loader hooks.
- File-backed media for testing and inspection.
- Standalone Linux x86-64 executable and Docker/native build scripts.
- 34 automated tests covering archive operations, injected failures, and the binary.

The binary embeds Python and targets glibc 2.31 or newer. GNU tar, `mt` (from
`mt-st`), and normal Linux runtime libraries including zlib remain required.
Physical tape hardware has not been tested; automated checks use simulated media.
Preserve the catalogs independently of the backup machine: they are required for
restore.
