#!/usr/bin/env python3
"""Stream file archives or native ZFS snapshots to self-contained tape sets."""

import argparse
import codecs
from collections import deque, OrderedDict
from contextlib import closing, contextmanager
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

BLOCK_SIZE = 64 * 1024
DEFAULT_BUFFER = 1024**3
MAX_BUFFER = 10 * 1024**3
FRAME_SIZE = 4 * 1024**2
POSITION_INTERVAL = 16 * 1024**2
MAGIC = b"TAPE-STREAM-3\n"
ZFS_MAGIC = b"TAPE-STREAM-4\n"
ZERO_CHAIN = "0" * 64
PROGRAM_VERSION = "2.0.0"
# Linux struct mtop: short operation, padding, int count (x86-64 / AArch64).
MTIOCTOP = 0x40086D01
MTWEOF = 5
MTFSF, MTREW, MTBSFM, MTEOM, MTSEEK = 1, 6, 10, 12, 22
MTERASE = 13
MTCOMPRESSION = 32
MAX_CATALOG_BYTES = 64 * 1024**2
RECOVERABLE = (errno.ENOSPC, errno.EIO)
SSH_MAGIC = b"TAPE-SSH-3\n"
# Current SSH transport uses a 64-bit payload length.
PACKET_HEADER = struct.Struct("!cQ")
OUTPUT_LOCK = threading.RLock()
DRIVE_LOCK_DIRECTORY = Path('/run/lock')


class BackupError(Exception):
    """An actionable backup or restore failure."""


class MediaNotBlank(BackupError):
    """A continuation cartridge was rejected before any records were written."""


class WrongMedia(BackupError):
    """Readable cartridge does not contain the requested backup volume."""


class DriveBusy(BackupError):
    """Another cooperating process owns the physical drive."""


def log(message):
    with OUTPUT_LOCK:
        print(message, file=sys.stderr, flush=True)


def logged_process(args, *, log_stdout=False, result_stdout=False, **kwargs):
    """Relay child output through the prompt lock with bounded buffering."""
    if result_stdout:
        kwargs['stdout'] = subprocess.PIPE
    if log_stdout:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
        stream = process.stdout
    else:
        process = subprocess.Popen(args, stderr=subprocess.PIPE, **kwargs)
        stream = process.stderr
    process._tape_output_errors = []
    def relay(stream, channel):
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                data = stream.read1(8192)
                message = decoder.decode(data, final=not data)
                if message:
                    with OUTPUT_LOCK:
                        target = getattr(sys, channel)
                        target.write(message)
                        target.flush()
                if not data:
                    break
        except (OSError, ValueError) as exc:
            process._tape_output_errors.append(exc)
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        finally:
            stream.close()
    process._tape_log_thread = threading.Thread(target=relay, args=(stream, 'stderr'),
                                               name='tape-child-logs', daemon=True)
    process._tape_log_thread.start()
    if result_stdout:
        process._tape_result_thread = threading.Thread(target=relay, args=(process.stdout, 'stdout'),
                                                       name='tape-child-results', daemon=True)
        process._tape_result_thread.start()
    return process


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


def run_command(args):
    result = subprocess.run(args, env=external_env(), stdout=subprocess.PIPE,
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


def empty_destination(destination):
    if destination.is_symlink() or (destination.exists() and
            (not destination.is_dir() or any(destination.iterdir()))):
        raise BackupError("Restore destination must be absent or an empty directory")


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise BackupError("Invalid backup ID; use the ID printed on the tape label")
    return value


@contextmanager
def drive_lock(device_number):
    # Linux tape minor bits 5/6 select mode; bit 7 selects non-rewind.
    number = os.minor(device_number) & ~0xe0
    path = DRIVE_LOCK_DIRECTORY / f'tape-backup-drive-{os.major(device_number)}-{number}.lock'
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        try:
            fd = os.open(path, os.O_RDONLY | flags)
        except FileNotFoundError:
            try:
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | flags, 0o666)
                os.fchmod(fd, 0o666)
            except FileExistsError:
                fd = os.open(path, os.O_RDONLY | flags)
    except OSError as exc:
        raise BackupError(f'Cannot access shared drive lock {path}: {exc}') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError(f'Invalid shared drive lock: {path}')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DriveBusy(f'Another operation is using this tape drive ({path})') from exc
        yield
    finally:
        os.close(fd)


def encoded_header(fields):
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    envelope = json.dumps({"fields": fields, "sha256": hashlib.sha256(payload).hexdigest()},
                          sort_keys=True, separators=(",", ":")).encode()
    magic = ZFS_MAGIC if fields.get('type') == 'volume' and fields.get('format') == 4 else MAGIC
    record = magic + envelope + b"\n"
    if len(record) > BLOCK_SIZE:
        raise BackupError("Tape metadata header exceeds 64 KiB")
    return record.ljust(BLOCK_SIZE, b"\0")


def decoded_header(record):
    if len(record) != BLOCK_SIZE or not record.startswith((MAGIC, ZFS_MAGIC)):
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
    def __init__(self, label, *, transfer=True, buffer_size=None):
        self.label = label
        self.transfer = transfer
        self.buffer_size = buffer_size
        self.read_bytes = self.written_bytes = self.transferred = 0
        self.queued_bytes = self.retry_bytes = self.durable_bytes = 0
        self.reader_state = None
        self.phase = "waiting for media"
        self.stop = threading.Event()
        self.started = self.last_time = time.monotonic()
        self.last_bytes = 0
        self.last_read_bytes = 0
        self.total_bytes = None
        self.eta_base = 0
        self.eta_started = self.started

    def estimate(self, total):
        self.total_bytes = total
        self.eta_base = self.written_bytes
        self.eta_started = time.monotonic()

    def report(self):
        # Skip status ticks while a prompt owns the terminal. Other logs and
        # child diagnostics wait on this lock and resume after the response.
        if not OUTPUT_LOCK.acquire(blocking=False):
            return
        try:
            self._report()
        finally:
            OUTPUT_LOCK.release()

    def _report(self):
        now = time.monotonic()
        if not self.transfer:
            log(f"{self.label}: {self.phase}; {now - self.started:.0f}s elapsed")
            return
        read_bytes, written_bytes, transferred = self.read_bytes, self.written_bytes, self.transferred
        rate = (transferred - self.last_bytes) / max(now - self.last_time, 0.001) / 1024**2
        read_rate = (read_bytes - self.last_read_bytes) / max(now - self.last_time, 0.001) / 1024**2
        average = transferred / max(now - self.started, 0.001) / 1024**2
        done = written_bytes - self.eta_base
        if self.phase == "complete":
            eta = "00:00:00"
        elif not self.total_bytes or not done:
            eta = "calculating"
        elif done >= self.total_bytes:
            eta = "finishing (estimate reached)"
        else:
            seconds = int((self.total_bytes - done) * (now - self.eta_started) / done)
            eta = f"~{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        buffer = f"buffer {self.buffer_size / 1024**2:g} MiB; " if self.buffer_size is not None else ""
        if self.buffer_size is not None:
            buffer += (f"queued {self.queued_bytes / 1024**2:.1f} MiB, "
                       f"recovery {self.retry_bytes / 1024**2:.1f} MiB, "
                       f"committed {self.durable_bytes / 1024**2:.1f} MiB; ")
        reader = (f"reader {self.reader_state}, {read_rate:.1f} MiB/s source; "
                  if self.reader_state is not None else "")
        log(f"{self.label}: {self.phase}; {read_bytes / 1024**2:.1f} MiB read, "
            f"{written_bytes / 1024**2:.1f} MiB delivered; "
            f"{buffer}{reader}"
            f"{rate:.1f} MiB/s I/O, {average:.1f} MiB/s average; "
            f"ETA {eta} (current archive); {now - self.started:.0f}s elapsed")
        self.last_time, self.last_bytes = now, transferred
        self.last_read_bytes = read_bytes

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


class SgIoHeader(ctypes.Structure):
    """Linux sg_io_hdr_t; native alignment also supports 32-bit hosts."""

    _fields_ = [("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
                ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
                ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
                ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
                ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint),
                ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
                ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
                ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
                ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
                ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
                ("duration", ctypes.c_uint), ("info", ctypes.c_uint)]


def tape_position(fd):
    """READ POSITION short form: (next host object, next unwritten object).

    Unlike MTIOCPOS, this exposes the on-medium position. No filemarks or
    movement commands are sent. None means telemetry cannot be trusted.
    """
    command = ctypes.create_string_buffer(bytes([0x34]) + bytes(9), 10)
    data, sense = ctypes.create_string_buffer(20), ctypes.create_string_buffer(64)
    header = SgIoHeader(interface_id=ord('S'), dxfer_direction=-3,
                        cmd_len=10, mx_sb_len=64, dxfer_len=20,
                        dxferp=ctypes.addressof(data), cmdp=ctypes.addressof(command),
                        sbp=ctypes.addressof(sense), timeout=60000)
    raw = bytearray(bytes(header))
    try:
        fcntl.ioctl(fd, 0x2285, raw, True)  # SG_IO
    except OSError as exc:
        if exc.errno in (errno.ENOTTY, errno.EINVAL, errno.ENOSYS, errno.EPERM, errno.EACCES):
            return None
        raise
    header = SgIoHeader.from_buffer_copy(raw)
    if header.status or header.host_status or header.driver_status:
        response = sense.raw[0] & 0x7f
        key = (sense.raw[2] if response in (0x70, 0x71) else sense.raw[1]) & 0xf
        if (header.status == 2 and not header.host_status and
                response in (0x70, 0x72) and key == 5):  # Unsupported command/form.
            return None
        raise OSError(errno.EIO, "READ POSITION failed; buffered tape writes may have failed")
    if header.resid or data.raw[0] & 0x06:  # Position unknown or position overflow.
        return None
    first, last = struct.unpack_from('>II', data.raw, 4)
    if last > first:
        return None
    return first, last


def compression_status(fd):
    """Read current DCE/DCC flags from SCSI Data Compression mode page 0Fh."""
    command = ctypes.create_string_buffer(bytes([0x1a, 0, 0x0f, 0, 255, 0]), 6)
    data, sense = ctypes.create_string_buffer(255), ctypes.create_string_buffer(64)
    header = SgIoHeader(interface_id=ord('S'), dxfer_direction=-3,
                        cmd_len=6, mx_sb_len=64, dxfer_len=255,
                        dxferp=ctypes.addressof(data), cmdp=ctypes.addressof(command),
                        sbp=ctypes.addressof(sense), timeout=60000)
    raw = bytearray(bytes(header))
    try:
        fcntl.ioctl(fd, 0x2285, raw, True)
    except OSError as exc:
        raise OSError(exc.errno, f'Cannot read drive compression state (SG_IO MODE SENSE): '
                                f'{exc.strerror or exc}') from exc
    header = SgIoHeader.from_buffer_copy(raw)
    if header.status or header.host_status or header.driver_status:
        raise BackupError('Cannot read drive compression state: '
                          f'SCSI status 0x{header.status:02x}, host status {header.host_status}, '
                          f'driver status {header.driver_status}, '
                          f'sense {sense.raw[:min(header.sb_len_wr, 64)].hex()}')
    available = 255 - header.resid
    if not 4 <= available <= 255:
        raise BackupError('Truncated compression mode response')
    payload = data.raw
    end, page = payload[0] + 1, 4 + payload[3]
    if (end > available or page + 16 > end or payload[page] & 0x7f != 0x0f or
            payload[page + 1] < 14 or page + 2 + payload[page + 1] > end):
        raise BackupError('Invalid or missing Data Compression mode page')
    return {'supported': bool(payload[page + 2] & 0x40),
            'enabled': bool(payload[page + 2] & 0x80)}


def device_status(media):
    result = {'device': media.device, 'version': PROGRAM_VERSION, 'busy': False,
              'state': None, 'position': None, 'compression': None, 'warnings': [], 'statistics': {}}
    sysfs = Path('/sys/class/scsi_tape') / Path(media.device).name
    for name in ('vendor', 'model', 'rev'):
        try:
            result[name] = (sysfs / 'device' / name).read_text().strip()
        except OSError:
            result[name] = None
    for name in ('read_byte_cnt', 'write_byte_cnt', 'read_ns', 'write_ns', 'io_ns', 'in_flight'):
        try:
            result['statistics'][name] = int((sysfs / 'stats' / name).read_text())
        except (OSError, ValueError):
            result['statistics'][name] = None
    try:
        result['driver_options'] = int((sysfs / 'options').read_text().strip(), 0)
    except (OSError, ValueError):
        result['driver_options'] = None
    try:
        with media.lock():
            fd = os.open(media.device, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                raw = bytearray(struct.calcsize('@5l2i'))
                fcntl.ioctl(fd, 0x80000000 | len(raw) << 16 | 0x6d02, raw, True)
                values = struct.unpack('@5l2i', raw)
                flags = {'beginning_of_tape': 0x40000000, 'end_of_data': 0x08000000,
                         'end_of_tape': 0x20000000, 'write_protected': 0x04000000,
                         'online': 0x01000000, 'door_open': 0x00040000}
                result['state'] = {name: bool(values[3] & mask) for name, mask in flags.items()}
                result['file_number'], result['block_number'] = values[5:]
                if result['state']['online'] and not result['state']['door_open']:
                    for name, query in (('compression', compression_status), ('position', tape_position)):
                        try:
                            result[name] = query(fd)
                        except (BackupError, OSError) as exc:
                            result['warnings'].append(str(exc))
            finally:
                os.close(fd)
    except DriveBusy:
        result['busy'] = True
        result['warnings'].append('Drive in use; only passive sysfs statistics were read')
    except OSError as exc:
        result['warnings'].append(str(exc))
    result['recommendations'] = []
    if result.get('driver_options') is not None:
        if result['driver_options'] & 0xa002:
            result['recommendations'].append('Backup will disable immediate/async driver modes before writing')
        if not result['driver_options'] & 0x800:
            result['recommendations'].append('Backup will enable logical positioning before writing')
    if result['state'] and result['state']['write_protected']:
        result['recommendations'].append('Tape is write-protected; reads remain available')
    return result


class Volume:
    """Record I/O with non-flushing durability queries and explicit commits."""

    def __init__(self, stream, physical=False):
        self.stream = stream
        self.physical = physical
        self.progress = None
        self.objects = self.durable_objects = 0
        self.position_disabled = False
        self.record_bytes = 0
        self.prefetched = None

    def control(self, operation, count=1):
        fcntl.ioctl(self.stream.fileno(), MTIOCTOP, struct.pack("@hi", operation, count))

    def position(self):
        if not self.physical:
            return self.stream.tell()
        # MTIOCPOS addresses are used only with the matching MTSEEK interface,
        # never as proof of durability (which uses READ POSITION below).
        raw = bytearray(struct.calcsize('@l'))
        try:
            fcntl.ioctl(self.stream.fileno(), 0x80000000 | len(raw) << 16 | 0x6d03, raw, True)
        except OSError as exc:
            raise OSError(exc.errno, f'MTIOCPOS tape-position query failed: {exc.strerror or exc}') from exc
        return struct.unpack('@l', raw)[0]

    def seek_position(self, position):
        self.prefetched = None
        if self.physical:
            if not 0 <= position <= 0x7fffffff:
                raise BackupError("Tape location is outside the supported seek range")
            self.control(MTSEEK, position)
        else:
            self.stream.seek(position)

    def seek_end(self):
        self.prefetched = None
        if self.physical:
            self.control(MTEOM)
        else:
            self.stream.seek(0, os.SEEK_END)
        return self.position()

    def start_appending(self):
        if self.physical:
            position = tape_position(self.stream.fileno())
            if position is None:
                self.position_disabled = True
                self.objects = self.durable_objects = 0
            else:
                self.objects = self.durable_objects = position[0]

    def require_blank(self):
        """A continuation must never reuse a recorded cartridge as fresh media."""
        if not self.physical:
            raise BackupError('Blank-cartridge check requires physical tape')
        end = self.seek_end()
        if end != 0:
            raise MediaNotBlank('Refusing to overwrite a cartridge containing recorded data; '
                                'load a blank cartridge for continuation')
        # EOD must be unambiguous. Position/read errors abort; an unreadable
        # cartridge is never assumed to be blank. Keep this descriptor open.
        record = self.next_record()
        if record is not None or self.position() != 0:
            raise MediaNotBlank('Refusing to overwrite a cartridge containing recorded data')
        self.control(MTREW)
        self.record_bytes = 0

    def next_record(self):
        """Read a boundary record; distinguish filemarks, clean EOD and errors."""
        for _ in range(8):
            before = self.position() if self.physical else None
            try:
                record = self.stream.read(BLOCK_SIZE)
            except OSError as exc:
                if not self.physical or exc.errno != errno.EIO:
                    raise
                # Linux returns EIO after the final zero-length EOD reads.
                # Other I/O errors must never be mistaken for clean EOD.
                raw = bytearray(struct.calcsize('@5l2i'))
                fcntl.ioctl(self.stream.fileno(), 0x80000000 | len(raw) << 16 | 0x6d02, raw, True)
                if struct.unpack('@5l2i', raw)[3] & 0x08000000:  # GMT_EOD
                    return None
                raise
            if record:
                self.record_bytes += len(record)
                if len(record) != BLOCK_SIZE:
                    raise BackupError("Truncated tape record; cannot locate a safe boundary")
                return record
            if not self.physical or self.position() == before:
                return None
            # Reading a filemark advances the position. Reset the driver's EOF
            # state so two adjacent filemarks cannot hide a later backup.
            self.control(MTFSF, 0)
        raise BackupError("Too many consecutive filemarks")

    def write(self, record):
        count = self.stream.write(record)
        if self.progress and count:
            self.progress.transferred += count
        if count != len(record):
            raise OSError(errno.ENOSPC, "Short tape record write")
        self.objects += 1
        self.record_bytes += count

    def commit(self):
        if self.physical:
            # MTWEOF, unlike MTWEOFI, waits for buffered data to reach the tape.
            self.control(MTWEOF)
            self.objects += 1
        else:
            os.fsync(self.stream.fileno())
        self.durable_objects = self.objects

    def durable_position(self):
        if not self.physical or self.position_disabled:
            return None
        position = tape_position(self.stream.fileno())
        if (position is None or position[0] != self.objects or
                not self.durable_objects <= position[1] <= self.objects):
            self.position_disabled = True
            log("Drive position unavailable or inconsistent; using synchronous commits "
                "when the recovery buffer fills")
            return None
        self.durable_objects = position[1]
        return position[1]

    def read(self, *, boundary=False):
        if self.prefetched is not None:
            record, self.prefetched = self.prefetched, None
            return record
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
        self.record_bytes += len(record)
        if len(record) != BLOCK_SIZE:
            raise EndVolume
        return record

    def skip_payload(self, length):
        """Skip file data when loading a snapshot; this does not verify data."""
        if self.physical:
            for _ in range((length + BLOCK_SIZE - 1) // BLOCK_SIZE):
                self.read()
        else:
            size = ((length + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE
            if self.stream.tell() + size > os.fstat(self.stream.fileno()).st_size:
                raise EndVolume
            self.stream.seek(size, os.SEEK_CUR)
            self.record_bytes += size

    def close(self):
        self.stream.close()


def payload_records(length):
    return (length + BLOCK_SIZE - 1) // BLOCK_SIZE


def read_payload(volume, length, output=None):
    digest = hashlib.sha256()
    result = bytearray() if output is None else None
    while length:
        record = volume.read()
        count = min(length, BLOCK_SIZE)
        if any(record[count:]):
            raise BackupError("Corrupt metadata padding")
        digest.update(record[:count])
        if output is None:
            result.extend(record[:count])
        else:
            output.write(record[:count])
        length -= count
    return result, digest.hexdigest()


def read_metadata(volume, record, snapshot_fd=None):
    """Validate an independent metadata file; never trust its cached locations alone."""
    head = decoded_header(record)
    try:
        if head['type'] != 'metadata' or head['version'] != 1:
            raise ValueError('unsupported metadata file')
        catalog_size, snapshot_size = head['catalog_bytes'], head['snapshot_bytes']
        if (type(catalog_size) is not int or not 0 < catalog_size <= MAX_CATALOG_BYTES or
                type(snapshot_size) is not int or snapshot_size <= 0 or
                type(head['used_bytes']) is not int or head['used_bytes'] < BLOCK_SIZE):
            raise ValueError('invalid metadata lengths')
        summary = head['summary']
        valid_id(summary['id'])
        if snapshot_size != summary['snapshot_bytes']:
            raise ValueError('snapshot length differs from completion record')
        catalog, digest = read_payload(volume, catalog_size)
        if digest != head['catalog_sha256']:
            raise ValueError('catalog checksum mismatch')
        entries = json.loads(catalog)
        if not isinstance(entries, list) or not entries:
            raise ValueError('empty catalog')
        for entry in entries:
            valid_id(entry['id'])
            if (type(entry['volume']) is not int or entry['volume'] < 1 or
                    type(entry['position']) is not int or entry['position'] < 0 or
                    type(entry['record_bytes']) is not int or entry['record_bytes'] < 0 or
                    not re.fullmatch('[0-9a-f]{64}', entry['header_sha256'])):
                raise ValueError('invalid catalog location')
        if entries[-1]['id'] != summary['id']:
            raise ValueError('catalog does not end with the completed backup')
        if snapshot_fd is None:
            class Discard:
                def write(self, data):
                    return len(data)
            _, digest = read_payload(volume, snapshot_size, Discard())
        else:
            os.ftruncate(snapshot_fd, 0)
            os.lseek(snapshot_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(snapshot_fd), 'wb', buffering=0) as stream:
                _, digest = read_payload(volume, snapshot_size, stream)
        if digest != summary['snapshot_sha256']:
            raise ValueError('snapshot checksum mismatch')
        trailer = decoded_header(volume.read())
        expected = {'type': 'metadata-end', 'version': 1,
                    'records': 2 + payload_records(catalog_size) + payload_records(snapshot_size),
                    'header_sha256': hashlib.sha256(record).hexdigest()}
        if trailer != expected:
            raise ValueError('metadata completion record mismatch')
        return {**head, 'entries': entries}
    except (KeyError, TypeError, ValueError, EndVolume) as exc:
        raise BackupError(f'Incomplete or corrupt metadata file: {exc}') from exc


def latest_metadata(volume, snapshot_fd=None):
    """Look only at the last tape file / fixed trailer, without scanning archives."""
    end = volume.seek_end()
    if volume.physical:
        candidates = (2, 3)  # One terminal filemark, or an additional empty file.
    else:
        if end < BLOCK_SIZE:
            return None
        volume.seek_position(end - BLOCK_SIZE)
        try:
            trailer = decoded_header(volume.read())
        except (BackupError, EndVolume):
            return None
        if trailer.get('type') != 'metadata-end':
            return None
        count = trailer.get('records')
        if type(count) is not int or count < 3 or count * BLOCK_SIZE > end:
            return None
        candidates = (end - count * BLOCK_SIZE,)
    for index, candidate in enumerate(candidates):
        try:
            if volume.physical:
                if index:
                    volume.seek_end()
                volume.control(MTBSFM, candidate)
            else:
                volume.seek_position(candidate)
            record = volume.next_record()
            if record is None or decoded_header(record).get('type') != 'metadata':
                continue
            result = read_metadata(volume, record, snapshot_fd)
            # An older valid footer must never authorize appending after a
            # crashed backup. Require clean, unchanged recorded EOD after it.
            if volume.next_record() is not None or volume.position() != end:
                continue
            return result
        except (OSError, BackupError, EndVolume):
            continue
    return None


def locate_volume(volume, backup_id, number):
    """Walk segment boundaries, leaving the matching header for StreamReader."""
    volume.catalog_entries = []
    while True:
        position = volume.position()
        record = volume.next_record()
        if record is None:
            return False
        head = decoded_header(record)
        kind = head.get('type')
        if kind == 'volume':
            try:
                if head['format'] not in (3, 4):
                    raise BackupError('Unsupported tape format; supported formats are 3 (tar) and 4 (ZFS)')
                job = head['backup']
                valid_id(job['id'])
                volume.catalog_entries.append({'id': job['id'], 'volume': head['volume'],
                                               'position': position,
                                               'record_bytes': volume.record_bytes - BLOCK_SIZE,
                                               'header_sha256': hashlib.sha256(record).hexdigest()})
                if (backup_id is None or job['id'] == backup_id) and head['volume'] == number:
                    volume.prefetched = record
                    return True
            except (KeyError, TypeError) as exc:
                raise BackupError(f'Invalid volume header at {position}') from exc
        elif kind == 'chunk':
            length = head.get('length')
            if type(length) is not int or not 0 < length <= FRAME_SIZE:
                raise BackupError('Invalid chunk length while locating backup')
            volume.skip_payload(length)
        elif kind == 'metadata':
            read_metadata(volume, record)
        else:
            raise BackupError(f'Unexpected record while locating backup: {kind}')


def write_metadata(volume, entries, summary, snapshot_fd, used_bytes, volume_size):
    catalog = json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()
    if len(catalog) > MAX_CATALOG_BYTES:
        raise BackupError('On-tape catalog exceeds 64 MiB')
    snapshot_size = os.fstat(snapshot_fd).st_size
    records = 2 + payload_records(len(catalog)) + payload_records(snapshot_size)
    size = records * BLOCK_SIZE
    if volume_size is not None and used_bytes + size > volume_size:
        log('Backup committed; metadata file does not fit on this cartridge. '
            'The next incremental will load its snapshot by scanning.')
        return False
    head = encoded_header({'type': 'metadata', 'version': 1, 'summary': summary,
                           'catalog_bytes': len(catalog), 'snapshot_bytes': snapshot_size,
                           'catalog_sha256': hashlib.sha256(catalog).hexdigest(),
                           'used_bytes': used_bytes + size})
    volume.write(head)
    for offset in range(0, len(catalog), BLOCK_SIZE):
        volume.write(catalog[offset:offset + BLOCK_SIZE].ljust(BLOCK_SIZE, b'\0'))
    os.lseek(snapshot_fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(snapshot_fd), 'rb', buffering=0) as snapshot:
        while data := snapshot.read(BLOCK_SIZE):
            volume.write(data.ljust(BLOCK_SIZE, b'\0'))
    volume.write(encoded_header({'type': 'metadata-end', 'version': 1, 'records': records,
                                 'header_sha256': hashlib.sha256(head).hexdigest()}))
    volume.commit()
    return True


def validate_metadata_locations(volume, cached):
    """Bind a footer to actual segment headers on this cartridge before using it."""
    end = volume.position()
    for entry in (cached['entries'][0], cached['entries'][-1]):
        volume.seek_position(entry['position'])
        record = volume.next_record()
        if record is None or hashlib.sha256(record).hexdigest() != entry['header_sha256']:
            raise BackupError('Metadata points to a different or corrupt cartridge')
        head = decoded_header(record)
        if (head.get('type') != 'volume' or head.get('volume') != entry['volume'] or
                head.get('backup', {}).get('id') != entry['id']):
            raise BackupError('Metadata backup location does not match its header')
    if any(cached['summary'].get(k) != v for k, v in head['backup'].items()):
        raise BackupError('Metadata summary differs from the backup header')
    if volume.seek_end() != end:
        raise BackupError('Tape position changed while validating metadata')


def select_volume(volume, backup_id, number):
    """Use a validated catalog for later backups; keep first-header reads cheap."""
    start = volume.position()
    initial_bytes = volume.record_bytes
    record = volume.next_record()
    if record is None:
        return False
    head = decoded_header(record)
    if (head.get('type') == 'volume' and head.get('volume') == number and
            (backup_id is None or head.get('backup', {}).get('id') == backup_id)):
        volume.prefetched = record
        volume.catalog_entries = [{'id': head['backup']['id'], 'volume': number,
                                   'position': start, 'record_bytes': initial_bytes,
                                   'header_sha256': hashlib.sha256(record).hexdigest()}]
        return True
    if backup_id is not None:
        cached = latest_metadata(volume)
        if cached:
            entries = cached['entries']
            for index, entry in enumerate(entries):
                if entry['id'] == backup_id and entry['volume'] == number:
                    volume.seek_position(entry['position'])
                    record = volume.next_record()
                    if record is not None and hashlib.sha256(record).hexdigest() == entry['header_sha256']:
                        volume.prefetched = record
                        volume.catalog_entries = entries[:index + 1]
                        volume.record_bytes = entry['record_bytes'] + BLOCK_SIZE
                        return True
    volume.seek_position(start)
    volume.record_bytes = initial_bytes
    return locate_volume(volume, backup_id, number)


class FileMedia:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.path = None
        self.prepared = None
        self.append_volume = None
        self.entries = []
        self.append_mode = False

    def load(self, backup_id, number, writing):
        if not writing and backup_id is not None:
            direct = self.directory / f'{backup_id}.{number:04d}.tape'
            paths = [direct] if direct.exists() else []
            paths += [p for p in sorted(self.directory.glob('*.tape')) if p != direct]
            for path in paths:
                volume = Volume(open(path, 'r+b' if self.append_mode else 'rb', buffering=0))
                try:
                    if select_volume(volume, backup_id, number):
                        self.path, self.prepared = path, volume
                        return
                except (BackupError, EndVolume):
                    if path == direct:
                        volume.close()
                        raise
                volume.close()
            if direct.exists():
                raise BackupError(f'Wrong backup or volume in {direct.name}')
            raise BackupError(f'Incomplete backup: missing tape volume {backup_id}.{number:04d}.tape')
        if backup_id is None:
            ids = sorted({p.name.split(".")[0] for p in self.directory.glob("*.0001.tape")})
            if len(ids) != 1:
                raise BackupError("Specify --backup; available IDs: " + ", ".join(ids))
            backup_id = ids[0]
        valid_id(backup_id)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / f"{backup_id}.{number:04d}.tape"

    def open(self, writing):
        if not writing and self.prepared is not None:
            volume, self.prepared = self.prepared, None
            return volume
        try:
            stream = open(self.path, "xb" if writing else ('r+b' if self.append_mode else 'rb'), buffering=0)
        except FileNotFoundError as exc:
            raise BackupError(f"Incomplete backup: missing tape volume {self.path.name}") from exc
        return Volume(stream)

    def cached_base(self, backup_id, snapshot_fd):
        for path in sorted(self.directory.glob('*.tape')):
            volume = Volume(open(path, 'r+b', buffering=0))
            try:
                cached = latest_metadata(volume, snapshot_fd)
                if cached and cached['summary']['id'] == backup_id:
                    validate_metadata_locations(volume, cached)
                    self.path = path
                    self.entries = cached['entries']
                    volume.record_bytes = cached['used_bytes']
                    volume.append_end = volume.position()
                    self.append_volume = volume
                    return cached['summary']
            except BaseException:
                volume.close()
                raise
            volume.close()
        return None

    def cartridges(self):
        paths = sorted(self.directory.glob('*.tape'))
        if not paths:
            raise BackupError('No tape files found')
        for path in paths:
            volume = Volume(open(path, 'rb', buffering=0))
            try:
                yield path.name, volume
            finally:
                volume.close()

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
        self.loaded = False
        self.prepared = self.append_volume = None
        self.entries = []
        self.append_mode = False

    def mt(self, *args):
        if args == ("rewind",):
            # MTWEOF itself honors the driver's NOWAIT_EOF setting. Disabling
            # immediate modes and kernel async writes keeps our object count
            # aligned with completed SCSI commands. Drive buffering stays on.
            options = Path("/sys/class/scsi_tape") / Path(self.device).name / "options"
            try:
                current = int(options.read_text().strip(), 0)
            except (OSError, ValueError):
                current = None
            if current is None or current & 0xA002:
                run_command(["mt", "-f", self.device, "stclearoptions", "0xa002"])
            # MTIOCPOS/MTSEEK default to device-dependent addresses. That uses
            # a legacy READ POSITION form that some modern drives reject.
            # Keep catalog tell/seek addresses in the same logical coordinate
            # system as the direct SCSI READ POSITION used for durability.
            if current is None or not current & 0x800:  # MT_ST_SCSI2LOGICAL
                run_command(["mt", "-f", self.device, "stsetoptions", "scsi2logical"])
        run_command(["mt", "-f", self.device, *args])

    def request(self, backup_id, number, action):
        backup_id = backup_id or "unknown-backup"
        if self.media_command:
            with OUTPUT_LOCK:
                run_command([self.media_command, action, backup_id, str(number), self.device])
        else:
            warning = " (CONTENTS WILL BE OVERWRITTEN)" if action == 'write' else ""
            if action == 'blank':
                warning = ' (BLANK CARTRIDGE REQUIRED; recorded tapes will be refused)'
            label = f'{backup_id} volume {number}'
            if action == 'append':
                label = f'the final cartridge of {backup_id}'
                warning = ' (existing backups will be retained)'
            elif number == 0:
                label = 'the cartridge to inspect'
            try:
                # Buffered r+ requires seeking, which terminals do not support.
                with OUTPUT_LOCK, open("/dev/tty", "r") as tty_in, open("/dev/tty", "w") as tty_out:
                    purpose = ('continuation' if number > 1 else 'write') if action == 'blank' else action
                    tty_out.write(f"Load {label} into {self.device} for {purpose}{warning}.\n"
                                  "Press Enter when ready, or type q to stop: ")
                    tty_out.flush()
                    response = tty_in.readline()
            except OSError as exc:
                raise BackupError(f"No terminal for tape changes; use --media-command ({exc})") from exc
            if not response or response.strip().lower() == "q":
                raise BackupError("Media change cancelled; the streaming operation is incomplete")

    def load(self, backup_id, number, writing):
        if not writing and getattr(self, 'loaded', False):
            # Restore chains can follow another backup on the same cartridge.
            for rewind in (False, True):
                if rewind:
                    self.mt('rewind')
                volume = self.raw_open(False)
                try:
                    if select_volume(volume, backup_id, number):
                        self.prepared = volume
                        return
                except (EndVolume, BackupError):
                    # This is only a lookup on the currently loaded cartridge,
                    # possibly starting in a partial trailing frame or footer.
                    # Retry from BOT, then request the intended cartridge. Its
                    # selected archive is still validated by open/StreamReader.
                    pass
                except BaseException:
                    volume.close()
                    raise
                volume.close()
        action = ('blank' if getattr(self, 'blank_required', False) else 'write') if writing else 'read'
        self.request(backup_id, number, action)
        self.mt("rewind")
        self.mt("setblk", "0")
        self.loaded = True
        self.read_request = (backup_id, number)

    def raw_open(self, writing):
        mode = 'r+b' if writing or getattr(self, 'append_mode', False) else 'rb'
        return Volume(open(self.device, mode, buffering=0), physical=True)

    def open(self, writing):
        if not writing and getattr(self, 'prepared', None) is not None:
            volume, self.prepared = self.prepared, None
            return volume
        volume = self.raw_open(writing)
        if writing and getattr(self, 'blank_required', False):
            try:
                volume.require_blank()
            except BaseException:
                volume.close()
                raise
        if not writing:
            try:
                if not select_volume(volume, *self.read_request):
                    raise WrongMedia('Requested backup/volume is not on the loaded cartridge')
            except BaseException:
                volume.close()
                raise
        return volume

    def cached_base(self, backup_id, snapshot_fd):
        self.request(backup_id, 0, 'append')
        self.mt('rewind')
        self.mt('setblk', '0')
        self.loaded = True
        volume = self.raw_open(False)
        try:
            cached = latest_metadata(volume, snapshot_fd)
            if cached:
                if cached['summary']['id'] != backup_id:
                    raise BackupError('Append requires the latest completed backup as --base: ' +
                                      cached['summary']['id'])
                validate_metadata_locations(volume, cached)
                self.entries = cached['entries']
                volume.record_bytes = cached['used_bytes']
                volume.append_end = volume.position()
                self.append_volume = volume
                return cached['summary']
        except BaseException:
            volume.close()
            raise
        volume.close()
        self.mt('rewind')
        return None

    def eject(self):
        self.mt("offline")

    def compression(self, action='status'):
        if action not in ('status', 'on', 'off'):
            raise BackupError('Compression action must be status, on, or off')
        volume = self.raw_open(action != 'status')
        try:
            status = compression_status(volume.stream.fileno())
            if action != 'status':
                if not status['supported']:
                    raise BackupError('Drive reports that hardware compression is unsupported')
                enabled = action == 'on'
                volume.control(MTCOMPRESSION, int(enabled))
                try:
                    status = compression_status(volume.stream.fileno())
                except (BackupError, OSError) as exc:
                    raise BackupError(f'Compression change was sent, but its resulting state could not '
                                      f'be verified: {exc}') from exc
                if not status['supported'] or status['enabled'] != enabled:
                    raise BackupError(f'Drive did not retain the requested compression setting: {action}')
            return status
        finally:
            volume.close()

    def wipe(self, *, long_erase=False, yes=False):
        mode = 'long erase' if long_erase else 'short erase (initialize for reuse)'
        if not yes:
            try:
                with OUTPUT_LOCK, open('/dev/tty', 'r') as tty_in, open('/dev/tty', 'w') as tty_out:
                    tty_out.write(f'DESTROY the recorded backups on the tape in {self.device}: {mode}.\n'
                                  'Type WIPE to confirm, or anything else to cancel: ')
                    tty_out.flush()
                    response = tty_in.readline()
            except OSError as exc:
                raise BackupError(f'No terminal for wipe confirmation; use --yes only when you intend '
                                  f'to erase the loaded tape ({exc})') from exc
            if response.strip() != 'WIPE':
                raise BackupError('Wipe cancelled; tape was not changed')
        log(f'Initializing tape in {self.device}: {mode}; existing backups will be destroyed')
        with Progress(f'Wipe {self.device}', transfer=False) as progress:
            progress.phase = 'rewinding and preparing drive'
            # Also disables immediate rewind/erase, async writes and immediate
            # filemarks, and enables the logical positions used to check EOD.
            self.mt('rewind')
            self.mt('setblk', '0')
            volume = self.raw_open(True)
            try:
                progress.phase = mode
                try:
                    # Explicit count: 0 is short erase; 1 requests long erase.
                    # Keep the descriptor open until blank verification ends.
                    volume.control(MTERASE, 1 if long_erase else 0)
                    volume.control(MTREW)
                except OSError as exc:
                    raise OSError(exc.errno, f'{mode} failed on {self.device}: {exc.strerror or exc}') from exc
                progress.phase = 'verifying blank tape'
                try:
                    volume.require_blank()
                except (BackupError, OSError) as exc:
                    raise BackupError(f'Erase returned, but blank verification failed on {self.device}: {exc}') from exc
            finally:
                volume.close()
            self.loaded = False
            self.entries = []
            progress.phase = 'blank verified; rewound and left loaded'

    def cartridges(self):
        self.request(None, 0, 'read')
        self.mt('rewind')
        self.mt('setblk', '0')
        volume = self.raw_open(False)
        try:
            yield self.device, volume
        finally:
            volume.close()

    @contextmanager
    def lock(self):
        with drive_lock(self.device_number):
            yield


class StreamWriter:
    """Continuously write small frames, retaining the unconfirmed tail in RAM."""

    def __init__(self, media, job, volume_size, progress, buffer_size=DEFAULT_BUFFER):
        self.media, self.job = media, job
        self.volume_size, self.progress = volume_size, progress
        self.replay_limit = max(2 * BLOCK_SIZE, buffer_size)
        if volume_size:
            self.replay_limit = min(self.replay_limit, volume_size - BLOCK_SIZE)
        self.volume = None
        self.number = self.sequence = self.used = 0
        self.chain = ZERO_CHAIN
        self.pending = deque()
        self.pending_bytes = self.since_position = 0
        self.no_progress = self.committed_frames = 0

    def close(self, *, ignore_errors=False):
        if self.volume is not None:
            volume, self.volume = self.volume, None
            try:
                volume.close()
            except OSError:
                if not ignore_errors:
                    raise

    @contextmanager
    def startup_operation(self, action):
        phase = f'{action} volume {self.number}'
        self.progress.phase = phase
        try:
            yield
        except OSError as exc:
            target = getattr(self.media, 'device', None) or getattr(self.media, 'path', 'media')
            raise OSError(exc.errno, f'{phase} on {target}: {exc.strerror or exc}') from exc

    def next_volume(self):
        self.close(ignore_errors=True)
        self.number += 1
        self.committed_frames = 0
        self.progress.phase = f"load volume {self.number}"
        prepared = getattr(self.media, 'append_volume', None) if self.number == 1 else None
        self.media.append_volume = None
        if prepared is not None and self.volume_size and prepared.record_bytes + 4 * BLOCK_SIZE > self.volume_size:
            log('No room for a new backup segment under the cartridge size limit; load fresh media')
            prepared.close()
            prepared = None
        if prepared is not None:
            self.volume = prepared
            self.used = self.volume.record_bytes
            # Validate a known header again after source preparation, on the
            # same open descriptor, before issuing any write or filemark.
            with self.startup_operation('validating append position for'):
                anchor = self.media.entries[0]
                self.volume.seek_position(anchor['position'])
                record = self.volume.next_record()
                if record is None or hashlib.sha256(record).hexdigest() != anchor['header_sha256']:
                    raise BackupError('Append cartridge changed after validation')
                position = self.volume.seek_end()
                if position != self.volume.append_end:
                    raise BackupError('Recorded tape end changed after append validation')
                if position <= max(entry['position'] for entry in self.media.entries):
                    raise BackupError('Append position is not beyond the recorded backup headers')
                self.volume.start_appending()
                if (self.volume.physical and not self.volume.position_disabled and
                        self.volume.objects != position):
                    raise BackupError('Append position disagrees with SCSI READ POSITION; refusing to write')
            self.volume.record_bytes = self.used
        else:
            self.media.blank_required = True
            while True:
                self.progress.phase = f"load volume {self.number}"
                self.media.load(self.job["id"], self.number, True)
                try:
                    with self.startup_operation('opening'):
                        self.volume = self.media.open(True)
                except MediaNotBlank as exc:
                    # open() closes the rejected descriptor before this retry.
                    # No header has been written, so retain this volume number,
                    # the source stream, and every pending recovery frame.
                    log(f'Volume {self.number} rejected before writing: {exc}. '
                        'Backup remains active; buffered data is retained. '
                        'Replace the cartridge and confirm the same volume again, or cancel to stop.')
                    continue
                break
            self.media.entries = []
            self.used = 0
            # A successful fresh-media load rewinds (or creates an empty file).
            # No position query is needed before the first record is written.
            position = 0
        self.volume.progress = self.progress
        first = self.pending[0] if self.pending else None
        header = encoded_header({"type": "volume", "format": 4 if self.job.get('archive_type') == 'zfs' else 3,
                                          "backup": self.job,
                                          "volume": self.number,
                                          "sequence": first["sequence"] if first else self.sequence,
                                          "previous": first["previous"] if first else self.chain,
                                          "replay_bytes": self.replay_limit})
        with self.startup_operation('writing header for'):
            self.volume.write(header)
        with self.startup_operation('committing header for'):
            self.volume.commit()
        self.media.entries.append({'id': self.job['id'], 'volume': self.number,
                                   'position': position, 'record_bytes': self.used,
                                   'header_sha256': hashlib.sha256(header).hexdigest()})
        self.used += BLOCK_SIZE
        self.committed_frames = self.since_position = 0
        # Probe before accepting archive bytes. Unsupported drives use the same
        # bounded recovery algorithm, with commits only on buffer pressure.
        with self.startup_operation('checking write position for'):
            self.volume.durable_position()
        self.progress.phase = f"streaming to volume {self.number}"
        log(f"Writing {self.job['id']} volume {self.number}, chunk "
            f"{first['sequence'] if first else self.sequence}")

    def retire(self, position):
        while self.pending and self.pending[0]["end"] is not None and self.pending[0]["end"] <= position:
            frame = self.pending.popleft()
            self.pending_bytes -= frame["size"]
            self.committed_frames += 1
            if frame["kind"] == "data":
                self.progress.durable_bytes += len(frame["data"])
        self.progress.retry_bytes = self.pending_bytes

    def poll_position(self):
        position = self.volume.durable_position()
        self.since_position = 0
        if position is not None:
            self.retire(position)

    def write_frame(self, frame):
        self.progress.phase = f"writing volume {self.number}, chunk {frame['sequence']}"
        self.volume.write(frame["record"])
        view = memoryview(frame["data"])
        for offset in range(0, len(view), BLOCK_SIZE):
            block = view[offset:offset + BLOCK_SIZE]
            self.volume.write(block if len(block) == BLOCK_SIZE else
                              bytes(block).ljust(BLOCK_SIZE, b"\0"))
        frame["end"] = self.volume.objects
        self.used += frame["size"]
        self.since_position += frame["size"]

    def recover(self, error):
        while True:
            if error.errno not in RECOVERABLE:
                raise error
            self.no_progress = 1 if self.committed_frames else self.no_progress + 1
            if self.no_progress >= 3:
                raise BackupError("Three volumes could not commit data; check drive/media and buffer size") from error
            log(f"Volume {self.number}: {error}. Retain this volume; replaying "
                f"{len(self.pending)} uncommitted chunk(s) from RAM on the next tape.")
            # Old physical positions must never release a frame on a new tape.
            for frame in self.pending:
                frame["end"] = None
            # A failed volume header is not a skippable data tail: readers need
            # every numbered volume's identity and replay boundary. Abort if
            # initialization fails rather than publish an unreadable tape set.
            self.next_volume()
            try:
                for frame in self.pending:
                    self.write_frame(frame)
                return
            except OSError as exc:
                error = exc

    def flush(self, reason):
        while True:
            self.progress.phase = f"flushing volume {self.number} ({reason})"
            try:
                self.volume.commit()
            except OSError as exc:
                self.recover(exc)
            else:
                self.retire(self.volume.objects)
                return

    def send(self, kind, data):
        if not data or len(data) > FRAME_SIZE:
            raise BackupError("Invalid continuous-stream frame size")
        size = BLOCK_SIZE + ((len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE
        if size > self.replay_limit:
            raise BackupError("Frame cannot fit in the recovery buffer")
        if self.volume is None:
            self.next_volume()
        if self.volume_size and self.used + size > self.volume_size:
            self.flush("volume boundary")
            self.next_volume()
        if self.pending_bytes + size > self.replay_limit:
            try:
                self.poll_position()
            except OSError as exc:
                self.recover(exc)
            if self.pending_bytes + size > self.replay_limit:
                self.flush("recovery buffer full")
        fields = {"type": "chunk", "sequence": self.sequence, "kind": kind,
                  "length": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                  "previous": self.chain}
        record = encoded_header(fields)
        frame = {**fields, "data": data, "record": record, "size": size, "end": None}
        self.pending.append(frame)
        self.pending_bytes += size
        self.progress.retry_bytes = self.pending_bytes
        self.sequence += 1
        self.chain = hashlib.sha256(record).hexdigest()
        try:
            self.write_frame(frame)
            if self.since_position >= POSITION_INTERVAL:
                self.poll_position()
        except OSError as exc:
            self.recover(exc)
        if kind == "data":
            self.progress.written_bytes += len(data)
        self.progress.phase = f"streaming to volume {self.number}"

    def finish(self):
        self.flush("backup completion")


class StreamReader:
    def __init__(self, media, backup_id=None, progress=None):
        self.media, self.backup_id, self.progress = media, backup_id, progress
        self.volume = self.job = None
        self.number = self.sequence = 0
        self.chain = ZERO_CHAIN
        self.summary = None
        self.replay_limit = self.history_bytes = 0
        self.history = OrderedDict()

    def close(self):
        if self.volume is not None:
            volume, self.volume = self.volume, None
            volume.close()

    def next_volume(self):
        self.close()
        requested_number = self.number + 1
        while True:
            try:
                self.media.load(self.backup_id, requested_number, False)
                self.volume = self.media.open(False)
                break
            except WrongMedia as exc:
                if not isinstance(self.media, TapeMedia):
                    raise
                self.media.loaded = False
                log(f'{exc}. Load {self.backup_id or "the requested backup"} volume '
                    f'{requested_number} and confirm again, or cancel to stop.')
        self.number = requested_number
        self.volume.progress = self.progress
        try:
            head = decoded_header(self.volume.read(boundary=True))
            job = head["backup"]
            valid_id(job["id"])
            if head["format"] not in (3, 4):
                raise ValueError('unsupported tape format; supported formats are 3 (tar) and 4 (ZFS)')
            if ((head['format'] == 4) != (job.get('archive_type', 'tar') == 'zfs') or
                    job.get('archive_type', 'tar') not in ('tar', 'zfs')):
                raise ValueError('archive type does not match the tape format')
            if (head["type"] != "volume" or head["volume"] != self.number or
                    (self.backup_id is not None and job["id"] != self.backup_id)):
                raise ValueError("wrong backup or volume number")
            limit = head["replay_bytes"]
            if (type(limit) is not int or not 2 * BLOCK_SIZE <= limit <= MAX_BUFFER or
                    (self.replay_limit and limit != self.replay_limit)):
                raise ValueError("invalid recovery window")
            self.replay_limit = limit
            if job["level"] not in ("full", "incremental") or not isinstance(job["source"], str):
                raise ValueError("invalid backup metadata")
            if job["level"] == "full" and job["parent"] is not None:
                raise ValueError("full backup has a parent")
            if job["level"] == "incremental":
                valid_id(job["parent"])
            if job.get('archive_type') == 'zfs':
                metadata = validate_zfs_metadata(job.get('zfs'))
                if (job['source'] != metadata['dataset'] or
                        (job['level'] == 'full') != (metadata['base_snapshot'] is None)):
                    raise ValueError('ZFS stream identity does not match the backup header')
            if self.job is not None and job != self.job:
                raise ValueError("volume belongs to a different backup")
            if type(head["sequence"]) is not int:
                raise ValueError("invalid sequence")
            if head["sequence"] == self.sequence:
                if head["previous"] != self.chain:
                    raise ValueError("broken chain between tapes")
            elif (head["sequence"] not in self.history or
                    self.history[head["sequence"]][0] != head["previous"]):
                raise ValueError("missing or reordered chunks between tapes")
        except (KeyError, TypeError, ValueError, EndVolume) as exc:
            raise BackupError(f"Wrong or incomplete volume {self.number}: {exc}") from exc
        self.job, self.backup_id = job, job["id"]
        self.volume_sequence, self.volume_chain = head["sequence"], head["previous"]
        if hasattr(self.volume, 'catalog_entries'):
            self.media.entries = self.volume.catalog_entries
        elif not self.volume.physical:
            self.media.entries = [{'id': job['id'], 'volume': self.number,
                                   'position': self.volume.position() - BLOCK_SIZE,
                                   'record_bytes': self.volume.record_bytes - BLOCK_SIZE,
                                   'header_sha256': hashlib.sha256(encoded_header(head)).hexdigest()}]

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
                            type(head["length"]) is not int or
                            not 0 < head["length"] <= FRAME_SIZE or
                            type(head["sequence"]) is not int or
                            not re.fullmatch(r"[0-9a-f]{64}", head["sha256"])):
                        raise ValueError("invalid chunk fields")
                    digest = hashlib.sha256(record).hexdigest()
                    duplicate = head["sequence"] < self.sequence
                    if head["sequence"] != self.volume_sequence or head["previous"] != self.volume_chain:
                        raise ValueError("missing, reordered or corrupt chunk")
                    if duplicate:
                        known = (head["sequence"] in self.history and
                                 self.history[head["sequence"]][1] == digest)
                        if not known:
                            raise ValueError("replayed chunk differs from the original")
                    elif head["sequence"] != self.sequence or head["previous"] != self.chain:
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
            self.volume_sequence += 1
            self.volume_chain = digest
            if duplicate:
                continue  # Several complete frames may survive a delayed error.
            self.sequence += 1
            self.chain = digest
            size = BLOCK_SIZE + ((head["length"] + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE
            if size > self.replay_limit:
                raise BackupError("Chunk exceeds the declared recovery window")
            self.history[head["sequence"]] = (head["previous"], digest, size)
            self.history_bytes += size
            while self.history_bytes > self.replay_limit:
                self.history_bytes -= self.history.popitem(last=False)[1][2]
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


def stop_process(process, *, close_streams=True):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for name in ('_tape_log_thread', '_tape_result_thread'):
        relay = getattr(process, name, None)
        if relay is not None:
            relay.join()
    if close_streams:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except BrokenPipeError:
                    pass


class ReadAhead:
    """A bounded queue; reserve a slot before reading, release after delivery."""

    def __init__(self, frames, cancel, progress, slots=2):
        self.frames, self.cancel, self.progress = frames, cancel, progress
        self.condition = threading.Condition()
        self.pending = deque()
        self.slots = slots
        self.stopped = self.done = False
        self.error = None
        self.thread = threading.Thread(target=self.read, name="tape-backup-reader", daemon=True)
        self.consumer = self.consume()

    def read(self):
        try:
            with closing(self.frames):
                while True:
                    with self.condition:
                        if not self.slots:
                            self.progress.reader_state = "waiting (buffers full)"
                        self.condition.wait_for(lambda: self.slots or self.stopped)
                        if self.stopped:
                            return
                        self.slots -= 1  # Reserve space before filling a chunk.
                        self.progress.reader_state = "reading"
                    try:
                        frame = next(self.frames)
                    except StopIteration:
                        return
                    with self.condition:
                        if self.stopped:
                            return
                        self.pending.append(frame)
                        self.progress.queued_bytes += len(frame[1])
                        del frame
                        self.condition.notify_all()
        except BaseException as exc:
            self.error = exc
        finally:
            with self.condition:
                self.done = True
                self.progress.reader_state = ("stopped" if self.stopped else
                                              "failed" if self.error else "finished")
                self.condition.notify_all()

    def consume(self):
        while True:
            with self.condition:
                if not self.pending and not self.done:
                    self.progress.phase = "waiting for source"
                self.condition.wait_for(lambda: self.pending or self.done)
                if not self.pending:
                    if self.error is not None:
                        raise self.error
                    return
                frame = self.pending.popleft()
                self.progress.queued_bytes -= len(frame[1])
            try:
                yield frame
            finally:
                del frame
                with self.condition:
                    self.slots += 1
                    self.condition.notify_all()

    def __enter__(self):
        self.progress.reader_state = "starting"
        self.thread.start()
        return self.consumer

    def __exit__(self, *args):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        try:
            if self.thread.is_alive():
                # Terminate the source to unblock reads; its reader owns the
                # stream until it exits, so do not close it from this thread.
                self.cancel()
        finally:
            self.thread.join()
            self.consumer.close()
            self.pending.clear()
            self.progress.queued_bytes = 0


def inspect_backup(media, backup_id=None):
    """Read the first volume's header without scanning archive or snapshot data."""
    reader = StreamReader(media, backup_id)
    try:
        reader.next_volume()
        return {**reader.job, "volume": reader.number, "header_verified": True,
                "data_verified": False, "completion_verified": False}
    finally:
        reader.close()


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


def list_files(media, backup_id=None):
    """List one archive without extracting; verify its complete framed stream."""
    require_tar()
    if backup_id is not None:
        valid_id(backup_id)
    with Progress('Listing files') as progress:
        reader = StreamReader(media, backup_id, progress)
        process = None
        try:
            reader.next_volume()
            if reader.job.get('archive_type', 'tar') != 'tar':
                raise BackupError('ZFS streams have no tar file listing; use inspect or restore the dataset with zfs-restore')
            progress.phase = f"listing {reader.backup_id}"
            log(f"Listing {reader.job['level']} backup {reader.backup_id}; reading all volumes")
            # Consume through EOF, including tar's zero padding, while the
            # reader checks the snapshot and completion marker after the data.
            process = logged_process(['tar', '--list', '--file=-', '--ignore-zeros',
                                      '--quoting-style=escape'], stdin=subprocess.PIPE,
                                     result_stdout=True, env=external_env())
            for kind, payload in reader.frames():
                if kind == 'data':
                    process.stdin.write(payload)
                    progress.read_bytes += len(payload)
                    progress.written_bytes += len(payload)
            process.stdin.close()
            if process.wait():
                raise BackupError('GNU tar listing failed; output may be incomplete')
            stop_process(process)
            if process._tape_output_errors:
                raise BackupError(f'File listing output failed: {process._tape_output_errors[0]}')
            progress.phase = 'complete'
            return reader.summary
        finally:
            stop_process(process)
            reader.close()


def prepare_append(media, backup_id, snapshot_fd):
    media.append_mode = True
    cached = media.cached_base(backup_id, snapshot_fd)
    if cached is not None:
        log(f'Loaded snapshot for {backup_id} from the final metadata file; no archive scan')
        return cached
    log('No usable final metadata file; scanning the base and checking the recorded tail')
    os.ftruncate(snapshot_fd, 0)
    os.lseek(snapshot_fd, 0, os.SEEK_SET)
    with Progress('Preparing append') as progress:
        reader = StreamReader(media, backup_id, progress)
        try:
            for kind, payload in reader.frames(skip_data=True):
                if kind == 'snapshot':
                    with os.fdopen(os.dup(snapshot_fd), 'ab', buffering=0) as snapshot:
                        snapshot.write(payload)
            volume = reader.volume
            record = volume.next_record()
            if record is not None:
                if decoded_header(record).get('type') != 'metadata':
                    raise BackupError('Append requires the last completed backup; recorded data follows the base')
                cached = read_metadata(volume, record)
                if cached['summary']['id'] != backup_id:
                    raise BackupError('Metadata does not belong to the requested base')
                media.entries = cached['entries']
                if volume.next_record() is not None:
                    raise BackupError('Recorded data follows the base metadata; select the latest completed base')
            end = volume.position()
            if volume.seek_end() != end:
                raise BackupError('Recorded tail is ambiguous; refusing to append')
            volume.append_end = end
            media.append_volume, reader.volume = volume, None
            return reader.summary
        finally:
            reader.close()


@contextmanager
def append_resources(media):
    try:
        yield
    finally:
        if getattr(media, 'append_volume', None) is not None:
            media.append_volume.close()
            media.append_volume = None
        media.append_mode = False


def listing_entry(head, cartridge, method):
    if head['type'] != 'volume' or head['format'] not in (3, 4):
        raise BackupError('Unsupported tape volume header')
    job = head['backup']
    valid_id(job['id'])
    if (type(head['volume']) is not int or head['volume'] < 1 or
            job['level'] not in ('full', 'incremental') or not isinstance(job['source'], str) or
            (job['level'] == 'full' and job['parent'] is not None)):
        raise BackupError('Invalid backup metadata in cartridge listing')
    if job['level'] == 'incremental':
        valid_id(job['parent'])
    return {**job, 'volume': head['volume'], 'cartridge': cartridge,
            'header_verified': True, 'data_verified': False, 'completion_verified': False,
            'completion_marker_present': None if method == 'catalog' else False,
            'listing_method': method}


def catalog_listing(volume, cartridge):
    """Read the final metadata and seek to its headers, without archive reads."""
    cached = latest_metadata(volume)
    if cached is None:
        return None
    results = []
    previous_position = previous_bytes = -1
    for entry in cached['entries']:
        if (entry['position'] <= previous_position or entry['record_bytes'] <= previous_bytes or
                (not results and (entry['position'] != 0 or entry['record_bytes'] != 0))):
            raise BackupError('Catalog locations are not in cartridge order')
        previous_position, previous_bytes = entry['position'], entry['record_bytes']
        volume.seek_position(entry['position'])
        record = volume.next_record()
        if record is None or hashlib.sha256(record).hexdigest() != entry['header_sha256']:
            raise BackupError('Catalog backup header checksum mismatch')
        head = decoded_header(record)
        info = listing_entry(head, cartridge, 'catalog')
        if info['id'] != entry['id'] or info['volume'] != entry['volume']:
            raise BackupError('Catalog backup identity differs from its header')
        results.append(info)
    if any(cached['summary'].get(k) != v for k, v in head['backup'].items()):
        raise BackupError('Metadata summary differs from the backup header')
    return results


def inspect_all(media, *, use_catalog=True, allow_scan=True, progress=None):
    results, errors = [], []
    for cartridge, volume in media.cartridges():
        if progress:
            progress.phase = f'locating catalog on {cartridge}'
        current = None
        try:
            if use_catalog:
                try:
                    entries = catalog_listing(volume, cartridge)
                except (BackupError, EndVolume, OSError, KeyError, TypeError, ValueError):
                    entries = None
                if entries is not None:
                    log(f'Listed {len(entries)} backup segment(s) on {cartridge} from the final catalog; no archive scan')
                    results.extend(entries)
                    continue
                if not allow_scan:
                    errors.append({'cartridge': cartridge, 'scan_required': True,
                                   'error': 'No usable final catalog; use inspect --scan for a full cartridge scan'})
                    continue
                log(f'No usable final catalog on {cartridge}; scanning backup segments')
                volume.seek_position(0)
                volume.record_bytes = 0
            if progress:
                progress.phase = f'scanning backup segments on {cartridge}'
            while True:
                record = volume.next_record()
                if record is None:
                    break
                head = decoded_header(record)
                if head.get('type') == 'volume':
                    current = listing_entry(head, cartridge, 'scan')
                    results.append(current)
                elif head.get('type') == 'metadata':
                    read_metadata(volume, record)
                elif head.get('type') == 'chunk':
                    length = head.get('length')
                    if current is None or type(length) is not int or not 0 < length <= FRAME_SIZE:
                        raise BackupError('Invalid chunk in cartridge listing')
                    if head['kind'] == 'end':
                        _, digest = read_payload(volume, length)
                        if digest != head['sha256']:
                            raise BackupError('Corrupt completion record')
                        current['completion_marker_present'] = True
                    else:
                        volume.skip_payload(length)
                else:
                    raise BackupError('Unknown record in cartridge listing')
        except (BackupError, EndVolume, OSError, KeyError, TypeError) as exc:
            # Keep earlier headers visible if a later append was interrupted.
            errors.append({'cartridge': cartridge, 'error': str(exc) or 'Incomplete tape record'})
    return {'backups': results, 'data_verified': False,
            'scan_complete': not errors, 'errors': errors}


def normalize_exclusions(patterns):
    normalized = []
    for pattern in patterns or []:
        if not isinstance(pattern, str) or not pattern or '\0' in pattern or pattern.startswith('/'):
            raise BackupError('Exclusions must be nonempty patterns relative to --source')
        while pattern.startswith('./'):
            pattern = pattern[2:]
        pattern = pattern.rstrip('/')
        if not pattern or any(part in ('.', '..') for part in pattern.split('/')):
            raise BackupError('Exclusion patterns must stay inside --source; do not exclude the source root')
        normalized.append(pattern)
    normalized = sorted(set(normalized))
    if len(json.dumps(normalized).encode()) > 16 * 1024:
        raise BackupError('Exclusion policy exceeds 16 KiB; simplify its patterns')
    return normalized


def exclusion_options(patterns=None, files=None):
    if patterns is None and files is None:
        return None
    result = list(patterns or [])
    for path in files or []:
        with open(path, encoding='utf-8', newline=None) as stream:
            result.extend(line.rstrip('\n') for line in stream if line.rstrip('\n'))
    return normalize_exclusions(result)


def zfs_name(value, *, snapshot=False):
    if not isinstance(value, str):
        raise BackupError('ZFS names must be strings')
    parts = value.split('@')
    if len(parts) != (2 if snapshot else 1):
        raise BackupError('Use DATASET@SNAPSHOT' if snapshot else 'Use POOL/DATASET, without @SNAPSHOT')
    if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9_.:%-]+)*', parts[0]) or
            any(part in ('.', '..') for part in parts[0].split('/')) or
            (snapshot and not re.fullmatch(r'[A-Za-z0-9_.:%-]+', parts[1]))):
        raise BackupError(f'Invalid ZFS dataset or snapshot name: {value!r}')
    return parts[0]


def zfs_properties(name, properties):
    output = run_command(['zfs', 'get', '-H', '-p', '-o', 'property,value', ','.join(properties), name])
    result = dict(line.split('\t', 1) for line in output.splitlines() if '\t' in line)
    if any(key not in result for key in properties):
        raise BackupError(f'Incomplete ZFS property response for {name}')
    return result


def validate_zfs_metadata(metadata):
    try:
        if not isinstance(metadata, dict):
            raise ValueError('missing metadata')
        if zfs_name(metadata['snapshot'], snapshot=True) != metadata['dataset']:
            raise ValueError('snapshot/dataset mismatch')
        if not re.fullmatch(r'[0-9]+', metadata['guid']) or type(metadata['raw']) is not bool:
            raise ValueError('invalid snapshot GUID or send mode')
        if metadata['base_snapshot'] is not None:
            if (zfs_name(metadata['base_snapshot'], snapshot=True) != metadata['dataset'] or
                    not re.fullmatch(r'[0-9]+', metadata['base_guid'])):
                raise ValueError('invalid incremental snapshot identity')
        elif metadata['base_guid'] is not None:
            raise ValueError('full stream has an incremental GUID')
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError(f'Invalid ZFS backup metadata: {exc}') from exc
    return metadata


def zfs_send_command(metadata, *, dry_run=False):
    flags = ['-nP'] if dry_run else []
    flags += ['-p', '-w' if metadata['raw'] else '-c']
    if metadata['base_snapshot']:
        flags += ['-i', metadata['base_snapshot']]
    return ['zfs', 'send', *flags, metadata['snapshot']]


def prepare_zfs(snapshot, parent=None, *, raw=False):
    dataset = zfs_name(snapshot, snapshot=True)
    if type(raw) is not bool:
        raise BackupError('ZFS raw mode must be true or false')
    if not shutil.which('zfs'):
        raise BackupError('OpenZFS zfs command is required on the source machine')
    properties = zfs_properties(dataset, ['type', 'encryption'])
    if properties['type'] != 'filesystem':
        raise BackupError('Native ZFS backups currently require a filesystem dataset')
    if properties['encryption'] != 'off' and not raw:
        raise BackupError('Encrypted ZFS datasets require --raw to preserve encryption on tape')
    current = zfs_properties(snapshot, ['type', 'guid'])
    if current['type'] != 'snapshot':
        raise BackupError('ZFS source must be an existing snapshot')
    if parent is not None:
        validate_zfs_metadata(parent)
        if parent['dataset'] != dataset or parent['raw'] != raw:
            raise BackupError('ZFS dataset or raw send mode differs from the parent backup')
        old = zfs_properties(parent['snapshot'], ['guid'])
        if old['guid'] != parent['guid'] or current['guid'] == parent['guid']:
            raise BackupError('ZFS base snapshot changed, or target is already backed up')
    metadata = validate_zfs_metadata({'dataset': dataset, 'snapshot': snapshot, 'guid': current['guid'],
        'raw': raw, 'base_snapshot': parent['snapshot'] if parent else None,
        'base_guid': parent['guid'] if parent else None})
    estimate = run_command(zfs_send_command(metadata, dry_run=True))
    sizes = [line.split('\t')[-1] for line in estimate.splitlines() if line.startswith('size\t')]
    if not sizes or not sizes[-1].isdigit() or int(sizes[-1]) <= 0:
        raise BackupError('ZFS send did not return a valid size estimate')
    return {'source': dataset, 'estimated_bytes': int(sizes[-1]), 'zfs': metadata}


def start_zfs(metadata):
    for name, guid in ((metadata['snapshot'], metadata['guid']),
                       (metadata['base_snapshot'], metadata['base_guid'])):
        if name and zfs_properties(name, ['guid'])['guid'] != guid:
            raise BackupError('ZFS snapshot identity changed before streaming')
    return logged_process(zfs_send_command(metadata), stdout=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, env=external_env())


def zfs_frames(metadata, buffer_size, process=None, progress=None):
    process = process or start_zfs(metadata)
    try:
        for chunk in read_chunks(process.stdout, buffer_size, progress):
            yield 'data', chunk
        if process.wait():
            raise BackupError('ZFS send failed; this backup has no completion marker')
        # The framing's snapshot section carries the immutable ZFS send identity.
        yield 'snapshot', json.dumps(metadata, sort_keys=True).encode()
    finally:
        stop_process(process)


def backup_zfs(snapshot, media, *, base=None, raw=None, volume_size=None,
               buffer_size=DEFAULT_BUFFER, quiet=False, ssh=None):
    zfs_name(snapshot, snapshot=True)
    if not BLOCK_SIZE <= buffer_size <= MAX_BUFFER:
        raise BackupError('Buffer size must be between 64KiB and 10GiB')
    if volume_size is not None and volume_size < 4 * BLOCK_SIZE:
        raise BackupError('Volume size must be at least 256KiB')
    frame_size = min(FRAME_SIZE, max(BLOCK_SIZE, (buffer_size // BLOCK_SIZE - 1) * BLOCK_SIZE))
    if volume_size:
        frame_size = min(frame_size, (volume_size // BLOCK_SIZE - 2) * BLOCK_SIZE)
    with media.lock(), ram_snapshot() as snapshot_fd, append_resources(media):
        parent = None
        if base:
            valid_id(base)
            parent = prepare_append(media, base, snapshot_fd)
            if parent.get('archive_type') != 'zfs' or parent.get('ssh') != (ssh.metadata if ssh else None):
                raise BackupError('ZFS incremental requires a ZFS parent from the same source host')
            validate_zfs_metadata(parent.get('zfs'))
        raw = parent['zfs']['raw'] if raw is None and parent else bool(raw)
        options = {'parent': parent['zfs'] if parent else None, 'raw': raw}
        remote = RemoteSource(ssh, snapshot, snapshot_fd, None, frame_size, quiet,
                              zfs_options=options) if ssh else None
        try:
            metadata = remote.prepare() if remote else prepare_zfs(snapshot, **options)
            if parent and parent['source'] != metadata['source']:
                raise BackupError('ZFS source differs from parent backup')
            return write_backup(metadata['source'], media, 'incremental' if base else 'full', base,
                                volume_size, buffer_size, quiet, snapshot_fd, metadata['estimated_bytes'],
                                remote, frame_size, zfs=metadata['zfs'])
        finally:
            if remote:
                remote.close()


ZFS_RESTORE_PROPERTY = 'org.tape-backup:restore'


def zfs_target_exists(dataset):
    parent, separator, leaf = dataset.rpartition('/')
    if not separator:
        raise BackupError('ZFS restore destination must be a child dataset, e.g. pool/recovered')
    output = run_command(['zfs', 'list', '-H', '-o', 'name', '-r', parent])
    names = output.splitlines()
    if parent not in names:
        raise BackupError('ZFS destination parent could not be verified')
    return dataset in names


def check_zfs_base(dataset, metadata):
    expected = metadata['base_guid']
    if expected is None:
        return
    snapshot = dataset + '@' + metadata['base_snapshot'].split('@', 1)[1]
    if zfs_properties(snapshot, ['guid'])['guid'] != expected:
        raise BackupError('ZFS destination does not have the required base snapshot GUID')
    rows = run_command(['zfs', 'list', '-H', '-p', '-t', 'snapshot', '-o', 'name,guid',
                        '-s', 'createtxg', '-d', '1', dataset]).splitlines()
    own = [row.split('\t', 1) for row in rows if row.startswith(dataset + '@')]
    if not own or own[-1][1] != expected:
        raise BackupError('ZFS destination has a newer snapshot; refusing to roll it back')


def restore_zfs(backup_ids, dataset, media):
    zfs_name(dataset)
    if not backup_ids or len(set(backup_ids)) != len(backup_ids):
        raise BackupError('Specify a ZFS full/incremental chain without duplicate backup IDs')
    if not shutil.which('zfs'):
        raise BackupError('OpenZFS zfs command is required on the restore machine')
    for backup_id in backup_ids:
        valid_id(backup_id)
    lock_id = hashlib.sha256(('zfs:' + dataset).encode()).hexdigest()
    with locked(Path.home() / '.cache/tape-backup/restores' / lock_id), media.lock():
        exists = zfs_target_exists(dataset)
        previous_id = None
        if exists:
            state = zfs_properties(dataset, [ZFS_RESTORE_PROPERTY, 'mounted', 'readonly'])
            previous_id = state[ZFS_RESTORE_PROPERTY]
            if state['mounted'] != 'no':
                raise BackupError('Unmount the ZFS restore destination before applying another incremental')
            if state['readonly'] != 'on':
                raise BackupError('Keep the ZFS destination readonly between incremental restores')
            if previous_id.startswith('pending:'):
                raise BackupError('A previous ZFS receive did not complete; restore into a separate dataset')
            if not re.fullmatch('[0-9a-f]{32}', previous_id):
                raise BackupError('Existing ZFS destination is not a completed tape-backup restore; choose a new dataset')
        verified, previous = {}, None
        # Verify every requested stream before any receive can alter a dataset.
        for backup_id in backup_ids:
            summary = scan(media, backup_id)
            if summary.get('archive_type') != 'zfs':
                raise BackupError('zfs-restore requires native ZFS backups; use restore for tar archives')
            metadata = validate_zfs_metadata(summary.get('zfs'))
            if summary['parent'] != previous_id:
                raise BackupError('Missing or out-of-order ZFS parent backup')
            if summary['level'] != ('incremental' if previous_id else 'full'):
                raise BackupError('ZFS restore chain must start with a full backup in a new dataset')
            if previous is not None and (metadata['base_guid'] != previous['zfs']['guid'] or
                    metadata['dataset'] != previous['zfs']['dataset'] or
                    metadata['raw'] != previous['zfs']['raw'] or summary.get('ssh') != previous.get('ssh')):
                raise BackupError('ZFS snapshot chain identity does not match its parent')
            if previous is None and exists:
                check_zfs_base(dataset, metadata)
            verified[backup_id] = summary
            previous, previous_id = summary, backup_id
        with Progress('ZFS restore') as progress:
            for backup_id in backup_ids:
                reader, process = StreamReader(media, backup_id, progress), None
                try:
                    reader.next_volume()
                    expected = verified[backup_id]
                    if any(expected.get(k) != v for k, v in reader.job.items()):
                        raise BackupError('ZFS backup header changed after verification')
                    metadata = validate_zfs_metadata(reader.job.get('zfs'))
                    if exists:
                        state = zfs_properties(dataset, [ZFS_RESTORE_PROPERTY, 'mounted', 'readonly'])
                        if (state[ZFS_RESTORE_PROPERTY] != reader.job['parent'] or
                                state['mounted'] != 'no' or state['readonly'] != 'on'):
                            raise BackupError('ZFS destination changed after verification')
                        check_zfs_base(dataset, metadata)
                        run_command(['zfs', 'set', f'{ZFS_RESTORE_PROPERTY}=pending:{backup_id}', dataset])
                    elif zfs_target_exists(dataset):
                        raise BackupError('ZFS destination appeared after verification; refusing to overwrite it')
                    progress.phase = f'receiving {backup_id} into {dataset}'
                    progress.estimate(reader.job.get('estimated_bytes'))
                    # Never use -F or mount the received filesystem. Explicit
                    # properties prevent incoming mount/share settings taking effect.
                    command = ['zfs', 'receive', '-u', '-o', 'readonly=on',
                               '-o', 'canmount=off', '-o', 'mountpoint=none',
                               '-o', 'sharenfs=off', '-o', 'sharesmb=off',
                               '-o', f'{ZFS_RESTORE_PROPERTY}=pending:{backup_id}', dataset]
                    process = logged_process(command, stdin=subprocess.PIPE,
                                             log_stdout=True, env=external_env())
                    for kind, payload in reader.frames():
                        if kind == 'data':
                            process.stdin.write(payload)
                            progress.read_bytes += len(payload)
                            progress.written_bytes += len(payload)
                    process.stdin.close()
                    if process.wait():
                        raise BackupError('ZFS receive failed; destination may be incomplete. No rollback was requested')
                    if reader.summary != expected:
                        raise BackupError('ZFS stream changed between verification and receive')
                    received = dataset + '@' + metadata['snapshot'].split('@', 1)[1]
                    if zfs_properties(received, ['guid'])['guid'] != metadata['guid']:
                        raise BackupError('Received ZFS snapshot GUID differs from the backup')
                    run_command(['zfs', 'set', f'{ZFS_RESTORE_PROPERTY}={backup_id}', dataset])
                    exists = True
                finally:
                    stop_process(process)
                    reader.close()
            progress.phase = 'complete'
    return dataset


def is_excluded(relative, patterns):
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def estimate_source(source, parent_created=None, excludes=None):
    """Approximate tar size from stat metadata only; never read source payloads."""
    cutoff = datetime.fromisoformat(parent_created).timestamp() if parent_created else None
    total, entries, hardlinks = 10240, 0, set()
    def scan_error(exc):
        raise exc
    with Progress("Source inventory", transfer=False) as progress:
        for directory, subdirs, files in os.walk(source, followlinks=False,
                                                onerror=scan_error):
            relative = Path(directory).relative_to(source)
            subdirs[:] = [name for name in subdirs
                          if not is_excluded((relative / name).as_posix(), excludes or [])]
            files = [name for name in files
                     if not is_excluded((relative / name).as_posix(), excludes or [])]
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


def start_archive(source, snapshot_fd, quiet, excludes=None):
    args = ["tar", "--create", "--format=posix", "--acls", "--xattrs", "--sparse",
            "--numeric-owner", f"--listed-incremental=/proc/self/fd/{snapshot_fd}",
            "--file=-", f"--directory={source}", *([] if quiet else ["--verbose"]),
            '--anchored', '--wildcards', '--wildcards-match-slash',
            *(f'--exclude=./{pattern}' for pattern in excludes or []), '--no-wildcards', "--", "."]
    return logged_process(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          env=external_env(), pass_fds=(snapshot_fd,))


def archive_frames(source, snapshot_fd, buffer_size, quiet, process=None, progress=None, excludes=None):
    """Produce tar and its updated RAM snapshot, locally or on the SSH source."""
    if process is None:
        process = start_archive(source, snapshot_fd, quiet, excludes)
    try:
        for chunk in read_chunks(process.stdout, buffer_size, progress):
            yield "data", chunk
        if process.wait():
            raise BackupError("GNU tar failed; this tape set has no completion marker and must be restarted")
        os.lseek(snapshot_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(snapshot_fd), "rb", buffering=0) as snapshot:
            for chunk in read_chunks(snapshot, buffer_size):
                yield "snapshot", chunk
    finally:
        stop_process(process)


def read_exact(stream, size, progress=None):
    result = bytearray()
    while len(result) < size:
        block = stream.read(min(1024**2, size - len(result)))
        if not block:
            raise BackupError("SSH source disconnected or returned an incomplete stream; "
                              "check the SSH errors above and the remote binary version")
        result.extend(block)
        if progress:
            progress.read_bytes += len(block)
    return result


def send_packet(stream, kind, payload=b""):
    stream.write(PACKET_HEADER.pack(kind, len(payload)))
    stream.write(payload)
    stream.flush()


def receive_packet(stream, limit, progress=None):
    kind, size = PACKET_HEADER.unpack(read_exact(stream, PACKET_HEADER.size))
    if size > limit:
        raise BackupError("SSH source packet exceeds the negotiated buffer size")
    return kind, read_exact(stream, size, progress if kind == b"d" else None)


def receive_json(stream):
    kind, payload = receive_packet(stream, BLOCK_SIZE)
    try:
        fields = json.loads(payload)
        if kind != b"j" or not isinstance(fields, dict):
            raise ValueError("expected a metadata object")
        return fields
    except (ValueError, TypeError) as exc:
        raise BackupError(f"Invalid SSH source metadata: {exc}") from exc


class SSHConfig:
    def __init__(self, host, *, port=None, identity=None, config=None, program="tape-backup"):
        if not host or host.startswith("-") or any(c.isspace() or ord(c) < 32 for c in host):
            raise BackupError("--ssh must be a host alias or user@host")
        if port is not None and not 1 <= port <= 65535:
            raise BackupError("--ssh-port must be between 1 and 65535")
        if not program or "\0" in program:
            raise BackupError("--remote-program must name the executable on the source machine")
        self.host, self.port = host, port
        self.identity, self.config, self.program = identity, config, program

    @property
    def metadata(self):
        return {"host": self.host, "port": self.port}

    def command(self):
        # SSH invokes a remote shell, so quote the executable as one shell word.
        # Source paths travel as JSON over stdin, never in a shell command.
        args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
        for flag, value in (("-p", self.port), ("-i", self.identity), ("-F", self.config)):
            if value is not None:
                args.extend((flag, str(value)))
        return [*args, "--", self.host, shlex.join([self.program, "_ssh-source"])]


class RemoteSource:
    def __init__(self, config, source, snapshot_fd, parent_created, buffer_size, quiet, *, excludes=None,
                 zfs_options=None):
        self.config, self.source, self.snapshot_fd = config, source, snapshot_fd
        self.parent_created, self.buffer_size, self.quiet = parent_created, buffer_size, quiet
        self.process = None
        self.excludes = normalize_exclusions(excludes)
        self.zfs_options = zfs_options

    def prepare(self):
        log(f"Connecting to SSH source {self.config.host}")
        self.process = logged_process(self.config.command(), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, env=external_env())
        process = self.process
        if read_exact(process.stdout, len(SSH_MAGIC)) != SSH_MAGIC:
            raise BackupError("Unsupported SSH source protocol; install the same tape-backup version "
                              "on the source and keep remote shell startup output off stdout")
        request = {"source": str(self.source), "parent_created": self.parent_created,
                   "buffer_size": self.buffer_size, "quiet": self.quiet, 'excludes': self.excludes}
        if self.zfs_options is not None:
            request['zfs'] = self.zfs_options
        send_packet(process.stdin, b"j", json.dumps(request).encode())
        os.lseek(self.snapshot_fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(self.snapshot_fd), "rb", buffering=0) as snapshot:
            for chunk in read_chunks(snapshot, BLOCK_SIZE):
                send_packet(process.stdin, b"s", chunk)
        send_packet(process.stdin, b"e")
        with Progress("SSH source inventory", transfer=False) as progress:
            progress.phase = f"waiting for metadata from {self.config.host}"
            metadata = receive_json(process.stdout)
        if (not isinstance(metadata.get("source"), str) or
                (self.zfs_options is None and not Path(metadata['source']).is_absolute()) or
                type(metadata.get("estimated_bytes")) is not int or metadata["estimated_bytes"] <= 0):
            raise BackupError("Invalid SSH source path or size estimate")
        if metadata.get('excludes') != self.excludes:
            raise BackupError('SSH source did not confirm the requested exclusion policy')
        if self.zfs_options is not None:
            validate_zfs_metadata(metadata.get('zfs'))
            if metadata['zfs']['snapshot'] != str(self.source):
                raise BackupError('SSH source returned a different ZFS snapshot')
        return metadata

    def frames(self, progress=None):
        send_packet(self.process.stdin, b"g")  # Start only after the first tape is loaded.
        self.process.stdin.close()
        phase = b"d"
        while True:
            kind, payload = receive_packet(self.process.stdout, self.buffer_size, progress)
            if kind == b"e" and not payload:
                break
            if kind not in (b"d", b"s") or not payload or (phase == b"s" and kind == b"d"):
                raise BackupError("Invalid or out-of-order SSH archive packet")
            phase = kind
            yield "data" if kind == b"d" else "snapshot", payload
        if self.process.stdout.read(1) or self.process.wait():
            raise BackupError("SSH source failed after streaming; this tape set is incomplete")

    def close(self):
        stop_process(self.process)


def serve_ssh_source():
    """Private versioned protocol; only archive bytes/metadata go to stdout."""
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    outgoing.write(SSH_MAGIC)
    outgoing.flush()
    request = receive_json(incoming)
    try:
        zfs_options = request.get('zfs')
        source = request['source'] if zfs_options is not None else Path(request["source"])
        buffer_size, quiet = request["buffer_size"], request["quiet"]
        parent_created = request["parent_created"]
        excludes = normalize_exclusions(request.get('excludes', []))
        if ((zfs_options is None and not source.is_absolute()) or type(buffer_size) is not int or
                not BLOCK_SIZE <= buffer_size <= MAX_BUFFER or type(quiet) is not bool):
            raise ValueError("expected an absolute source path and a valid buffer size")
        if parent_created is not None:
            datetime.fromisoformat(parent_created)
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError(f"Invalid SSH source request: {exc}") from exc
    if zfs_options is None:
        source = source.resolve()
        if not source.is_dir():
            raise BackupError(f"Source is not a directory: {source}")
        require_tar()
    elif excludes or not isinstance(zfs_options, dict):
        raise BackupError('Invalid ZFS source options; file exclusions apply only to tar backups')
    with ram_snapshot() as snapshot_fd:
        with os.fdopen(os.dup(snapshot_fd), "wb", buffering=0) as snapshot:
            while True:
                kind, payload = receive_packet(incoming, BLOCK_SIZE)
                if kind == b"e" and not payload:
                    break
                if kind != b"s" or not payload:
                    raise BackupError("Invalid SSH incremental snapshot packet")
                snapshot.write(payload)
        if zfs_options is not None:
            metadata = prepare_zfs(source, zfs_options.get('parent'), raw=zfs_options.get('raw', False))
            metadata['excludes'] = []
        else:
            metadata = {"source": str(source), "estimated_bytes": estimate_source(source, parent_created, excludes),
                        'excludes': excludes}
        send_packet(outgoing, b"j", json.dumps(metadata).encode())
        if receive_packet(incoming, 0) != (b"g", b""):
            raise BackupError("Missing SSH source start request")
        frames = (zfs_frames(metadata['zfs'], buffer_size) if zfs_options is not None else
                  archive_frames(source, snapshot_fd, buffer_size, quiet, excludes=excludes))
        with closing(frames):
            for kind, payload in frames:
                send_packet(outgoing, b"d" if kind == "data" else b"s", payload)
        send_packet(outgoing, b"e")


def backup(source, media, *, level="full", base=None, volume_size=None,
           buffer_size=DEFAULT_BUFFER, quiet=False, ssh=None, append=False, excludes=None):
    source = Path(source) if ssh else Path(source).resolve()
    if ssh and not source.is_absolute():
        raise BackupError("SSH --source must be an absolute path on the remote machine")
    if not ssh and not source.is_dir():
        raise BackupError(f"Source is not a directory: {source}")
    if not ssh and isinstance(media, FileMedia) and (inside(media.directory, source) or
                                                    inside(source, media.directory)):
        raise BackupError("Source and media directories must be separate")
    if level not in ("full", "incremental") or (level == "incremental") != bool(base):
        raise BackupError("Incremental backup requires --base ID; full backup must not use --base")
    if append and level != 'incremental':
        raise BackupError('--append requires --level incremental and --base')
    # Incrementals always retain their ancestors. --append remains accepted
    # for existing scripts, but omitting it must never select overwrite mode.
    append = level == 'incremental'
    excludes = normalize_exclusions(excludes) if excludes is not None else None
    if not BLOCK_SIZE <= buffer_size <= MAX_BUFFER:
        raise BackupError("Buffer size must be between 64KiB and 10GiB")
    if volume_size is not None:
        if volume_size < 4 * BLOCK_SIZE:
            raise BackupError("Volume size must be at least 256KiB")
    frame_size = min(FRAME_SIZE, max(BLOCK_SIZE, (buffer_size // BLOCK_SIZE - 1) * BLOCK_SIZE))
    if volume_size:
        frame_size = min(frame_size, (volume_size // BLOCK_SIZE - 2) * BLOCK_SIZE)
    if not ssh:
        require_tar()
    with media.lock(), ram_snapshot() as snapshot_fd, append_resources(media):
        parent = None
        parent_created = None
        if base:
            valid_id(base)
            log(f"Loading the incremental snapshot from backup {base}")
            parent = (prepare_append(media, base, snapshot_fd) if append else
                      scan(media, base, snapshot_fd, verify=False))
            if parent.get("ssh") != (ssh.metadata if ssh else None):
                raise BackupError("Incremental SSH source differs from the parent backup")
            parent_created = parent["created"]
            if parent.get('archive_type', 'tar') != 'tar':
                raise BackupError('A tar incremental requires a tar parent; use zfs-backup for ZFS streams')
            inherited = normalize_exclusions(parent.get('excludes', []))
            if excludes is not None and excludes != inherited:
                raise BackupError('Exclusions differ from the parent backup; start a new full backup to change them')
            excludes = inherited
        excludes = excludes or []
        remote = RemoteSource(ssh, source, snapshot_fd, parent_created, frame_size, quiet,
                              excludes=excludes) if ssh else None
        try:
            if remote:
                metadata = remote.prepare()
                source, estimated_bytes = metadata["source"], metadata["estimated_bytes"]
            else:
                log("Estimating archive size from file metadata; file contents are not staged")
                estimated_bytes = estimate_source(source, parent_created, excludes)
            if parent and parent["source"] != str(source):
                raise BackupError("Incremental source differs from the parent backup")
            return write_backup(source, media, level, base, volume_size, buffer_size, quiet,
                                snapshot_fd, estimated_bytes, remote, frame_size, excludes=excludes)
        finally:
            if remote:
                remote.close()


def write_backup(source, media, level, base, volume_size, buffer_size, quiet,
                 snapshot_fd, estimated_bytes, remote, frame_size, *, excludes=None, zfs=None):
    job = {"id": uuid.uuid4().hex, "level": level, "parent": base, "source": str(source),
           "created": datetime.now(timezone.utc).isoformat(), "estimated_bytes": estimated_bytes}
    if remote:
        job["ssh"] = remote.config.metadata
    if excludes:
        job['excludes'] = excludes
    if zfs is not None:
        job.update(archive_type='zfs', zfs=zfs)
    label = f"{remote.config.host}:{source}" if remote else source
    log(f"Streaming {level} backup {job['id']} from {label}; "
        f"buffer {buffer_size / 1024**2:g} MiB; continuous writes, "
        f"{frame_size / 1024**2:g} MiB frames, background reader; "
        "no disk archive or state directory")
    with Progress(job["id"], buffer_size=buffer_size) as progress, ram_snapshot() as metadata_snapshot:
        progress.estimate(estimated_bytes)
        writer = StreamWriter(media, job, volume_size, progress, buffer_size)
        process = None
        try:
            writer.next_volume()  # Load the tape before starting the source scan.
            data_hash = hashlib.sha256()
            snapshot_hash, snapshot_bytes = hashlib.sha256(), 0
            if remote:
                process, source_frames = remote.process, remote.frames(progress)
            elif zfs is not None:
                process = start_zfs(zfs)
                source_frames = zfs_frames(zfs, frame_size, process, progress)
            else:
                process = (start_archive(source, snapshot_fd, quiet, excludes) if excludes else
                           start_archive(source, snapshot_fd, quiet))
                source_frames = archive_frames(source, snapshot_fd, frame_size, quiet, process, progress)
            with ReadAhead(source_frames, lambda: stop_process(process, close_streams=False), progress,
                           slots=max(2, buffer_size // frame_size + 1)) as frames:
                for kind, chunk in frames:
                    progress.phase = f"hashing chunk {writer.sequence}"
                    if kind == "data":
                        data_hash.update(chunk)
                    else:
                        progress.phase = "writing incremental snapshot to tape"
                        snapshot_hash.update(chunk)
                        snapshot_bytes += len(chunk)
                        with os.fdopen(os.dup(metadata_snapshot), 'ab', buffering=0) as snapshot_copy:
                            snapshot_copy.write(chunk)
                    writer.send(kind, chunk)
                    del chunk  # Release it before the reader reuses its slot.
            if not snapshot_bytes or not progress.read_bytes:
                raise BackupError("Source did not produce an archive and incremental snapshot")
            end = {"data_bytes": progress.read_bytes, "data_sha256": data_hash.hexdigest(),
                   "snapshot_bytes": snapshot_bytes, "snapshot_sha256": snapshot_hash.hexdigest(),
                   "chunks": writer.sequence}
            writer.send("end", json.dumps(end, sort_keys=True).encode())
            writer.finish()
            progress.phase = 'writing metadata file'
            metadata_complete, append_ready, warnings = False, True, []
            try:
                metadata_complete = write_metadata(writer.volume, media.entries,
                    {**job, **end, 'volumes': writer.number}, metadata_snapshot, writer.used, volume_size)
                if not metadata_complete:
                    warnings.append('Catalog did not fit; future lookup and append may require a scan')
            except (OSError, BackupError) as exc:
                append_ready = None
                warnings.append(f'Metadata incomplete: {exc}; append readiness is unknown')
                log(f'Backup data committed, but the metadata file could not be completed: {exc}. '
                    'Restore/verify remain available; further appends may be refused until the tail is resolved.')
            writer.close()
            media.last_result = {**job, **end, 'volumes': writer.number, 'archive_complete': True,
                                 'metadata_complete': metadata_complete, 'append_ready': append_ready,
                                 'data_verified': False, 'warnings': warnings}
            progress.phase = "complete"
            log(f"Completed {job['id']}: {progress.read_bytes} archive bytes, {writer.number} volume(s)")
            return job["id"]
        finally:
            try:
                stop_process(process)
            finally:
                writer.close(ignore_errors=True)


def restore_marker_path(destination):
    return destination.with_name(f'.{destination.name}.tape-restore.json')


def directory_identity(destination):
    info = destination.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise BackupError('Restore destination must be a directory, not a symlink or file')
    return [info.st_dev, info.st_ino]


def restore_job(job):
    return {key: job[key] for key in ('id', 'level', 'parent', 'source', 'ssh') if key in job}


def read_restore_marker(destination):
    path = restore_marker_path(destination)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > BLOCK_SIZE:
            raise BackupError(f'Invalid restore history: {path}')
        with os.fdopen(os.dup(fd), 'r') as stream:
            state = json.load(stream)
        if state['version'] != 1 or state['destination'] != str(destination):
            raise BackupError(f'Restore history does not match this directory: {path}')
        valid_id(state['backup']['id'])
        job = state['backup']
        if (job['level'] not in ('full', 'incremental') or not isinstance(job['source'], str) or
                (job['level'] == 'full' and job['parent'] is not None)):
            raise BackupError(f'Invalid restored backup identity: {path}')
        if job['level'] == 'incremental':
            valid_id(job['parent'])
        if not destination.exists() and not destination.is_symlink():
            return None  # A removed restore can be rebuilt from a full backup.
        if state['identity'] != directory_identity(destination):
            # An empty replacement has no usable baseline; it must start with
            # a full backup (or an explicitly asserted --base for an empty tree).
            empty_destination(destination)
            return None
        if state['status'] != 'complete':
            raise BackupError('A previous incremental apply did not finish; this directory may be partially '
                              'updated. Restore the full chain into a separate directory before continuing')
        return state['backup']
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError(f'Invalid restore history: {path}') from exc
    finally:
        os.close(fd)


def write_restore_marker(destination, job, *, pending=None):
    path = restore_marker_path(destination)
    state = {'version': 1, 'destination': str(destination),
             'identity': directory_identity(destination), 'backup': restore_job(job),
             'status': 'applying' if pending else 'complete', 'pending': pending}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', prefix=f'.{destination.name}.restore-history-',
                                         dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_restore_parent(job, parent):
    if job.get('archive_type', 'tar') != 'tar':
        raise BackupError('This is a native ZFS backup; use zfs-restore --dataset POOL/DATASET')
    if parent is None:
        if job['level'] != 'full' or job['parent'] is not None:
            raise BackupError('Restore chain must start with a full backup')
    elif (job['level'] != 'incremental' or job['parent'] != parent['id'] or
          job['source'] != parent['source'] or job.get('ssh') != parent.get('ssh')):
        raise BackupError('Missing parent or out-of-order incremental backup; '
                          f"destination is at {parent['id']}, requested backup expects {job.get('parent')}")


def extract_restore_stream(reader, tree, progress, quiet):
    process = None
    try:
        process = logged_process(['tar', '--extract', '--listed-incremental=/dev/null',
                                    '--acls', '--xattrs', '--numeric-owner', '--file=-',
                                    f'--directory={tree}', *([] if quiet else ['--verbose'])],
                                 stdin=subprocess.PIPE, log_stdout=True, env=external_env())
        for kind, payload in reader.frames():
            if kind == 'data':
                process.stdin.write(payload)
                progress.read_bytes += len(payload)
                progress.written_bytes += len(payload)
        process.stdin.close()
        if process.wait():
            raise BackupError('GNU tar extraction failed')
    finally:
        stop_process(process)


def restore(backup_ids, destination, media, *, quiet=False, base=None):
    require_tar()
    if not backup_ids:
        raise BackupError("Specify the full backup ID followed by all incremental IDs")
    for backup_id in backup_ids:
        valid_id(backup_id)
    if len(set(backup_ids)) != len(backup_ids):
        raise BackupError("Duplicate backup ID in restore chain")
    if base:
        valid_id(base)
    requested = Path(destination).absolute()
    destination = requested.parent.resolve() / requested.name
    if isinstance(media, FileMedia) and (inside(media.directory, destination) or
                                        inside(destination, media.directory)):
        raise BackupError("Restore destination and media must be separate")
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_id = hashlib.sha256(os.fsencode(destination)).hexdigest()
    lock_path = Path.home() / '.cache' / 'tape-backup' / 'restores' / lock_id
    tree = None
    try:
        with locked(lock_path), media.lock(), Progress("Restore") as progress:
            parent = read_restore_marker(destination)
            if parent is not None and base and parent['id'] != base:
                raise BackupError('--base differs from the last completed restore recorded for this directory')
            if parent is None and base:
                # Adopt a restore made by older executables, which did not
                # leave history. The explicit ID asserts the tree's baseline;
                # tape metadata still validates its source and the next parent.
                directory_identity(destination)
                parent = inspect_backup(media, base)
                log(f'Using explicitly supplied restored base {base} for {destination}')
            inplace = parent is not None
            if not inplace:
                try:
                    empty_destination(destination)
                except BackupError as exc:
                    raise BackupError(f'{exc}. To continue an older restore, use --base ID '
                                      'with the last backup already restored here') from exc
                tree = Path(tempfile.mkdtemp(prefix=f'.{destination.name}.restoring-', dir=destination.parent))
                log(f'Extracting into {tree}; destination is published only after verification')
            else:
                log(f'Continuing restore at {parent["id"]} in {destination}; applying incrementals in place')
                identity = directory_identity(destination)

            # Check every requested incremental before changing an existing
            # restore. Payloads are verified from tape, never staged on disk.
            verified = {}
            if inplace:
                previous = parent
                for backup_id in backup_ids:
                    reader = StreamReader(media, backup_id, progress)
                    try:
                        reader.next_volume()
                        check_restore_parent(reader.job, previous)
                        progress.phase = f'verifying {backup_id} before apply'
                        progress.estimate(reader.job.get('estimated_bytes'))
                        for kind, payload in reader.frames():
                            if kind == 'data':
                                progress.read_bytes += len(payload)
                        verified[backup_id] = reader.summary
                        previous = reader.job
                    finally:
                        reader.close()
            for backup_id in backup_ids:
                reader = StreamReader(media, backup_id, progress)
                try:
                    reader.next_volume()
                    job = reader.job
                    check_restore_parent(job, parent)
                    if inplace:
                        if any(verified[backup_id].get(key) != value for key, value in job.items()):
                            raise BackupError('Incremental header changed after verification; refusing to apply')
                        if directory_identity(destination) != identity:
                            raise BackupError('Restore destination changed during verification')
                        # Persist uncertainty before tar can alter or delete any
                        # file; a crash must never permit the next incremental.
                        write_restore_marker(destination, parent, pending=backup_id)
                    log(f"Restoring {backup_id} ({job['level']}) directly from tape")
                    progress.phase = f"extracting {backup_id}"
                    progress.estimate(job.get("estimated_bytes"))
                    extract_restore_stream(reader, destination if inplace else tree, progress, quiet)
                    if inplace:
                        if reader.summary != verified[backup_id]:
                            raise BackupError('Incremental changed between verification and apply')
                        if directory_identity(destination) != identity:
                            raise BackupError('Restore destination changed during apply')
                        progress.phase = 'flushing updated files'
                        os.sync()
                        write_restore_marker(destination, job)
                    parent = job
                except BaseException:
                    if inplace:
                        log('Incremental restore stopped. If application began, the directory is marked '
                            'incomplete; restore the full chain into a separate directory to recover.')
                    raise
                finally:
                    reader.close()
            if not inplace:
                progress.phase = "flushing restored files"
                os.sync()
                empty_destination(destination)
                os.replace(tree, destination)
                tree = None
                fsync_dir(destination.parent)
                try:
                    write_restore_marker(destination, parent)
                except OSError as exc:
                    raise BackupError(f'Files restored successfully, but restore history could not be saved: {exc}. '
                                      f'Use --base {parent["id"]} when continuing this restore') from exc
            progress.phase = "complete"
        return destination
    finally:
        if tree is not None and tree.exists():
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
    parser = argparse.ArgumentParser(description=__doc__, usage='%(prog)s COMMAND [OPTIONS]',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''File backup and recovery:
  backup --source /opt --level full             Back up files to a blank tape
  backup --source /opt --level incremental --base ID
                                                Append changes to the last backup
  inspect                                      List backups from tape metadata
  list --backup ID                             List archived file names
  restore --backup FULL_ID DELTA_ID --destination /srv/recovered

Native ZFS snapshots (existing snapshots; no file exclusions):
  zfs-backup --snapshot tank/books@daily
  zfs-backup --snapshot tank/books@next --base ID
  zfs-restore --backup FULL_ID DELTA_ID --dataset tank/recovered

Check the drive: status, doctor, compression status.
Manage the tape: compression on|off, wipe (destructive), eject.
Start full backups on blank tapes; incrementals always append.
Run tape-backup COMMAND --help for options and tape-selection details.
Default device: /dev/nst0. Tapes stay loaded until you run eject.''')
    parser.add_argument("--version", action="version", version=f"%(prog)s {PROGRAM_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True, title='Commands', metavar='COMMAND')

    def media_options(command):
        group = command.add_mutually_exclusive_group()
        group.add_argument("--device", default="/dev/nst0", help="Tape device (default: /dev/nst0)")
        group.add_argument("--media-dir", type=Path, help="Use files as simulated tape volumes")
        command.add_argument("--media-command", help="Loader executable; arguments: write|read|append|blank ID NUMBER DEVICE")

    def ssh_options(command):
        command.add_argument('--ssh', metavar='USER@HOST', help='Read the source over SSH')
        command.add_argument('--ssh-port', type=int)
        command.add_argument('--ssh-identity', type=Path)
        command.add_argument('--ssh-config', type=Path)
        command.add_argument('--remote-program', help='Remote tape-backup executable path')

    create = commands.add_parser("backup", help="Stream source files directly to tape")
    create.add_argument("--source", required=True, type=Path)
    create.add_argument("--ssh", metavar="USER@HOST", help="Read source on a remote Linux machine over SSH")
    create.add_argument("--ssh-port", type=int, help="SSH port (otherwise use SSH config)")
    create.add_argument("--ssh-identity", type=Path, help="SSH private key (otherwise use SSH config/agent)")
    create.add_argument("--ssh-config", type=Path, help="Use an alternative OpenSSH configuration file")
    create.add_argument("--remote-program", help="Remote executable path (default: tape-backup in PATH)")
    create.add_argument("--level", choices=("full", "incremental"), default="full")
    create.add_argument('--exclude', action='append', metavar='PATTERN',
                        help='Exclude a source-relative file, directory subtree, or wildcard pattern; repeatable')
    create.add_argument('--exclude-from', action='append', type=Path, metavar='FILE',
                        help='Read one exclusion pattern per line from a local file; repeatable')
    create.add_argument("--base", help="Previous backup ID, required for incremental backups; load its tapes first")
    create.add_argument('--append', action='store_true', help='Optional compatibility flag; incrementals always append')
    create.add_argument("--buffer-size", type=parse_size, default=DEFAULT_BUFFER,
                        help="RAM read-ahead and recovery budgets, each 64KiB to 10GiB (default: 1GiB)")
    create.add_argument("--volume-size", type=parse_size, help=argparse.SUPPRESS)
    create.add_argument("--quiet", action="store_true", help="Suppress file names; keep progress and transfer rates")
    create.add_argument('--json', action='store_true', help='Print a structured completion summary instead of only the backup ID')
    create.add_argument('--verify', action='store_true', help='Read back and verify all volumes after backup')
    media_options(create)
    extract = commands.add_parser("restore", help="Stream tapes directly into a restored directory")
    extract.add_argument("--backup", nargs="+", required=True, help="Full chain for a new restore, or next incremental ID(s) for an existing restore")
    extract.add_argument('--base', help='Last backup already restored here; only needed to adopt an older restore without history')
    extract.add_argument("--destination", required=True, type=Path)
    extract.add_argument("--quiet", action="store_true", help="Suppress file names; keep progress and transfer rates")
    media_options(extract)
    zcreate = commands.add_parser('zfs-backup', help='Stream an existing ZFS snapshot; --base appends an incremental')
    zcreate.add_argument('--snapshot', required=True, metavar='POOL/DATASET@SNAPSHOT')
    zcreate.add_argument('--base', help='Previous ZFS backup ID; its source snapshot must still exist')
    zcreate.add_argument('--raw', action='store_true', default=None,
                         help='Preserve native ZFS encryption; inherited from an incremental parent')
    zcreate.add_argument('--buffer-size', type=parse_size, default=DEFAULT_BUFFER,
                         help='RAM budget per source/recovery buffer (default: 1GiB, maximum: 10GiB)')
    zcreate.add_argument('--volume-size', type=parse_size, help=argparse.SUPPRESS)
    zcreate.add_argument('--quiet', action='store_true')
    zcreate.add_argument('--json', action='store_true', help='Print a structured completion summary')
    zcreate.add_argument('--verify', action='store_true', help='Read back and verify all volumes after backup')
    ssh_options(zcreate)
    media_options(zcreate)
    zextract = commands.add_parser('zfs-restore', help='Verify and receive native ZFS backups into an unmounted dataset')
    zextract.add_argument('--backup', nargs='+', required=True, help='Full chain, or next incremental IDs')
    zextract.add_argument('--dataset', required=True, metavar='POOL/DATASET', help='New dataset for a full restore')
    media_options(zextract)
    listing = commands.add_parser('list', help='List file names in one backup without extracting (reads all volumes)')
    listing.add_argument('--backup', help='Backup ID; omit to discover the first backup on the loaded tape')
    media_options(listing)
    for name in ("inspect", "verify"):
        command = commands.add_parser(name, help="List backups on the loaded cartridge" if name == "inspect" else "Verify all tape data")
        if name == 'inspect':
            selection = command.add_mutually_exclusive_group()
            selection.add_argument('--backup', help='Read only the selected backup header')
            selection.add_argument('--first', action='store_true', help='Read only the first backup header')
            selection.add_argument('--all', action='store_true', help='List all backup segments (the default)')
            command.add_argument('--scan', action='store_true',
                                 help='Allow a full cartridge scan when no catalog is usable (can take hours)')
        else:
            command.add_argument("--backup", help="Backup ID; can be discovered from the first tape")
        media_options(command)
    eject = commands.add_parser("eject", help="Rewind and eject the loaded tape")
    eject.add_argument("--device", default="/dev/nst0", help="Tape device (default: /dev/nst0)")
    wipe = commands.add_parser('wipe', help='Erase and initialize the loaded tape for reuse')
    wipe.add_argument('--device', default='/dev/nst0', help='Tape device (default: /dev/nst0)')
    wipe.add_argument('--long', dest='long_erase', action='store_true',
                      help='Request a long erase, which can take hours (default: short initialization)')
    wipe.add_argument('--yes', action='store_true', help='Confirm destroying the loaded tape contents without prompting')
    compression = commands.add_parser('compression', help='Show or change drive hardware compression')
    compression.add_argument('action', nargs='?', choices=('status', 'on', 'off'), default='status')
    compression.add_argument('--device', default='/dev/nst0', help='Tape device (default: /dev/nst0)')
    for name in ('status', 'doctor'):
        status = commands.add_parser(name, help='Show drive state and diagnostics without moving or changing the tape')
        status.add_argument('--device', default='/dev/nst0', help='Tape device (default: /dev/nst0)')
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    os.umask(0o077)
    args = None
    try:
        if argv == ["_ssh-source"]:
            serve_ssh_source()
            return 0
        parser = make_parser()
        if not argv:
            parser.print_help()
            return 0
        if argv[0] == 'help':
            argv = [*argv[1:], '--help']
        args = parser.parse_args(argv)
        if args.command in ('status', 'doctor'):
            result = device_status(TapeMedia(args.device))
            print(json.dumps(result, indent=2))
            return 0 if result['busy'] or result['state'] is not None else 1
        if args.command == "eject":
            media = TapeMedia(args.device)
            with media.lock():
                media.eject()
            print(f"Ejected tape from {media.device}")
            return 0
        if args.command == 'wipe':
            media = TapeMedia(args.device)
            with media.lock():
                media.wipe(long_erase=args.long_erase, yes=args.yes)
            print(f'Initialized tape in {media.device}; blank verified, rewound, and left loaded')
            return 0
        if args.command == 'compression':
            media = TapeMedia(args.device)
            with media.lock():
                status = media.compression(args.action)
            state = ('ON' if status['enabled'] else 'OFF') if status['supported'] else 'UNSUPPORTED'
            print(f'Hardware compression on {media.device}: {state}')
            return 0
        ssh = None
        excludes = None
        if args.command in ('backup', 'zfs-backup'):
            if args.command == 'backup':
                excludes = exclusion_options(args.exclude, args.exclude_from)
            if args.ssh:
                ssh = SSHConfig(args.ssh, port=args.ssh_port, identity=args.ssh_identity,
                                config=args.ssh_config, program=args.remote_program or "tape-backup")
            elif any(value is not None for value in
                     (args.ssh_port, args.ssh_identity, args.ssh_config, args.remote_program)):
                raise BackupError("SSH options require --ssh USER@HOST")
        if args.media_dir and args.media_command:
            raise BackupError("--media-command only applies to physical tapes")
        media = FileMedia(args.media_dir) if args.media_dir else TapeMedia(args.device, args.media_command)
        if args.command == "backup":
            size = args.volume_size or (1024**3 if args.media_dir else None)
            result = backup(args.source, media, level=args.level, base=args.base,
                            volume_size=size, buffer_size=args.buffer_size, quiet=args.quiet, ssh=ssh,
                            append=args.append, excludes=excludes)
        elif args.command == 'zfs-backup':
            result = backup_zfs(args.snapshot, media, base=args.base, raw=args.raw,
                                volume_size=args.volume_size or (1024**3 if args.media_dir else None),
                                buffer_size=args.buffer_size, quiet=args.quiet, ssh=ssh)
        elif args.command == 'zfs-restore':
            result = restore_zfs(args.backup, args.dataset, media)
        elif args.command == "restore":
            result = restore(args.backup, args.destination, media, quiet=args.quiet, base=args.base)
        else:
            if args.backup:
                valid_id(args.backup)
            with media.lock():
                if args.command == 'inspect':
                    with Progress('Inspect', transfer=False) as progress:
                        progress.phase = 'reading backup header' if args.backup or args.first else 'locating cartridge metadata'
                        result = inspect_backup(media, args.backup) if args.backup or args.first else inspect_all(
                            media, allow_scan=args.scan or isinstance(media, FileMedia), progress=progress)
                        progress.phase = 'complete' if result.get('scan_complete', True) else 'incomplete'
                    if not result.get('scan_complete', True):
                        print(json.dumps(result, indent=2))
                        return 1
                elif args.command == 'list':
                    list_files(media, args.backup)
                    return 0
                else:
                    result = scan(media, args.backup)
            result = json.dumps(result, indent=2)
        if args.command in ('backup', 'zfs-backup'):
            if args.verify:
                try:
                    with media.lock():
                        scan(media, result)
                except (BackupError, OSError) as exc:
                    raise BackupError(f'Backup {result} was committed, but read-back verification failed: {exc}') from exc
                media.last_result['data_verified'] = True
            if args.json:
                result = json.dumps(media.last_result, indent=2)
        print(result)
        return 0
    except (BackupError, OSError) as exc:
        log(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        if args is not None and args.command == 'wipe':
            log('Wipe interrupted; the erase may still be running in the drive. '
                'Tape initialization has not been verified.')
        elif args is not None and args.command == 'compression':
            log('Compression command interrupted; run compression status to check the drive setting.')
        elif args is not None and args.command in ('restore', 'zfs-restore'):
            log('Restore interrupted. If an incremental apply began, restore the full chain '
                'into a separate directory; an incomplete apply cannot be continued.')
        elif args is not None and args.command == 'list':
            log('File listing interrupted; output may be incomplete.')
        elif args is not None and args.command in ('inspect', 'verify'):
            log(f'{args.command.capitalize()} interrupted; the operation did not complete.')
        else:
            log("Interrupted. Preserve existing tapes; an incomplete tail cannot be appended to. "
                "Start a new full backup on separate media, or repeat restore from the first tape.")
        return 130


if __name__ == "__main__":
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    sys.exit(main())
