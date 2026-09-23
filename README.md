# Tape backup and restore

`tape_backup.py` creates full and incremental GNU tar archives, writes them across
multiple tape cartridges, verifies them by reading them back, and restores an
ordered backup chain. Linux `/dev/nst0` is the default; `--device /dev/nst1` selects
another drive. There are no third-party Python dependencies.

## Requirements

- Linux, GNU tar, and `mt` from the `mt-st` package. Running the source script
  requires Python 3.11 or newer; the standalone executable includes Python.
- A non-rewinding Linux SCSI tape device supporting variable-length 64 KiB
  records, and permission to read, write, rewind, and unload it.
- Local disk space to stage the complete tar archive before writing tapes.
- Separate source, state, and optional file-media directories.

On Debian/Ubuntu, install dependencies with `sudo apt install python3 tar mt-st`.
Use a filesystem snapshot or quiesce applications while tar reads the source.
Any nonzero tar exit status, including “file changed as we read it,” fails staging
and leaves the previous incremental baseline intact. This is a directory backup
tool; filesystem snapshots and application/database consistency are managed by
the operator. It does not follow symlinks, and it includes mounted directories
beneath the source.

## Standalone Linux executable

Download the Linux x86-64 executable and its checksum from the
[v0.1.0 release](https://github.com/lbadger/tape-backup/releases/tag/v0.1.0):

```bash
curl -fLO https://github.com/lbadger/tape-backup/releases/download/v0.1.0/tape-backup
curl -fLO https://github.com/lbadger/tape-backup/releases/download/v0.1.0/tape-backup.sha256
sha256sum --check tape-backup.sha256
chmod +x tape-backup
./tape-backup --version
```

Build a single executable using Docker with BuildKit:

```bash
./build.sh
./dist/tape-backup --help

# Optional installation; only this executable needs to be copied.
sudo install -m 755 dist/tape-backup /usr/local/bin/tape-backup
```

The output is `dist/tape-backup`, with a SHA-256 checksum in
`dist/tape-backup.sha256`. Verify it from the `dist` directory with
`sha256sum --check tape-backup.sha256`. Build output is ignored by Git.

Replace `python3 tape_backup.py` in the examples below with `tape-backup` (or
`./dist/tape-backup`). All commands and options, including `--device`, are the same:

```bash
./dist/tape-backup backup --source /srv/data --state /var/lib/tape-backup \
  --level full --device /dev/nst0
```

Python and the Python modules are bundled with PyInstaller. **GNU tar and `mt`
remain system dependencies**; on Debian/Ubuntu install `tar mt-st`. Normal Linux
runtime libraries, including glibc and zlib, are also required. Docker and Python
are not needed on the destination machine.

The Docker build uses Debian 11 as a glibc 2.31 baseline and builds for the build
machine's CPU architecture. The binary produced here is Linux x86-64. It targets
glibc 2.31 or newer; Alpine/musl systems require a separate build. PyInstaller's
single executable unpacks its runtime into a temporary directory when launched;
that filesystem must support executable mappings and symlinks. Set `TMPDIR` to a
suitable directory if the default temporary filesystem is mounted `noexec`.
See [PyInstaller's Linux deployment notes](https://pyinstaller.org/en/stable/usage.html#gnu-linux)
for the underlying compatibility constraints.

For a native build without Docker:

```bash
# Requires Python 3.11+, its venv module, pip 22.3+, and binutils.
./build.sh --local
```

This installs pinned PyInstaller in `.venv-build` without changing system Python
packages. `PYTHON=/path/to/python3 ./build.sh --local` selects the build interpreter.
A native build inherits that machine's runtime-library requirements, so build on
the oldest distribution you intend to support. It replaces `dist/tape-backup`.

Test the executable as well as the source:

```bash
TAPE_BACKUP_BINARY="$PWD/dist/tape-backup" python3 -m unittest discover -s tests -v

# Exercise full + incremental restore in Debian 11 without Python installed.
docker run --rm --network none \
  -v "$PWD/dist:/opt/tape-backup:ro" \
  -v "$PWD/tests/binary_smoke.sh:/smoke.sh:ro" \
  debian:bullseye-slim sh /smoke.sh
```

## Full and incremental backups

```bash
# Full backup; /dev/nst0 is the default.
python3 tape_backup.py backup \
  --source /srv/data --state /var/lib/tape-backup --level full

# A subsequent delta, using a different drive.
python3 tape_backup.py backup \
  --source /srv/data --state /var/lib/tape-backup \
  --level incremental --device /dev/nst1

# Inspect the completed baseline and any unfinished backup.
python3 tape_backup.py status --state /var/lib/tape-backup
```

An incremental contains files changed since the last **completed** backup in that
state directory, along with directory metadata needed to reproduce deletions and
renames. Every intermediate delta is required to restore the latest state. A new
full backup starts an independent chain. Use a separate state directory for each
source/backup series.

Successful backup commands print the absolute path of their `catalog.json` to
stdout; progress goes to stderr. Save these paths for restore. The catalog and
tape labels include a generated backup ID and a volume number.

At each media prompt, load the requested writable cartridge and press Enter.
**Writing rewinds and overwrites the loaded cartridge.** Each volume uses one
cartridge; a new backup starts on a fresh cartridge, rather than appending after a
previous backup. Verified cartridges are unloaded automatically. Label and retain
every committed cartridge with its backup ID and volume number.

By default, physical tapes are filled until the drive reports end of medium.
`--volume-size 100GiB` can impose a smaller payload limit per cartridge; actual
end-of-medium detection still applies. This limit excludes the 64 KiB header and
final record padding. Size suffixes are `KiB`, `MiB`, `GiB`, and `TiB`.

## Failure recovery

```bash
python3 tape_backup.py resume \
  --state /var/lib/tape-backup --device /dev/nst0
```

On end of tape, the script closes the tape, rewinds it, and verifies its readable
prefix against the staged archive. Only verified bytes are checkpointed. A lost
buffered tail or a short write is replayed on the next cartridge. Verification
doubles media I/O and adds a rewind per volume.

Other I/O errors stop the command with a nonzero exit status. Fix the problem and
run `resume`. Previously committed volumes are retained; the unfinished volume
is rewritten from its beginning on the requested writable cartridge. A crash
after verification but before the checkpoint may therefore require rewriting
that volume too. Never load a cartridge containing an earlier committed volume
for this write prompt.

If staging was interrupted, `resume` regenerates the archive from the previous
committed snapshot. Once staging completes, resume uses the same staged bytes,
even if the source has subsequently changed or is no longer mounted. New source
changes are picked up by the next incremental backup. A corrupt staged archive
or snapshot is rejected rather than silently skipped.

The incremental snapshot becomes the new baseline only after all volumes have
been verified and the completion checkpoint has been saved. State and media
locks prevent concurrent operations in the same repository; a per-device lock
also serializes physical-drive access by this script under the same Unix account.
Do not run another tape utility or a job under a different account on that drive
at the same time.

## Restore

Pass the full catalog first, followed by **every** incremental catalog through the
desired recovery point, in order. A full-only restore needs just its own catalog.

```bash
python3 tape_backup.py restore \
  --catalog /safe/catalogs/full.json /safe/catalogs/delta-1.json /safe/catalogs/delta-2.json \
  --destination /srv/recovered \
  --work-dir /srv/restore-work \
  --device /dev/nst0
```

The destination must be absent or empty. The work directory must initially be
empty and must be separate from, and on the same filesystem as, the destination.
Budget space for all reassembled tar archives plus the extracted directory tree.
Restore requires the catalogs and tapes; the original source and `.snar`
snapshots are not needed.

The script checks chain continuity, volume identities, SHA-256 checksums, and
archive sizes before extracting. It applies the full archive and then the
incrementals in a private directory, reproducing additions, modifications,
deletions, and renames. When extraction succeeds, it publishes that directory
with an atomic rename. GNU tar preserves permissions, timestamps, links, sparse
files, ACLs, and extended attributes where the filesystem and user permissions
allow. Restoring arbitrary owners and privileged metadata generally requires root.

After an interrupted restore, repeat the **same command** with the same work
directory. Cached volume data is revalidated and reused. An interrupted extraction
is restarted from the full archive in the private directory. Existing destination
files are never merged with a restore. Use trusted catalogs and archives, especially
when restoring as root.

## Keep the catalogs and state

State is stored as:

```text
state/
  index.json                       # Committed baseline and unfinished backup ID
  backups/<backup-id>/
    catalog.json                   # Archive identity, chain, volume hashes/offsets
    snapshot.snar                  # GNU tar incremental snapshot
    archive.tar                    # Staged GNU tar archive
```

**Store copies of completed catalogs somewhere independent of the source and
backup machine. They are required to restore these tape sets.** Protect the state
directory as well: its committed snapshot is required to create the next delta.
Catalogs describe checksums but are not cryptographically signed.

Staged archives are retained deliberately for recovery and inspection. After a
backup is complete and `status` shows no pending job, its `archive.tar` can be
removed to reclaim space. Keep its catalog and snapshot. Never remove or modify
files belonging to a pending backup. Restore work directories can be removed
after successful restoration.

The on-media format is a 64 KiB identifying header followed by 64 KiB records
containing a segment of a GNU tar archive, with zero padding in the last record.
Catalogs record the exact payload length of each verified volume. This is a
versioned tape container, **not GNU tar's native `--multi-volume` format**; use
this script to reassemble tapes. The staged and reassembled `.tar` files are
ordinary GNU incremental tar archives. For example, inspect one with:

```bash
tar --list --incremental --verbose --file archive.tar
```

## Automated loading

Use `--media-command /absolute/path/to/loader` for unattended operation. The
executable receives four arguments:

```text
write|read  BACKUP_ID  VOLUME_NUMBER  DEVICE
```

It must load the requested cartridge, wait until the drive is ready, and return
zero on success. On `write`, that cartridge must be safe to overwrite. The script
then rewinds it and sets variable block mode. The hook runs directly without a
shell, once per requested volume; read-back verification keeps the same cartridge
loaded and does not call the hook again. A failing loader leaves the operation
resumable. Without a hook, prompts use `/dev/tty`; a noninteractive process fails
with instructions to supply a loader.

## File-backed demonstration and tests

`--media-dir` replaces physical cartridges with files using the same record format.
It is mutually exclusive with `--device`. Its default volume payload is 1 GiB.

```bash
python3 tape_backup.py backup \
  --source ./sample-data --state ./demo-state \
  --media-dir ./demo-tapes --volume-size 128KiB

# Use the catalog path printed by the backup command.
python3 tape_backup.py restore \
  --catalog ./demo-state/backups/BACKUP_ID/catalog.json \
  --destination ./demo-restored --work-dir ./demo-restore-work \
  --media-dir ./demo-tapes

python3 -m unittest discover -s tests -v
```

Tests run real GNU tar backup/restore cycles and inject ENOSPC, short writes,
delayed close failures, lost buffered data, corrupt/truncated/wrong volumes,
checkpoint failures, and interrupted extraction. They check multiple deltas,
deletions, renames, unchanged-file omission, sparse files, extended attributes,
links, permissions, drive selection, and resume without the source. Physical tape
hardware has not been exercised by these tests; qualify your drive and loader
with a scratch-media backup and restore before relying on them.

The implementation follows [GNU tar's incremental backup semantics](https://www.gnu.org/software/tar/manual/html_node/Incremental-Dumps.html)
and the Linux [SCSI tape driver's record, close, and end-of-medium behavior](https://www.kernel.org/doc/html/latest/scsi/st.html).
