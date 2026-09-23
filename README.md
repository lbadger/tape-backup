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

Download the executable and checksum from the
[v0.3.0 release](https://github.com/lbadger/tape-backup/releases/tag/v0.3.0):

```bash
curl -fLO https://github.com/lbadger/tape-backup/releases/download/v0.3.0/tape-backup
curl -fLO https://github.com/lbadger/tape-backup/releases/download/v0.3.0/tape-backup.sha256
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
and filemark modes when necessary; changing those driver settings may require
root. Run it with exclusive access to the drive. Locks prevent competing jobs
under the same Unix account, but cannot exclude other tape programs or accounts.

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
writable tapes for the new backup. The metadata reader uses tape filemarks to
skip archive payloads, avoiding transfer of the old file contents through RAM.
Mechanical tape traversal still takes time. No persistent snapshot/cache file is
required. Metadata memory consumption grows with the number of filenames.

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
BACKUP_ID: streaming to volume 1; 8192.0 MiB read, 8128.0 MiB delivered; 155.0 MiB/s I/O, 149.3 MiB/s average; ETA ~02:14:08 (current archive); 55s elapsed
```

I/O rates measure bytes passed to/from the device, including framing, padding,
and retries. They are host-side rates, not measurements of physical tape motion
or compressed media capacity. Delivered backup bytes have passed a synchronous
filemark flush. Restore delivery counts archive bytes passed to GNU tar. The
average includes elapsed media-change time.

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

The default retry chunk is 64 MiB; adjust it with `--buffer-size`, between 64 KiB
and 256 MiB. The payload buffer is bounded independently of archive size. Peak
memory also includes Python/GNU tar overhead, temporary chunk copies, and the
incremental snapshot metadata. The volume cap can reduce the effective chunk
size. Larger chunks reduce filemark overhead; smaller chunks reduce RAM use and
retry work.

Each chunk has an identifier, length, checksum, and link to the preceding chunk.
The writer retains it in RAM until a synchronous tape filemark commits it. On a
short write, end-of-tape indication, or write I/O error, it requests another tape
and replays the pending chunk. **Keep the earlier volume**, including one that
ended in a write error: it may contain previously committed data. Restore handles
an incomplete tail or a complete duplicate chunk after a failed flush. Repeated
failures without progress stop the job.

A final completion marker is written only after GNU tar exits successfully and
the new incremental snapshot has been recorded. Missing chunks, wrong tapes,
checksum failures, and incomplete sets are rejected. Backup flushes each chunk
but does not perform automatic read-back verification; use `verify` for a full
read pass.

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

This release reads streaming tapes created by v0.2 and v0.3. The legacy restore
command and disk-staging implementation have been removed. To restore v0.1 tapes,
use the [v0.1.0 release](https://github.com/lbadger/tape-backup/releases/tag/v0.1.0)
with their original catalogs. Start a new full backup when migrating from v0.1.

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
temporary loopback server with isolated keys/configuration. Physical tape hardware
has not been exercised: qualify your drive and loader with scratch media before
relying on it.

The format uses 64 KiB records with checksummed headers; each volume header and
each chunk is a separate tape file. Archive payloads are GNU incremental tar
streams; the surrounding version-2 framing is specific to this tool. Use this
script to restore these volumes, rather than invoking tar directly on the device.

See [GNU tar incremental semantics](https://www.gnu.org/software/tar/manual/html_node/Incremental-Dumps.html)
and the [Linux SCSI tape driver](https://www.kernel.org/doc/html/latest/scsi/st.html)
for the underlying archive and tape behavior.
