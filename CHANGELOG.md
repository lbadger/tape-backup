# Changelog

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
