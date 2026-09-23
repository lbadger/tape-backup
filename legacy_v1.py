#!/usr/bin/env python3
"""Resumable, verified multi-volume GNU tar backups for Linux tape drives."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid


BLOCK_SIZE = 64 * 1024
MAGIC = b"TAPE-ARCHIVE-1\n"
VERSION = 1
PROGRAM_VERSION = "0.1.0"


class BackupError(Exception):
    """An actionable backup or restore failure."""


def log(message):
    print(message, file=sys.stderr, flush=True)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, data):
    """Publish a checkpoint only after its contents reach stable storage."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    try:
        with open(path) as stream:
            return json.load(stream)
    except (ValueError, OSError) as exc:
        raise BackupError(f"Cannot read checkpoint {path}: {exc}") from exc


def digest_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sync_file(path):
    with open(path, "rb") as stream:
        os.fsync(stream.fileno())


@contextmanager
def locked(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / ".lock", "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError(f"Another operation is using {directory}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def run_command(args):
    env = os.environ.copy()
    env.pop("TAR_OPTIONS", None)
    if getattr(sys, "frozen", False):
        # PyInstaller prepends its bundled libraries to LD_LIBRARY_PATH. tar,
        # mt and media loaders must use the host's original library search path.
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    result = subprocess.run(args, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise BackupError(f"{args[0]} failed ({result.returncode}): "
                          f"{result.stderr.strip() or result.stdout.strip()}")
    if result.stderr.strip():
        log(result.stderr.strip())
    return result.stdout


def require_tar():
    if "GNU tar" not in run_command(["tar", "--version"]):
        raise BackupError("GNU tar is required for incremental archives")


def inside(path, parent):
    return path == parent or parent in path.parents


def check_source_paths(source, state, media, *, require_source=True):
    if require_source and not source.is_dir():
        raise BackupError(f"Source is not a directory: {source}")
    for path in [state] + ([media.directory] if isinstance(media, FileMedia) else []):
        if inside(path, source) or inside(source, path):
            raise BackupError("Source must be separate from state and media directories")
    if isinstance(media, FileMedia) and (inside(state, media.directory) or
                                        inside(media.directory, state)):
        raise BackupError("State and media directories must be separate")


class FileMedia:
    """Regular files with the same record format as physical tape volumes."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.path = None

    def load(self, job, number, writing):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / f"{job['id']}.{number:04d}.tape"

    @contextmanager
    def writer(self):
        with open(self.path, "wb", buffering=0) as stream:
            yield stream
            os.fsync(stream.fileno())
        fsync_dir(self.directory)

    def reader(self):
        return open(self.path, "rb", buffering=0)

    def release(self):
        pass

    @contextmanager
    def lock(self):
        with locked(self.directory):
            yield


class TapeMedia:
    def __init__(self, device="/dev/nst0", media_command=None):
        self.device = str(Path(device).resolve())
        self.media_command = media_command
        try:
            info = os.stat(self.device)
        except OSError as exc:
            raise BackupError(f"Cannot access tape device {self.device}: {exc}") from exc
        if not stat.S_ISCHR(info.st_mode):
            raise BackupError("--device must be a Linux SCSI tape character device")
        if os.major(info.st_rdev) != 9 or not (os.minor(info.st_rdev) & 128):
            raise BackupError("Use a non-rewinding Linux SCSI tape device, e.g. /dev/nst0")
        if not shutil.which("mt"):
            raise BackupError("The mt command is required (install the mt-st package)")
        self.device_number = info.st_rdev

    def mt(self, *args):
        run_command(["mt", "-f", self.device, *args])

    def load(self, job, number, writing):
        action = "write" if writing else "read"
        label = f"{job['id']} volume {number}"
        if self.media_command:
            run_command([self.media_command, action, job["id"], str(number), self.device])
        else:
            warning = " (CONTENTS WILL BE OVERWRITTEN)" if writing else ""
            try:
                with open("/dev/tty", "r+") as tty:
                    tty.write(f"Load {label} into {self.device} for {action}{warning}.\n"
                              "Press Enter when ready, or type q to stop: ")
                    tty.flush()
                    response = tty.readline()
            except OSError as exc:
                raise BackupError("No terminal for tape changes; use --media-command") from exc
            if not response or response.strip().lower() == "q":
                raise BackupError("Media change cancelled; use resume to continue")
        self.mt("rewind")
        self.mt("setblk", "0")

    def writer(self):
        # Unbuffered Python I/O preserves physical 64 KiB tape records. Closing
        # writes the filemark and reports delayed drive-buffer errors.
        return open(self.device, "wb", buffering=0)

    def reader(self):
        self.mt("rewind")
        return open(self.device, "rb", buffering=0)

    def release(self):
        self.mt("offline")

    @contextmanager
    def lock(self):
        # Different state repositories using the same drive and Unix account
        # must still serialize. mt needs the actual tape descriptor closed.
        directory = Path.home() / ".cache" / "tape-backup" / str(self.device_number)
        with locked(directory):
            yield


def header(job, number, offset):
    fields = {key: job[key] for key in
              ("id", "parent", "level", "archive_size", "archive_sha256")}
    fields.update(version=VERSION, number=number, offset=offset, block_size=BLOCK_SIZE)
    data = MAGIC + json.dumps(fields, sort_keys=True).encode() + b"\n"
    if len(data) > BLOCK_SIZE:
        raise BackupError("Volume header exceeds the record size")
    return data.ljust(BLOCK_SIZE, b"\0")


def write_record(stream, data):
    if stream.write(data) != len(data):
        # A short tape write cannot be completed by another write: that would
        # create a second physical record. Replay this record on the next tape.
        raise OSError(errno.ENOSPC, "Short tape write")


def write_volume(job, archive, media, number, offset):
    """Write and read back one volume; only return proven durable bytes."""
    media.load(job, number, True)
    expected_header = header(job, number, offset)
    limit = min(job["volume_size"] or job["archive_size"], job["archive_size"] - offset)
    written = 0
    eom = False
    with open(archive, "rb") as source:
        source.seek(offset)
        try:
            with media.writer() as tape:
                write_record(tape, expected_header)
                while written < limit:
                    data = source.read(min(BLOCK_SIZE, limit - written))
                    if not data:
                        raise BackupError("Staged archive was truncated during writing")
                    write_record(tape, data.ljust(BLOCK_SIZE, b"\0"))
                    written += len(data)
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            eom = True
            log(f"End of tape at volume {number}; verifying its readable prefix")

        if not written:
            raise BackupError("Volume has no complete data records; replace the tape and resume")

        source.seek(offset)
        verified = 0
        digest = hashlib.sha256()
        with media.reader() as tape:
            if tape.read(BLOCK_SIZE) != expected_header:
                raise BackupError(f"Volume {number} header failed read-back verification")
            while verified < written:
                size = min(BLOCK_SIZE, written - verified)
                expected = source.read(size)
                try:
                    actual = tape.read(BLOCK_SIZE)
                except OSError as exc:
                    # At EOM a buffered tail can be lost despite write success.
                    # Only an already verified prefix can become a checkpoint.
                    if eom and exc.errno in (errno.EIO, errno.ENOSPC):
                        break
                    raise
                if len(actual) != BLOCK_SIZE:
                    if eom:
                        break
                    raise BackupError(f"Volume {number} is truncated during verification")
                if actual != expected.ljust(BLOCK_SIZE, b"\0"):
                    raise BackupError(f"Volume {number} failed read-back verification")
                digest.update(expected)
                verified += size
        if not verified:
            raise BackupError("No data survived verification; replace the tape and resume")
    return {"number": number, "offset": offset, "size": verified,
            "sha256": digest.hexdigest()}


def index_for(state):
    path = state / "index.json"
    if path.exists():
        index = read_json(path)
        if index.get("version") != VERSION:
            raise BackupError("Unsupported state version")
        return index
    return {"version": VERSION, "head": None, "pending": None}


def job_path(state, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise BackupError("Invalid backup ID")
    return state / "backups" / job_id


def prepare_job(state, job):
    directory = job_path(state, job["id"])
    snapshot = directory / "snapshot.snar"
    archive = directory / "archive.tar"
    # Restart an interrupted tar creation from its committed parent snapshot.
    snapshot.unlink(missing_ok=True)
    archive.unlink(missing_ok=True)
    if job["parent"]:
        parent_dir = job_path(state, job["parent"])
        parent = read_json(parent_dir / "catalog.json")
        if digest_file(parent_dir / "snapshot.snar") != parent["snapshot_sha256"]:
            raise BackupError("Parent incremental snapshot is corrupt")
        shutil.copyfile(parent_dir / "snapshot.snar", snapshot)
    log(f"Staging {job['level']} backup {job['id']} from {job['source']}")
    run_command(["tar", "--create", "--format=posix", "--acls", "--xattrs",
                 "--sparse", "--numeric-owner", f"--listed-incremental={snapshot}",
                 f"--file={archive}", f"--directory={job['source']}", "--", "."])
    sync_file(archive)
    sync_file(snapshot)
    job.update(archive_size=archive.stat().st_size, archive_sha256=digest_file(archive),
               snapshot_sha256=digest_file(snapshot), status="ready")
    atomic_json(directory / "catalog.json", job)


def continue_job(state, index, media):
    directory = job_path(state, index["pending"])
    job = read_json(directory / "catalog.json")
    if job["id"] != index["pending"] or job["status"] not in ("preparing", "ready", "complete"):
        raise BackupError("Invalid pending backup checkpoint")
    check_source_paths(Path(job["source"]), state, media,
                       require_source=job["status"] == "preparing")
    if job["status"] == "preparing":
        prepare_job(state, job)
    validate_catalog(job, require_complete=False)
    archive = directory / "archive.tar"
    if (archive.stat().st_size != job["archive_size"] or
            digest_file(archive) != job["archive_sha256"] or
            digest_file(directory / "snapshot.snar") != job["snapshot_sha256"]):
        raise BackupError("Staged archive or snapshot is corrupt; checkpoint was not advanced")
    offset = sum(volume["size"] for volume in job["volumes"])
    while offset < job["archive_size"]:
        number = len(job["volumes"]) + 1
        log(f"Writing {job['id']} volume {number}, archive offset {offset}")
        volume = write_volume(job, archive, media, number, offset)
        job["volumes"].append(volume)
        atomic_json(directory / "catalog.json", job)
        offset += volume["size"]
        log(f"Verified volume {number}: {offset}/{job['archive_size']} archive bytes")
        media.release()
    job["status"] = "complete"
    atomic_json(directory / "catalog.json", job)
    # One atomic pointer publishes both the new baseline and completion. A
    # crash before this update leaves a resumable pending job, even if complete.
    index.update(head=job["id"], pending=None)
    atomic_json(state / "index.json", index)
    return directory / "catalog.json"


def backup(state, source, level, media, volume_size=None):
    state, source = Path(state).resolve(), Path(source).resolve()
    check_source_paths(source, state, media)
    if level not in ("full", "incremental"):
        raise BackupError("Backup level must be full or incremental")
    if volume_size is not None and volume_size < BLOCK_SIZE:
        raise BackupError(f"Volume size must be at least {BLOCK_SIZE} bytes")
    require_tar()
    with locked(state), media.lock():
        index = index_for(state)
        if index["pending"]:
            raise BackupError("An unfinished backup exists; run resume first")
        parent = index["head"] if level == "incremental" else None
        if level == "incremental" and not parent:
            raise BackupError("An incremental backup requires a completed full backup")
        if parent:
            previous = read_json(job_path(state, parent) / "catalog.json")
            validate_catalog(previous)
            if previous["source"] != str(source):
                raise BackupError("Incremental source differs from its full backup")
        job = {"version": VERSION, "id": uuid.uuid4().hex, "parent": parent,
               "level": level, "source": str(source), "status": "preparing",
               "created": datetime.now(timezone.utc).isoformat(),
               "volume_size": volume_size, "volumes": []}
        directory = job_path(state, job["id"])
        directory.mkdir(parents=True, mode=0o700)
        fsync_dir(directory.parent)
        fsync_dir(state)
        atomic_json(directory / "catalog.json", job)
        index["pending"] = job["id"]
        atomic_json(state / "index.json", index)
        return continue_job(state, index, media)


def resume(state, media):
    state = Path(state).resolve()
    require_tar()
    with locked(state), media.lock():
        index = index_for(state)
        if not index["pending"]:
            raise BackupError("There is no unfinished backup to resume")
        return continue_job(state, index, media)


def validate_catalog(job, *, require_complete=True):
    """Reject incomplete chains, missing volumes and malformed offsets up front."""
    try:
        allowed = ("complete",) if require_complete else ("ready", "complete")
        if job["version"] != VERSION or job["status"] not in allowed:
            raise ValueError("catalog is incomplete or has an unsupported version")
        job_path(Path("."), job["id"])
        if job["level"] not in ("full", "incremental"):
            raise ValueError("invalid backup level")
        if job["level"] == "full" and job["parent"] is not None:
            raise ValueError("a full backup cannot have an incremental parent")
        if job["level"] == "incremental":
            job_path(Path("."), job["parent"])
        if not isinstance(job["source"], str):
            raise ValueError("invalid source")
        if type(job["archive_size"]) is not int or job["archive_size"] <= 0:
            raise ValueError("invalid archive size")
        offset = 0
        for number, volume in enumerate(job["volumes"], 1):
            if (volume["number"] != number or volume["offset"] != offset or
                    type(volume["size"]) is not int or volume["size"] <= 0):
                raise ValueError("missing, reordered or invalid volumes")
            if not re.fullmatch(r"[0-9a-f]{64}", volume["sha256"]):
                raise ValueError("invalid volume checksum")
            offset += volume["size"]
        if offset > job["archive_size"] or (job["status"] == "complete" and
                                           offset != job["archive_size"]):
            raise ValueError("volume sizes do not match archive size")
        if not re.fullmatch(r"[0-9a-f]{64}", job["archive_sha256"]):
            raise ValueError("invalid archive checksum")
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError(f"Invalid catalog: {exc}") from exc


def load_chain(catalogs):
    jobs = [read_json(path) for path in catalogs]
    if not jobs:
        raise BackupError("At least one catalog is required")
    previous = None
    seen = set()
    for job in jobs:
        validate_catalog(job)
        if job["id"] in seen:
            raise BackupError("Duplicate backup in restore chain")
        seen.add(job["id"])
        if previous is None:
            if job["level"] != "full" or job["parent"] is not None:
                raise BackupError("Restore chain must start with a full backup")
        elif (job["level"] != "incremental" or job["parent"] != previous["id"] or
              job["source"] != previous["source"]):
            raise BackupError("Incremental chain is missing a parent or is out of order")
        previous = job
    return jobs


def digest_region(stream, size):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        data = stream.read(min(1024 * 1024, remaining))
        if not data:
            return None
        digest.update(data)
        remaining -= len(data)
    return digest.hexdigest()


def recover_archive(job, work, media):
    path = work / f"{job['id']}.tar"
    if not path.exists():
        path.touch(mode=0o600)
    with open(path, "r+b") as archive:
        for volume in job["volumes"]:
            offset, size = volume["offset"], volume["size"]
            archive.seek(offset)
            if digest_region(archive, size) == volume["sha256"]:
                continue  # Locally cached and revalidated after an interruption.
            archive.seek(offset)
            archive.truncate()
            media.load(job, volume["number"], False)
            digest = hashlib.sha256()
            try:
                with media.reader() as tape:
                    if tape.read(BLOCK_SIZE) != header(job, volume["number"], offset):
                        raise BackupError(f"Wrong or corrupt tape: expected {job['id']} "
                                          f"volume {volume['number']}")
                    remaining = size
                    while remaining:
                        record = tape.read(BLOCK_SIZE)
                        if len(record) != BLOCK_SIZE:
                            raise BackupError(f"Truncated tape volume {volume['number']}")
                        data = record[:min(BLOCK_SIZE, remaining)]
                        archive.write(data)
                        digest.update(data)
                        remaining -= len(data)
                if digest.hexdigest() != volume["sha256"]:
                    raise BackupError(f"Checksum mismatch on volume {volume['number']}")
                archive.flush()
                os.fsync(archive.fileno())
            except BaseException:
                archive.seek(offset)
                archive.truncate()
                raise
            media.release()
        archive.truncate(job["archive_size"])
        archive.flush()
        os.fsync(archive.fileno())
    if digest_file(path) != job["archive_sha256"]:
        raise BackupError("Reassembled archive checksum does not match the catalog")
    return path


def empty_destination(destination):
    if destination.is_symlink() or (destination.exists() and
            (not destination.is_dir() or any(destination.iterdir()))):
        raise BackupError("Restore destination must be absent or an empty directory")


def restore(catalogs, destination, work, media):
    require_tar()
    jobs = load_chain(catalogs)
    # Preserve the final component for symlink rejection.
    requested = Path(destination).absolute()
    destination = requested.parent.resolve() / requested.name
    work = Path(work).resolve()
    if inside(work, destination) or inside(destination, work):
        raise BackupError("Restore work and destination directories must be separate")
    if isinstance(media, FileMedia) and (inside(media.directory, work) or
            inside(work, media.directory) or inside(media.directory, destination) or
            inside(destination, media.directory)):
        raise BackupError("Restore media must be separate from work and destination")
    identity = {"destination": str(destination), "catalogs": jobs}
    with locked(work), media.lock():
        journal_path = work / "restore.json"
        if journal_path.exists():
            journal = read_json(journal_path)
            if journal["identity"] != identity:
                raise BackupError("Restore work directory belongs to a different restore")
        else:
            if any(path.name != ".lock" for path in work.iterdir()):
                raise BackupError("Use an empty work directory for a new restore")
            empty_destination(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if work.stat().st_dev != destination.parent.stat().st_dev:
                raise BackupError("Work and destination must be on the same filesystem")
            journal = {"identity": identity, "phase": "assembling"}
            atomic_json(journal_path, journal)
        tree = work / "tree"
        if journal["phase"] == "done":
            if not destination.is_dir():
                raise BackupError("Restore was completed, but its destination is missing")
            return destination
        if journal["phase"] != "publishing":
            empty_destination(destination)
            archives = [recover_archive(job, work, media) for job in jobs]
            journal["phase"] = "extracting"
            atomic_json(journal_path, journal)
            if tree.exists():
                shutil.rmtree(tree)
            tree.mkdir(mode=0o700)
            for archive in archives:
                log(f"Extracting {archive.name}")
                run_command(["tar", "--extract", "--listed-incremental=/dev/null",
                             "--acls", "--xattrs", "--numeric-owner",
                             f"--file={archive}", f"--directory={tree}"])
            # syncfs would suffice; os.sync is available on the supported Linux
            # platform and makes extracted data durable before publication.
            os.sync()
            journal["phase"] = "publishing"
            atomic_json(journal_path, journal)
        if tree.exists():
            empty_destination(destination)
            os.replace(tree, destination)
            fsync_dir(destination.parent)
            fsync_dir(work)
        elif not destination.is_dir():
            raise BackupError("Interrupted publication has no restored directory")
        journal["phase"] = "done"
        atomic_json(journal_path, journal)
    return destination


def parse_size(value):
    match = re.fullmatch(r"([0-9]+)(KiB|MiB|GiB|TiB)?", value)
    if not match:
        raise argparse.ArgumentTypeError("Use bytes or an integer with KiB/MiB/GiB/TiB")
    size = int(match[1]) * {None: 1, "KiB": 1024, "MiB": 1024**2,
                            "GiB": 1024**3, "TiB": 1024**4}[match[2]]
    if size < BLOCK_SIZE:
        raise argparse.ArgumentTypeError("Volume size must be at least 64KiB")
    return size


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    def media_options(command):
        group = command.add_mutually_exclusive_group()
        group.add_argument("--device", default="/dev/nst0", help="Tape device (default: /dev/nst0)")
        group.add_argument("--media-dir", type=Path, help="Use volume files instead of physical tapes")
        command.add_argument("--media-command", help="Executable to load tapes; arguments: write|read ID NUMBER DEVICE")

    create = commands.add_parser("backup", help="Create a full or incremental backup")
    create.add_argument("--state", required=True, type=Path)
    create.add_argument("--source", required=True, type=Path)
    create.add_argument("--level", choices=("full", "incremental"), default="full")
    create.add_argument("--volume-size", type=parse_size,
                        help="Maximum archive bytes per volume; default: until end of tape (files: 1GiB)")
    media_options(create)
    restart = commands.add_parser("resume", help="Resume an unfinished backup")
    restart.add_argument("--state", required=True, type=Path)
    media_options(restart)
    extract = commands.add_parser("restore", help="Restore a full backup and its ordered increments")
    extract.add_argument("--catalog", nargs="+", required=True, type=Path)
    extract.add_argument("--destination", required=True, type=Path)
    extract.add_argument("--work-dir", required=True, type=Path)
    media_options(extract)
    status = commands.add_parser("status", help="Show the current backup and pending job")
    status.add_argument("--state", required=True, type=Path)
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    # Catalogs, tar payloads and snapshots may contain private source data.
    os.umask(0o077)
    try:
        if args.command == "status":
            state = args.state.resolve()
            index = index_for(state)
            result = {**index, "backups": {}}
            for job_id in (index["head"], index["pending"]):
                if job_id:
                    result["backups"][job_id] = read_json(job_path(state, job_id) / "catalog.json")
            print(json.dumps(result, indent=2))
            return 0
        if args.media_dir and args.media_command:
            raise BackupError("--media-command applies only to physical tape drives")
        media = FileMedia(args.media_dir) if args.media_dir else TapeMedia(args.device, args.media_command)
        if args.command == "backup":
            size = args.volume_size
            if size is None and args.media_dir:
                size = 1024**3
            result = backup(args.state, args.source, args.level, media, size)
        elif args.command == "resume":
            result = resume(args.state, media)
        else:
            result = restore(args.catalog, args.destination, args.work_dir, media)
        print(result)
        return 0
    except (BackupError, OSError) as exc:
        log(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        log("Interrupted. Backup: run resume. Restore: repeat the same restore command.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
