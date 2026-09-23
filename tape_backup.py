#!/usr/bin/env python3
"""Stream full and incremental GNU tar backups to self-contained tape sets."""

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
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import legacy_v1 as legacy

BackupError = legacy.BackupError
BLOCK_SIZE = 64 * 1024
DEFAULT_BUFFER = 64 * 1024**2
MAX_BUFFER = 256 * 1024**2
MAGIC = b"TAPE-STREAM-2\n"
ZERO_CHAIN = "0" * 64
PROGRAM_VERSION = "0.2.0"
# Linux struct mtop: short operation, padding, int count (x86-64 / AArch64).
MTIOCTOP = 0x40086D01
MTFSF, MTWEOF = 1, 5
RECOVERABLE = (errno.ENOSPC, errno.EIO)


def log(message):
    print(message, file=sys.stderr, flush=True)


def external_env():
    env = os.environ.copy()
    env.pop("TAR_OPTIONS", None)
    if getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise BackupError("Invalid backup ID; use the ID printed on the tape label")
    return value


def encoded_header(fields):
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    envelope = json.dumps({"fields": fields, "sha256": hashlib.sha256(payload).hexdigest()},
                          sort_keys=True, separators=(",", ":")).encode()
    record = MAGIC + envelope + b"\n"
    if len(record) > BLOCK_SIZE:
        raise BackupError("Tape metadata header exceeds 64 KiB")
    return record.ljust(BLOCK_SIZE, b"\0")


def decoded_header(record):
    if record.startswith(legacy.MAGIC):
        raise BackupError("This is a v0.1 tape; use legacy-restore with its catalogs")
    if len(record) != BLOCK_SIZE or not record.startswith(MAGIC):
        raise BackupError("Wrong tape format or corrupt record header")
    try:
        envelope = json.loads(record[len(MAGIC):].rstrip(b"\0\n"))
        fields = envelope["fields"]
        if not isinstance(fields, dict) or encoded_header(fields) != record:
            raise ValueError("metadata checksum mismatch")
        return fields
    except (TypeError, ValueError, KeyError) as exc:
        raise BackupError(f"Corrupt tape header: {exc}") from exc


class EndVolume(Exception):
    pass


class Progress:
    def __init__(self, label, *, transfer=True):
        self.label = label
        self.transfer = transfer
        self.read_bytes = self.written_bytes = self.transferred = 0
        self.phase = "waiting for media"
        self.stop = threading.Event()
        self.started = self.last_time = time.monotonic()
        self.last_bytes = 0
        self.total_bytes = None
        self.eta_base = 0
        self.eta_started = self.started

    def estimate(self, total):
        self.total_bytes = total
        self.eta_base = self.written_bytes
        self.eta_started = time.monotonic()

    def report(self):
        now = time.monotonic()
        if not self.transfer:
            log(f"{self.label}: {self.phase}; {now - self.started:.0f}s elapsed")
            return
        rate = (self.transferred - self.last_bytes) / max(now - self.last_time, 0.001) / 1024**2
        average = self.transferred / max(now - self.started, 0.001) / 1024**2
        done = self.written_bytes - self.eta_base
        if self.phase == "complete":
            eta = "00:00:00"
        elif not self.total_bytes or not done:
            eta = "calculating"
        elif done >= self.total_bytes:
            eta = "finishing (estimate reached)"
        else:
            seconds = int((self.total_bytes - done) * (now - self.eta_started) / done)
            eta = f"~{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        log(f"{self.label}: {self.phase}; {self.read_bytes / 1024**2:.1f} MiB read, "
            f"{self.written_bytes / 1024**2:.1f} MiB delivered; "
            f"{rate:.1f} MiB/s I/O, {average:.1f} MiB/s average; "
            f"ETA {eta} (current archive); {now - self.started:.0f}s elapsed")
        self.last_time, self.last_bytes = now, self.transferred

    def __enter__(self):
        def heartbeat():
            while not self.stop.wait(5):
                self.report()
        self.thread = threading.Thread(target=heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        self.report()


class Volume:
    """Record I/O; physical tape uses synchronous filemarks as commit points."""

    def __init__(self, stream, physical=False):
        self.stream = stream
        self.physical = physical
        self.progress = None

    def write(self, record):
        count = self.stream.write(record)
        if self.progress and count:
            self.progress.transferred += count
        if count != len(record):
            raise OSError(errno.ENOSPC, "Short tape record write")

    def commit(self):
        if self.physical:
            # MTWEOF, unlike MTWEOFI, waits for buffered data to reach the tape.
            fcntl.ioctl(self.stream.fileno(), MTIOCTOP, struct.pack("@hi", MTWEOF, 1))
        else:
            os.fsync(self.stream.fileno())

    def read(self, *, boundary=False):
        try:
            record = self.stream.read(BLOCK_SIZE)
            if not record and boundary and self.physical:
                record = self.stream.read(BLOCK_SIZE)  # Cross one filemark.
        except OSError as exc:
            if exc.errno in RECOVERABLE:
                raise EndVolume from exc
            raise
        if self.progress:
            self.progress.transferred += len(record)
        if len(record) != BLOCK_SIZE:
            raise EndVolume
        return record

    def skip_payload(self, length):
        """Skip file data when loading a snapshot; this does not verify data."""
        if self.physical:
            try:
                fcntl.ioctl(self.stream.fileno(), MTIOCTOP, struct.pack("@hi", MTFSF, 1))
            except OSError as exc:
                if exc.errno in RECOVERABLE:
                    raise EndVolume from exc
                raise
        else:
            size = ((length + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE
            if self.stream.tell() + size > os.fstat(self.stream.fileno()).st_size:
                raise EndVolume
            self.stream.seek(size, os.SEEK_CUR)

    def close(self):
        self.stream.close()


class FileMedia(legacy.FileMedia):
    def load(self, backup_id, number, writing):
        if backup_id is None:
            ids = sorted({p.name.split(".")[0] for p in self.directory.glob("*.0001.tape")})
            if len(ids) != 1:
                raise BackupError("Specify --backup; available IDs: " + ", ".join(ids))
            backup_id = ids[0]
        valid_id(backup_id)
        super().load({"id": backup_id}, number, writing)

    def open(self, writing):
        try:
            stream = open(self.path, "xb" if writing else "rb", buffering=0)
        except FileNotFoundError as exc:
            raise BackupError(f"Incomplete backup: missing tape volume {self.path.name}") from exc
        return Volume(stream)


class TapeMedia(legacy.TapeMedia):
    def mt(self, *args):
        if args == ("rewind",):
            # MTWEOF itself honors the driver's NOWAIT_EOF setting. Disabling
            # both immediate modes is essential before discarding RAM buffers.
            options = Path("/sys/class/scsi_tape") / Path(self.device).name / "options"
            try:
                synchronous = int(options.read_text().strip(), 0) & 0xA000 == 0
            except (OSError, ValueError):
                synchronous = False
            if not synchronous:
                super().mt("stclearoptions", "0xa000")
        super().mt(*args)

    def load(self, backup_id, number, writing):
        try:
            super().load({"id": backup_id or "unknown-backup"}, number, writing)
        except BackupError as exc:
            if "cancelled" in str(exc):
                raise BackupError("Media change cancelled; the streaming operation is incomplete") from exc
            raise

    def open(self, writing):
        return Volume(open(self.device, "wb" if writing else "rb", buffering=0), physical=True)


class StreamWriter:
    def __init__(self, media, job, volume_size, progress):
        self.media, self.job = media, job
        self.volume_size, self.progress = volume_size, progress
        self.volume = None
        self.number = self.sequence = self.used = self.frames = 0
        self.chain = ZERO_CHAIN
        self.no_progress = 0

    def close(self, *, ignore_errors=False):
        if self.volume is not None:
            volume, self.volume = self.volume, None
            try:
                volume.close()
            except OSError:
                if not ignore_errors:
                    raise

    def next_volume(self):
        self.close(ignore_errors=True)
        if self.number:
            self.media.release()
        self.number += 1
        self.progress.phase = f"load volume {self.number}"
        self.media.load(self.job["id"], self.number, True)
        self.volume = self.media.open(True)
        self.volume.progress = self.progress
        self.volume.write(encoded_header({"type": "volume", "format": 2, "backup": self.job,
                                          "volume": self.number, "sequence": self.sequence,
                                          "previous": self.chain}))
        self.volume.commit()
        self.used, self.frames = BLOCK_SIZE, 0
        self.progress.phase = f"streaming to volume {self.number}"
        log(f"Writing {self.job['id']} volume {self.number}, chunk {self.sequence}")

    def send(self, kind, data):
        if not data or len(data) > MAX_BUFFER:
            raise BackupError("Invalid streaming chunk size")
        fields = {"type": "chunk", "sequence": self.sequence, "kind": kind,
                  "length": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                  "previous": self.chain}
        record = encoded_header(fields)
        required = BLOCK_SIZE + ((len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE
        if self.volume_size and required + BLOCK_SIZE > self.volume_size:
            raise BackupError("Chunk cannot fit on the configured volume")
        while True:
            if self.volume is None or (self.volume_size and self.used + required > self.volume_size):
                self.next_volume()
            try:
                self.volume.write(record)
                view = memoryview(data)
                for offset in range(0, len(data), BLOCK_SIZE):
                    block = view[offset:offset + BLOCK_SIZE]
                    self.volume.write(block if len(block) == BLOCK_SIZE else
                                      bytes(block).ljust(BLOCK_SIZE, b"\0"))
                self.progress.phase = f"flushing volume {self.number}, chunk {self.sequence}"
                self.volume.commit()
            except OSError as exc:
                if exc.errno not in RECOVERABLE:
                    raise
                self.no_progress = self.no_progress + 1 if not self.frames else 1
                if self.no_progress >= 3:
                    raise BackupError("Three volumes could not commit a chunk; check drive/media and buffer size") from exc
                log(f"Volume {self.number}: {exc}. Retain this volume; "
                    f"replaying chunk {self.sequence} from RAM on the next tape.")
                self.next_volume()
                continue
            self.sequence += 1
            self.chain = hashlib.sha256(record).hexdigest()
            self.used += required
            self.frames += 1
            self.no_progress = 0
            self.progress.phase = f"streaming to volume {self.number}"
            if kind == "data":
                self.progress.written_bytes += len(data)
            return


class StreamReader:
    def __init__(self, media, backup_id=None, progress=None):
        self.media, self.backup_id, self.progress = media, backup_id, progress
        self.volume = self.job = None
        self.number = self.sequence = 0
        self.chain = ZERO_CHAIN
        self.last_header = self.summary = None

    def close(self):
        if self.volume is not None:
            volume, self.volume = self.volume, None
            volume.close()

    def next_volume(self):
        self.close()
        if self.number:
            self.media.release()
        self.number += 1
        self.media.load(self.backup_id, self.number, False)
        self.volume = self.media.open(False)
        self.volume.progress = self.progress
        try:
            head = decoded_header(self.volume.read(boundary=True))
            job = head["backup"]
            valid_id(job["id"])
            if (head["type"] != "volume" or head["format"] != 2 or head["volume"] != self.number or
                    (self.backup_id is not None and job["id"] != self.backup_id)):
                raise ValueError("wrong backup or volume number")
            if job["level"] not in ("full", "incremental") or not isinstance(job["source"], str):
                raise ValueError("invalid backup metadata")
            if job["level"] == "full" and job["parent"] is not None:
                raise ValueError("full backup has a parent")
            if job["level"] == "incremental":
                valid_id(job["parent"])
            if self.job is not None and job != self.job:
                raise ValueError("volume belongs to a different backup")
            replay = self.last_header is not None and head["sequence"] == self.sequence - 1
            previous = self.last_header["previous"] if replay else self.chain
            if head["sequence"] != self.sequence - int(replay) or head["previous"] != previous:
                raise ValueError("missing or reordered chunks between tapes")
        except (KeyError, TypeError, ValueError, EndVolume) as exc:
            raise BackupError(f"Wrong or incomplete volume {self.number}: {exc}") from exc
        self.job, self.backup_id = job, job["id"]

    def frames(self, *, skip_data=False):
        if self.volume is None:
            self.next_volume()
        data_hash, snapshot_hash = hashlib.sha256(), hashlib.sha256()
        data_bytes = snapshot_bytes = 0
        phase = "data"
        empty_volumes = 0
        while True:
            try:
                record = self.volume.read(boundary=True)
                head = decoded_header(record)
                try:
                    if (head["type"] != "chunk" or head["kind"] not in ("data", "snapshot", "end") or
                            type(head["length"]) is not int or not 0 < head["length"] <= MAX_BUFFER or
                            not re.fullmatch(r"[0-9a-f]{64}", head["sha256"])):
                        raise ValueError("invalid chunk fields")
                    duplicate = head["sequence"] == self.sequence - 1 and head == self.last_header
                    if not duplicate and (head["sequence"] != self.sequence or head["previous"] != self.chain):
                        raise ValueError("missing, reordered or corrupt chunk")
                except (KeyError, TypeError, ValueError) as exc:
                    raise BackupError(f"Invalid chunk header: {exc}") from exc
                if skip_data and head["kind"] == "data":
                    self.volume.skip_payload(head["length"])
                    payload = None
                else:
                    payload = bytearray()
                    remaining = head["length"]
                    while remaining:
                        block = self.volume.read()
                        size = min(BLOCK_SIZE, remaining)
                        if any(block[size:]):
                            raise BackupError("Corrupt chunk padding")
                        payload.extend(block[:size])
                        remaining -= size
                    if hashlib.sha256(payload).hexdigest() != head["sha256"]:
                        raise BackupError(f"Checksum mismatch on volume {self.number}, chunk {head['sequence']}")
            except EndVolume:
                empty_volumes += 1
                if empty_volumes > 3:
                    raise BackupError("Too many volumes without a complete chunk; backup is incomplete")
                self.next_volume()
                continue
            empty_volumes = 0
            if duplicate:
                continue  # A complete chunk can survive a failed filemark flush.
            self.last_header = head
            self.sequence += 1
            self.chain = hashlib.sha256(record).hexdigest()
            if head["kind"] == "data":
                if phase != "data":
                    raise BackupError("File data appears after the incremental snapshot")
                data_bytes += head["length"]
                if payload is not None:
                    data_hash.update(payload)
            elif head["kind"] == "snapshot":
                phase = "snapshot"
                snapshot_bytes += len(payload)
                snapshot_hash.update(payload)
            else:
                try:
                    end = json.loads(payload)
                    if (phase != "snapshot" or not data_bytes or end["data_bytes"] != data_bytes or
                            end["snapshot_bytes"] != snapshot_bytes or
                            end["snapshot_sha256"] != snapshot_hash.hexdigest() or
                            end["chunks"] != self.sequence - 1 or
                            (not skip_data and end["data_sha256"] != data_hash.hexdigest())):
                        raise ValueError("completion checksum or length mismatch")
                except (KeyError, TypeError, ValueError) as exc:
                    raise BackupError(f"Invalid backup completion marker: {exc}") from exc
                self.summary = {**self.job, **end, "volumes": self.number,
                                "data_verified": not skip_data}
                return
            yield head["kind"], payload


@contextmanager
def ram_snapshot():
    fd = os.memfd_create("tape-backup-snapshot", os.MFD_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)


def read_chunks(stream, size, progress=None):
    while True:
        chunk = bytearray()
        while len(chunk) < size:
            block = stream.read(min(1024**2, size - len(chunk)))
            if not block:
                break
            chunk.extend(block)
            if progress:
                progress.read_bytes += len(block)
        if not chunk:
            return
        yield chunk


def stop_process(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            try:
                stream.close()
            except BrokenPipeError:
                pass


def scan(media, backup_id=None, snapshot_fd=None, *, verify=True):
    with Progress("Reading tape metadata" if not verify else "Verifying backup") as progress:
        reader = StreamReader(media, backup_id, progress)
        try:
            progress.phase = "reading tapes"
            for kind, payload in reader.frames(skip_data=not verify):
                if payload is not None:
                    progress.read_bytes += len(payload)
                if kind == "snapshot" and snapshot_fd is not None:
                    with os.fdopen(os.dup(snapshot_fd), "ab", buffering=0) as snapshot:
                        snapshot.write(payload)
            progress.phase = "complete"
            return reader.summary
        finally:
            reader.close()


def estimate_source(source, parent_created=None):
    """Approximate tar size from stat metadata only; never read source payloads."""
    cutoff = datetime.fromisoformat(parent_created).timestamp() if parent_created else None
    total, entries, hardlinks = 10240, 0, set()
    def scan_error(exc):
        raise exc
    with Progress("Source inventory", transfer=False) as progress:
        for directory, subdirs, files in os.walk(source, followlinks=False,
                                                onerror=scan_error):
            total += 2048 + sum(len(os.fsencode(name)) + 2 for name in subdirs + files)
            for name in files + subdirs:
                info = (Path(directory) / name).lstat()
                entries += 1
                total += 2048  # Allow for PAX and regular headers.
                if not stat.S_ISREG(info.st_mode):
                    continue
                if cutoff is not None and max(info.st_mtime, info.st_ctime) < cutoff:
                    continue
                inode = (info.st_dev, info.st_ino)
                if info.st_nlink > 1:
                    if inode in hardlinks:
                        continue
                    hardlinks.add(inode)
                size = min(info.st_size, info.st_blocks * 512)
                total += ((size + 511) // 512) * 512
            progress.phase = f"scanned {entries} entries, approximately {total / 1024**3:.2f} GiB"
        progress.phase = f"estimated {total / 1024**3:.2f} GiB from {entries} entries"
    return ((total + 10239) // 10240) * 10240


def backup(source, media, *, level="full", base=None, volume_size=None,
           buffer_size=DEFAULT_BUFFER, quiet=False):
    source = Path(source).resolve()
    if not source.is_dir():
        raise BackupError(f"Source is not a directory: {source}")
    if isinstance(media, FileMedia) and (legacy.inside(media.directory, source) or
                                        legacy.inside(source, media.directory)):
        raise BackupError("Source and media directories must be separate")
    if level not in ("full", "incremental") or (level == "incremental") != bool(base):
        raise BackupError("Incremental backup requires --base ID; full backup must not use --base")
    if not BLOCK_SIZE <= buffer_size <= MAX_BUFFER:
        raise BackupError("Buffer size must be between 64KiB and 256MiB")
    if volume_size is not None:
        if volume_size < 4 * BLOCK_SIZE:
            raise BackupError("Volume size must be at least 256KiB")
        buffer_size = min(buffer_size, (volume_size // BLOCK_SIZE - 2) * BLOCK_SIZE)
    legacy.require_tar()
    with media.lock(), ram_snapshot() as snapshot_fd:
        parent_created = None
        if base:
            valid_id(base)
            log(f"Loading the incremental snapshot from backup {base}")
            parent = scan(media, base, snapshot_fd, verify=False)
            if parent["source"] != str(source):
                raise BackupError("Incremental source differs from the parent backup")
            parent_created = parent["created"]
            media.release()
        log("Estimating archive size from file metadata; file contents are not staged")
        estimated_bytes = estimate_source(source, parent_created)
        job = {"id": uuid.uuid4().hex, "level": level, "parent": base, "source": str(source),
               "created": datetime.now(timezone.utc).isoformat(), "estimated_bytes": estimated_bytes}
        process = None
        log(f"Streaming {level} backup {job['id']} from {source}; "
            f"buffer {buffer_size / 1024**2:g} MiB; no disk archive or state directory")
        with Progress(job["id"]) as progress:
            progress.estimate(estimated_bytes)
            writer = StreamWriter(media, job, volume_size, progress)
            try:
                writer.next_volume()  # Load the tape before starting the source scan.
                args = ["tar", "--create", "--format=posix", "--acls", "--xattrs", "--sparse",
                        "--numeric-owner", f"--listed-incremental=/proc/self/fd/{snapshot_fd}",
                        "--file=-", f"--directory={source}", *( [] if quiet else ["--verbose"]), "--", "."]
                process = subprocess.Popen(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                           env=external_env(), pass_fds=(snapshot_fd,))
                data_hash = hashlib.sha256()
                for chunk in read_chunks(process.stdout, buffer_size, progress):
                    writer.send("data", chunk)
                    data_hash.update(chunk)
                if process.wait():
                    raise BackupError("GNU tar failed; this tape set has no completion marker and must be restarted")
                progress.phase = "writing incremental snapshot to tape"
                os.lseek(snapshot_fd, 0, os.SEEK_SET)
                snapshot_hash, snapshot_bytes = hashlib.sha256(), 0
                with os.fdopen(os.dup(snapshot_fd), "rb", buffering=0) as snapshot:
                    for chunk in read_chunks(snapshot, buffer_size):
                        writer.send("snapshot", chunk)
                        snapshot_hash.update(chunk)
                        snapshot_bytes += len(chunk)
                if not snapshot_bytes:
                    raise BackupError("GNU tar did not create its incremental snapshot")
                end = {"data_bytes": progress.read_bytes, "data_sha256": data_hash.hexdigest(),
                       "snapshot_bytes": snapshot_bytes, "snapshot_sha256": snapshot_hash.hexdigest(),
                       "chunks": writer.sequence}
                writer.send("end", json.dumps(end, sort_keys=True).encode())
                writer.close()
                media.release()
                progress.phase = "complete"
                log(f"Completed {job['id']}: {progress.read_bytes} archive bytes, {writer.number} volume(s)")
                return job["id"]
            finally:
                stop_process(process)
                writer.close(ignore_errors=True)


def restore(backup_ids, destination, media, *, quiet=False):
    legacy.require_tar()
    if not backup_ids:
        raise BackupError("Specify the full backup ID followed by all incremental IDs")
    for backup_id in backup_ids:
        valid_id(backup_id)
    if len(set(backup_ids)) != len(backup_ids):
        raise BackupError("Duplicate backup ID in restore chain")
    requested = Path(destination).absolute()
    destination = requested.parent.resolve() / requested.name
    legacy.empty_destination(destination)
    if isinstance(media, FileMedia) and (legacy.inside(media.directory, destination) or
                                        legacy.inside(destination, media.directory)):
        raise BackupError("Restore destination and media must be separate")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Only restored files occupy disk. No intermediate archive or restore journal.
    tree = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restoring-", dir=destination.parent))
    log(f"Extracting into {tree}; destination is published only after verification")
    try:
        with media.lock(), Progress("Restore") as progress:
            parent = None
            for backup_id in backup_ids:
                reader, process = StreamReader(media, backup_id, progress), None
                try:
                    reader.next_volume()
                    job = reader.job
                    if parent is None:
                        if job["level"] != "full" or job["parent"] is not None:
                            raise BackupError("Restore chain must start with a full backup")
                    elif (job["level"] != "incremental" or job["parent"] != parent["id"] or
                          job["source"] != parent["source"]):
                        raise BackupError("Missing parent or out-of-order incremental backup")
                    log(f"Restoring {backup_id} ({job['level']}) directly from tape")
                    progress.phase = f"extracting {backup_id}"
                    progress.estimate(job.get("estimated_bytes"))
                    process = subprocess.Popen(["tar", "--extract", "--listed-incremental=/dev/null",
                                                "--acls", "--xattrs", "--numeric-owner", "--file=-",
                                                f"--directory={tree}", *([] if quiet else ["--verbose"])],
                                               stdin=subprocess.PIPE, stdout=2, env=external_env())
                    for kind, payload in reader.frames():
                        if kind == "data":
                            process.stdin.write(payload)
                            progress.read_bytes += len(payload)
                            progress.written_bytes += len(payload)
                    process.stdin.close()
                    if process.wait():
                        raise BackupError("GNU tar extraction failed; destination was not published")
                    parent = job
                finally:
                    stop_process(process)
                    reader.close()
                media.release()
            progress.phase = "flushing restored files"
            os.sync()
            legacy.empty_destination(destination)
            os.replace(tree, destination)
            legacy.fsync_dir(destination.parent)
            progress.phase = "complete"
        return destination
    finally:
        if tree.exists():
            shutil.rmtree(tree)


def parse_size(value):
    match = re.fullmatch(r"([0-9]+)(KiB|MiB|GiB|TiB)?", value)
    if not match:
        raise argparse.ArgumentTypeError("Use bytes or an integer with KiB/MiB/GiB/TiB")
    size = int(match[1]) * {None: 1, "KiB": 1024, "MiB": 1024**2,
                            "GiB": 1024**3, "TiB": 1024**4}[match[2]]
    if size <= 0:
        raise argparse.ArgumentTypeError("Size must be positive")
    return size


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    def media_options(command):
        group = command.add_mutually_exclusive_group()
        group.add_argument("--device", default="/dev/nst0", help="Tape device (default: /dev/nst0)")
        group.add_argument("--media-dir", type=Path, help="Use files as simulated tape volumes")
        command.add_argument("--media-command", help="Loader executable; arguments: write|read ID NUMBER DEVICE")

    create = commands.add_parser("backup", help="Stream source files directly to tape")
    create.add_argument("--source", required=True, type=Path)
    create.add_argument("--level", choices=("full", "incremental"), default="full")
    create.add_argument("--base", help="Previous backup ID, required for incremental backups; load its tapes first")
    create.add_argument("--buffer-size", type=parse_size, default=DEFAULT_BUFFER, help="RAM retry buffer (default: 64MiB)")
    create.add_argument("--volume-size", type=parse_size, help="Optional cap on record bytes per tape; default: until full")
    create.add_argument("--quiet", action="store_true", help="Suppress file names; keep progress and transfer rates")
    media_options(create)
    extract = commands.add_parser("restore", help="Stream tapes directly into a restored directory")
    extract.add_argument("--backup", nargs="+", required=True, help="Full backup ID followed by every incremental ID")
    extract.add_argument("--destination", required=True, type=Path)
    extract.add_argument("--quiet", action="store_true", help="Suppress file names; keep progress and transfer rates")
    media_options(extract)
    for name in ("inspect", "verify"):
        command = commands.add_parser(name, help="Read metadata from tapes" if name == "inspect" else "Verify all tape data")
        command.add_argument("--backup", help="Backup ID; can be discovered from the first tape")
        media_options(command)
    old = commands.add_parser("legacy-restore", help="Restore v0.1 tapes using their external catalogs")
    old.add_argument("--catalog", nargs="+", required=True, type=Path)
    old.add_argument("--destination", required=True, type=Path)
    old.add_argument("--work-dir", required=True, type=Path)
    media_options(old)
    return parser


def main(argv=None):
    args = make_parser().parse_args(argv)
    os.umask(0o077)
    try:
        if args.media_dir and args.media_command:
            raise BackupError("--media-command only applies to physical tapes")
        if args.command == "legacy-restore":
            media = legacy.FileMedia(args.media_dir) if args.media_dir else legacy.TapeMedia(args.device, args.media_command)
            print(legacy.restore(args.catalog, args.destination, args.work_dir, media))
            return 0
        media = FileMedia(args.media_dir) if args.media_dir else TapeMedia(args.device, args.media_command)
        if args.command == "backup":
            size = args.volume_size or (1024**3 if args.media_dir else None)
            result = backup(args.source, media, level=args.level, base=args.base,
                            volume_size=size, buffer_size=args.buffer_size, quiet=args.quiet)
        elif args.command == "restore":
            result = restore(args.backup, args.destination, media, quiet=args.quiet)
        else:
            if args.backup:
                valid_id(args.backup)
            with media.lock():
                result = scan(media, args.backup, verify=args.command == "verify")
                media.release()
            result = json.dumps(result, indent=2)
        print(result)
        return 0
    except (BackupError, OSError) as exc:
        log(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        log("Interrupted. Restart the streaming backup with fresh tapes, or repeat restore from the first tape.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
