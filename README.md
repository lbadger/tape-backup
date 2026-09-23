# Tape backup and restore

`tape-backup` streams full and incremental GNU tar archives directly between a
local or remote Linux source directory and a Linux tape drive. It requires **no `--state`
directory, no disk copy of the archive, and no external restore catalog**.
Checksums, backup-chain identifiers, completion information, and the incremental
snapshot are carried on the tapes.

Backup and restore print file names, transfer totals, current and average MiB/s,
elapsed time, and an approximate ETA. `/dev/nst0` is the default; use `--device`
to select another non-rewinding tape drive.

## Download

Version 1.0.0 implements continuous streaming with small frames, background
read-ahead, and buffers up to 10 GiB. It writes tape format 3 and reads existing
format-2 backups.

Download the executable and checksum from the
[v1.0.0 release](https://github.com/lbadger/tape-backup/releases/tag/v1.0.0):

```bash
curl -fLO https://github.com/lbadger/tape-backup/releases/download/v1.0.0/tape-backup
curl -fLO https://github.com/lbadger/tape-backup/releases/download/v1.0.0/tape-backup.sha256
sha256sum --check tape-backup.sha256
chmod +x tape-backup
./tape-backup --version
```

The supplied binary is Linux x86-64, built against glibc 2.31. It embeds Python;
Python and Docker are not required at runtime. Install GNU tar and `mt` from
`mt-st` (`sudo apt install tar mt-st` on Debian/Ubuntu). Normal Linux runtime
libraries, including glibc and zlib, are required. Alpine/musl needs a separate
build. Running the source script requires Python 3.11+.

Use a Linux SCSI tape drive supporting variable-length 64 KiB records. The user
must have permission to operate the device. The script disables immediate rewind
and filemark modes, plus kernel asynchronous writes, when necessary; changing
those driver settings may require root. Run it with exclusive access to the drive.
Locks prevent competing jobs under the same Unix account, but cannot exclude other
tape programs or accounts.

## Full backup

```bash
./tape-backup backup --source /opt/audiobooks --level full --device /dev/nst0
```

There is no `--state` argument. The script inventories file metadata to estimate
size, loads a tape, and starts GNU tar with its output piped to the tape writer.
Source file contents are never staged on disk. A successful command prints the
backup ID on stdout; progress and file names go to stderr. Label every cartridge
with that ID and the volume number shown in the prompt.

**Writing rewinds and overwrites the loaded cartridge.** Each new volume uses a
fresh writable cartridge. Backup IDs describe one full or incremental archive;
a new backup starts on fresh media rather than appending to an existing tape.

Omit `--volume-size` to continue until the drive reports end of medium. To impose
an earlier limit, for example:

```bash
./tape-backup backup --source /opt/audiobooks --volume-size 100GiB
```

This cap counts formatted record bytes, including headers and padding, but not
physical tape filemarks or drive overhead. Actual end-of-medium handling still
applies. Sizes accept integer bytes or integer `KiB`, `MiB`, `GiB`, or `TiB` values.
The minimum volume limit is 256 KiB.

Use a filesystem snapshot or quiesce applications while backing up. GNU tar errors,
including changed/unreadable files, prevent writing the final completion marker.
This tool does not establish database or application consistency. It includes
mounted directories beneath the source and does not follow symlinks.

## Incremental backup without a local snapshot

An incremental needs the snapshot from the previous **completed** backup. It reads
that snapshot from the previous tape set into a temporary Linux memory-backed
file, then writes a new backup to fresh tapes:

```bash
./tape-backup backup --source /opt/audiobooks --level incremental \
  --base PREVIOUS_BACKUP_ID --device /dev/nst0
```

Load the previous backup's volumes in order when prompted, followed by fresh
writable tapes for the new backup. Format-3 metadata scans read past archive
records without keeping or hashing their payloads; there are no per-chunk
filemarks to skip. Format-2 tapes still use filemark spacing. This can make loading
a format-3 incremental base slower. No persistent snapshot/cache file is required.
Metadata memory consumption grows with the number of filenames.

Each incremental is relative to the backup specified by `--base`. For a linear
chain, pass the most recent successful backup ID. The source directory must match.
All ancestors, beginning with a full backup, are needed for restore. A new full
backup starts an independent chain. An incomplete tape set cannot be used as a
base.

## Back up a remote source over SSH

Run the command on the **machine with the tape drive**. Install the same version
of `tape-backup` on the source machine, together with GNU tar. Both machines must
run Linux; use a binary built for each machine's architecture. Neither machine
needs Python when using the standalone binary. The tape host also needs the
OpenSSH client; the source must accept SSH connections.

For example, copy the binary to the source account's home directory:

```bash
scp ./tape-backup backup@fileserver:/home/backup/tape-backup
ssh backup@fileserver 'chmod +x /home/backup/tape-backup'

./tape-backup backup --ssh backup@fileserver \
  --remote-program /home/backup/tape-backup \
  --source /srv/data --level full --device /dev/nst0

./tape-backup backup --ssh backup@fileserver \
  --remote-program /home/backup/tape-backup \
  --source /srv/data --level incremental --base PREVIOUS_BACKUP_ID \
  --device /dev/nst0
```

If `tape-backup` is in the remote account's PATH, omit `--remote-program`. This
option is one executable path, not a shell command. `--source` must be an absolute
path on the remote machine. The SSH account must be able to read all source files
and metadata; the tool does not run sudo automatically.

Use SSH keys or an agent and establish the server's trusted host key before
starting. Backups use batch authentication and strict host-key checking, so an
unknown host or a password prompt fails before tape writing starts. Existing
OpenSSH configuration, including host aliases and jump hosts, is honored. Options
`--ssh-port 2222`, `--ssh-identity /path/to/key`, and `--ssh-config /path/to/config`
override connection settings. Keep the same `--ssh` host/account or alias and
`--ssh-port` setting throughout an incremental chain; the resolved source path
must also match.

The remote helper inventories metadata for ETA, runs GNU tar, and streams archive
chunks over SSH to the local tape writer. Previous and updated incremental
snapshots stay in RAM on both machines. No archive or state directory is staged
on either machine. Tape changes pause the stream through pipe backpressure;
memory use remains bounded by buffers and snapshot metadata rather than archive
size. Remote file names, errors, transfer rates, and ETA appear in the local
terminal. SSH transport encrypts the network connection; tapes are not encrypted
by this tool.

An SSH disconnection or remote tar failure leaves an incomplete tape set that
cannot be used for restore or as an incremental base. Restart on fresh tapes,
using the previous completed backup as the base. Restore uses the usual local
`restore` command below and requires no SSH access or original source machine.

## Restore directly from tape

Pass a full backup ID, followed by every incremental ID to the desired recovery
point, in order:

```bash
./tape-backup restore --backup FULL_ID DELTA_1_ID DELTA_2_ID \
  --destination /srv/recovered --device /dev/nst0
```

For a full-only restore, specify just the full ID. Tape headers contain the
metadata required for restore; the original machine, source directory, external
catalogs, and local incremental snapshots are unnecessary.

Restore validates each in-memory chunk before passing its archive bytes to GNU
tar. It applies additions, changes, deletions, and renames from the ordered chain.
An empty or absent destination is required. Files are extracted into a private
sibling directory and renamed into the destination only after the full chain and
its completion markers have been verified.

**Restore writes only the extracted files, not a reassembled tar archive.** Allow
space for the largest intermediate directory tree in the chain, including files
later deleted by an incremental. Publication uses a rename, without copying the
restored tree. Permissions, timestamps, links, sparse files, ACLs, and extended
attributes are restored where supported; arbitrary ownership and privileged
metadata generally require root. Restore only trusted tape sets, especially as
root. Checksums detect corruption but are not signatures or encryption.

## Progress, transfer rates, and ETA

File names are printed by default. A status line is printed every five seconds
and at completion, for example:

```text
BACKUP_ID: writing volume 1, chunk 1792; 8192.0 MiB read, 7168.0 MiB delivered; buffer 1024 MiB; queued 1020.0 MiB, recovery 130.0 MiB, committed 7040.0 MiB; reader reading, 180.0 MiB/s source; 155.0 MiB/s I/O, 149.3 MiB/s average; ETA ~02:14:08 (current archive); 55s elapsed
```

Backup status shows the configured buffer budget, queued payload bytes, recovery
bytes retained (including framing), and archive bytes confirmed on media. Each of
the read-ahead and recovery buffers has that budget; `--volume-size` can reduce
the recovery budget. The active writer frame and partly filled reader frame are
not included in the queued count.

During backup, bytes read update after each source read of up to 1 MiB, including
partially filled chunks. Source MiB/s measures archive bytes received from GNU tar
or SSH over the reporting interval; it excludes SSH framing and snapshot metadata.
The reader reports `reading`, `waiting (buffers full)`, or `finished` separately
from the writer's phase. `waiting for source` means the writer needs another
small frame; `flushing` identifies a volume boundary, backup completion, or a full
recovery buffer awaiting confirmation. A zero tape I/O rate alone does not
indicate whether source reads are still progressing.

I/O rates measure bytes passed to/from the device, including framing, padding,
and retries. They are host-side rates, not measurements of physical tape motion
or compressed media capacity. Delivered backup bytes have been accepted by the
writer; they may still be buffered in the drive. Committed bytes have been
confirmed on tape by READ POSITION or a synchronous filemark flush. Replay does
not double-count either logical counter. Restore delivery counts archive bytes
passed to GNU tar. The average includes elapsed media-change time.

The ETA estimates remaining time for the **current archive**, not later
incrementals in a restore chain. Backup estimates come from file metadata only;
restore uses that estimate from the tape header. Sparse files, incremental
selection, directory changes, media changes, and final flushing can affect its
accuracy. It starts as `calculating` and shows `finishing (estimate reached)` when
an underestimated archive is still running. No source contents are read merely
to calculate the estimate.

`--quiet` suppresses per-file output while retaining rates, ETA, summaries, and
errors. Large-file transfers still produce periodic status lines.

## Bounded buffering and failure recovery

The default buffer budget is 1 GiB; adjust it with `--buffer-size`, between 64 KiB
and 10 GiB. For example:

```bash
./tape-backup backup --source /opt --level full --buffer-size 10GiB
```

The background reader feeds a bounded queue of small frames (normally 4 MiB),
while the writer drains it continuously. Writing starts with the first frame;
it does not wait for the entire buffer budget to fill. Free queue slots are
refilled individually during writes and tape changes. Whole-archive checksums
are updated incrementally; individual frames also carry checksums and chain links.

There is no synchronous filemark after every frame. On supported physical drives,
SCSI READ POSITION reports which records have reached the medium without flushing
the drive. Only complete frames below that position are released from the recovery
buffer. The accepted-write position is checked against our record count, and the
medium position must never move backwards. Kernel asynchronous writes are disabled
to keep these counts aligned; the drive buffering setting is preserved.

The writer commits at volume boundaries and backup completion. It also commits if
the recovery buffer fills before the drive confirms enough data. This fallback is
necessary when position reporting is unsupported, unavailable, inconsistent, or
the buffer is too small for the drive's unconfirmed tail. Linux SG_IO access may
require root/CAP_SYS_RAWIO. The startup log reports unavailable position tracking;
status identifies fallback flushes as `recovery buffer full`. File-backed media
uses the same fallback with fsync instead of hardware position reports. A source
that cannot keep up, media changes, and drive behavior can still cause pauses.

The source queue and recovery window each use up to the selected budget, plus a
few active frames, allocation overhead, Python/GNU tar memory, and incremental
snapshot metadata. Allow more than 2 GiB of RAM at the default, or more than 20 GiB
with a 10 GiB budget. Very small budgets still allow one active frame and one queued
frame, and at least 128 KiB of recovery framing. Allocation grows on demand.
The SSH source only needs small transport frames and its snapshot metadata.
Format-3 restore holds a small frame plus a bounded history of header digests;
legacy format-2 restore can still require RAM for large legacy chunks.

On a short write, end-of-tape indication, or write/position I/O error, the writer
requests another tape and replays **every unconfirmed frame**, including the
partially written frame. Keep earlier volumes: they contain the confirmed prefix
and may also contain some or all of the replayed tail. Restore validates the chain
and each replayed header before suppressing duplicates, so several overlapping
frames across replacement volumes are safe. Repeated failures without confirmed
progress stop the job.

A final completion marker is written only after GNU tar exits successfully and
the new incremental snapshot has been recorded. Backup reports success only after
the final synchronous commit. Missing frames, wrong tapes, checksum failures, and
incomplete sets are rejected. Backup does not perform automatic read-back
verification; use `verify` for a full read pass.

New backups use **format 3** and require v1.0.0 for restore,
inspect, verify, and incremental-base reading. Existing format-2 tapes from
v0.2/v0.3 and earlier 0.4 development builds remain readable, including chunks up
to 10 GiB. Incremental chains may cross from format 2 to format 3. Update both ends
of SSH backups; the transport uses 64-bit version-2 packet framing.

See the [continuous-streaming design and validation plan](docs/continuous-streaming.md).

**A stopped/killed process or power failure requires restarting the backup from
the source on fresh media.** There is no persistent byte-resume checkpoint. The
previous completed backup remains usable as the next incremental base. Repeat
an interrupted restore from its first tape. Ordinary failures clean up the private
restore tree; after SIGKILL or power loss, an abandoned `.DEST.restoring-*` sibling
may remain and can be removed after confirming it belongs to that failed restore.

The executable itself extracts its bundled runtime into a temporary directory;
this is a small runtime footprint, not backup staging. A few fixed-size lock files
are also used. `TMPDIR` must support executable mappings and symlinks when running
the standalone executable.

## Inspect and verify

```bash
# Discover the ID on the loaded first tape and read its embedded metadata.
./tape-backup inspect --device /dev/nst0

# Read and verify every data chunk and the whole archive's checksum.
./tape-backup verify --backup BACKUP_ID --device /dev/nst0
```

Both commands may request subsequent volumes. `inspect` skips file data and is
not a media-integrity check; its JSON output says `data_verified: false`. `verify`
reads all data without extracting files and reports `data_verified: true` only
when it reaches and validates the completion marker.

## Supported tape formats

Version 1.0.0 writes format 3 and reads both format 3 and format 2 streaming tapes
created by v0.2, v0.3, and earlier 0.4 development builds. The legacy restore command
and disk-staging implementation have been removed. To restore v0.1 tapes, build
the [v0.1.0 source tag](https://github.com/lbadger/tape-backup/tree/v0.1.0) and use
the original catalogs. Start a new full backup when migrating from v0.1.

## Automated tape loading

`--media-command /absolute/path/to/loader` runs an executable with four arguments:

```text
write|read  BACKUP_ID  VOLUME_NUMBER  DEVICE
```

The loader must load the requested cartridge, wait for readiness, and return zero.
For `write`, that cartridge must be safe to overwrite. For ID discovery, the ID
argument is `unknown-backup`. The command runs without a shell; prompts otherwise
use `/dev/tty`. Completed volumes are unloaded before subsequent media requests.

## File-backed testing and building

`--media-dir` uses files as simulated cartridges instead of a physical device.
It cannot be combined with `--device`. Its default volume cap is 1 GiB.

```bash
full_id=$(./tape-backup backup --source ./sample-data --media-dir ./demo-tapes \
  --volume-size 1MiB --buffer-size 256KiB)
./tape-backup restore --backup "$full_id" --media-dir ./demo-tapes \
  --destination ./demo-restored

# Build a glibc 2.31 executable using Docker with BuildKit.
./build.sh

# Or build for the local Linux environment with Python 3.11+, pip 22.3+,
# the venv module, and binutils. The result inherits host library requirements.
./build.sh --local

# Test source code, SSH, and the built executable.
# SSH integration tests need ssh, ssh-keygen, and sshd (openssh-client/server).
TAPE_BACKUP_BINARY="$PWD/dist/tape-backup" python3 -m unittest discover -s tests -v

# Optional 10 GiB streaming round trip (about 31 GiB disk and >20 GiB free RAM).
TMPDIR="$PWD" TAPE_BACKUP_BINARY="$PWD/dist/tape-backup" TAPE_BACKUP_LARGE_TEST=1 \
  python3 -m unittest discover -s tests -p test_binary.py -k full_10_gib -v

# Test in Debian 11 without Python installed.
docker run --rm --network none \
  -v "$PWD/dist:/opt/tape-backup:ro" \
  -v "$PWD/tests/binary_smoke.sh:/smoke.sh:ro" \
  debian:bullseye-slim sh /smoke.sh
```

Build output is `dist/tape-backup` and `dist/tape-backup.sha256`; only the executable
needs to be copied to another compatible system. `PYTHON=/path/to/python3` selects
the interpreter for `--local` builds. PyInstaller is isolated in `.venv-build`.

Tests cover streaming full and multiple incremental restores, no disk staging,
metadata, ETA/rates, end-of-medium rollover, short writes, synchronous flush
failures, lost buffered tails, duplicate replay, corruption, incomplete tapes,
chain validation, SSH full/delta restores, host-key verification, remote failures,
connection loss, and standalone binaries at both ends. SSH tests launch a
temporary loopback server with isolated keys/configuration. Automated tape checks
use simulated media; qualify end-of-medium recovery and restore on your drive
and loader with scratch media before relying on them.

Format 3 uses 64 KiB records with checksummed headers and payload frames up to
4 MiB. Frames share a tape file between explicit commits. Archive payloads are
GNU incremental tar streams; the surrounding framing is specific to this tool.
Use this script to restore these volumes, rather than invoking tar directly on
the device.

See [GNU tar incremental semantics](https://www.gnu.org/software/tar/manual/html_node/Incremental-Dumps.html)
and the [Linux SCSI tape driver](https://www.kernel.org/doc/html/latest/scsi/st.html)
for the underlying archive and tape behavior.
