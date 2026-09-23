"""Streaming format tests with real GNU tar and injected tape failures."""
from contextlib import redirect_stderr
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import tape_backup as tb


def tree_contents(root):
    result = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            result[name] = ("link", os.readlink(path))
        elif path.is_dir():
            result[name] = ("dir", stat.S_IMODE(path.stat().st_mode))
        else:
            result[name] = ("file", path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return result


class FaultMedia(tb.FileMedia):
    def __init__(self, directory, *, capacity=None, short=False, fail_write=False,
                 fail_commit=False, lost_records=0):
        super().__init__(directory)
        self.capacity, self.short = capacity, short
        self.fail_write, self.fail_commit = fail_write, fail_commit
        self.lost_records = lost_records
        self.loads = []

    def load(self, backup_id, number, writing):
        super().load(backup_id, number, writing)
        self.number = number
        self.loads.append((backup_id, number, writing))

    def open(self, writing):
        volume = super().open(writing)
        if not writing:
            return volume
        owner = self
        class FaultVolume(tb.Volume):
            commits = 0
            def write(self, record):
                if owner.fail_write and owner.number == 1 and self.stream.tell() >= 3 * tb.BLOCK_SIZE:
                    owner.fail_write = False
                    raise OSError(errno.EIO, 'Injected tape failure')
                if owner.capacity is not None and self.stream.tell() + len(record) > owner.capacity:
                    left = max(0, owner.capacity - self.stream.tell())
                    if left:
                        self.stream.write(record[:left])
                    raise OSError(errno.ENOSPC, 'Short write' if owner.short else 'End of tape')
                super().write(record)
            def commit(self):
                self.commits += 1
                if owner.fail_commit and owner.number == 1 and self.commits == 2:
                    if owner.lost_records:
                        self.stream.truncate(self.stream.tell() - owner.lost_records * tb.BLOCK_SIZE)
                    raise OSError(errno.ENOSPC, 'Injected delayed filemark error')
                super().commit()
        return FaultVolume(volume.stream)


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.media = FaultMedia(self.root / 'media')
        self.destination = self.root / 'restored'
        self.quiet = redirect_stderr(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        sync = patch.object(tb.os, 'sync')
        sync.start()
        self.addCleanup(sync.stop)

    def seed(self):
        (self.source / 'unchanged').write_bytes(os.urandom(400_000))
        (self.source / 'changed').write_text('before')
        (self.source / 'deleted').write_text('delete')
        (self.source / 'old-dir').mkdir()
        (self.source / 'old-dir' / 'child').write_text('child')
        (self.source / 'symlink').symlink_to('unchanged')
        (self.source / 'executable').write_text('#!/bin/sh\nexit 0\n')
        (self.source / 'executable').chmod(0o751)
        os.link(self.source / 'executable', self.source / 'hardlink')
        (self.source / 'space and\nnewline').write_text('odd name')
        (self.source / 'empty').mkdir()

    def create(self, base=None, media=None, limit=8*tb.BLOCK_SIZE, buffer=tb.BLOCK_SIZE):
        return tb.backup(self.source, media or self.media,
                         level='incremental' if base else 'full', base=base,
                         volume_size=limit, buffer_size=buffer, quiet=True)

    def extract(self, ids, media=None):
        return tb.restore(ids, self.destination, media or self.media, quiet=True)

    def archive_bytes(self, backup_id):
        reader = tb.StreamReader(self.media, backup_id)
        try:
            return b''.join(data for kind, data in reader.frames() if kind == 'data')
        finally:
            reader.close()

    def test_full_backup_streams_and_restores_without_state_or_archive(self):
        self.seed()
        actual_frames = tb.archive_frames
        counts = []
        def observe(*args):
            for index, (kind, chunk) in enumerate(actual_frames(*args)):
                if kind == "data":
                    counts.append(len(chunk))
                    if index >= 2:  # The reader may fill one chunk ahead of the writer.
                        self.assertGreater(sum(p.stat().st_size for p in self.media.directory.glob('*.tape')),
                                           tb.BLOCK_SIZE)
                yield kind, chunk
        with patch.object(tb, 'archive_frames', side_effect=observe):
            backup_id = self.create()
        self.assertGreater(len(counts), 2)
        self.assertLessEqual(max(counts), tb.BLOCK_SIZE)
        self.assertEqual({p.name for p in self.root.iterdir()}, {'source', 'media'})
        self.assertFalse(list(self.root.rglob('*.tar')))
        self.assertFalse(list(self.root.rglob('*.snar')))
        self.assertFalse(list(self.root.rglob('*.json')))
        self.extract([backup_id])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))
        self.assertEqual((self.destination/'executable').stat().st_ino,
                         (self.destination/'hardlink').stat().st_ino)

    def test_background_reader_fills_next_chunk_during_write_but_cannot_read_a_third(self):
        self.seed()
        second_ready, third_started = threading.Event(), threading.Event()
        actual_frames, actual_send = tb.archive_frames, tb.StreamWriter.send
        checked = False

        def observe(*args):
            for index, frame in enumerate(actual_frames(*args)):
                if index == 1:
                    second_ready.set()
                elif index == 2:
                    third_started.set()
                yield frame

        def send(writer, kind, chunk):
            nonlocal checked
            if kind == 'data' and not checked:
                checked = True
                self.assertTrue(second_ready.wait(3), 'Source did not read ahead during the write')
                self.assertFalse(third_started.wait(0.1), 'More than two chunks were read')
            return actual_send(writer, kind, chunk)

        with patch.object(tb, 'archive_frames', side_effect=observe), \
                patch.object(tb.StreamWriter, 'send', send):
            full = self.create()
        self.assertTrue(checked)
        self.extract([full])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_background_reader_failure_does_not_publish_completion(self):
        self.seed()
        actual_frames = tb.archive_frames

        def fail(*args):
            frames = actual_frames(*args)
            try:
                yield next(frames)
                raise tb.BackupError('Injected background source failure')
            finally:
                frames.close()

        with patch.object(tb, 'archive_frames', side_effect=fail), \
                self.assertRaisesRegex(tb.BackupError, 'background source failure'):
            self.create()
        backup_id = self.media.loads[0][0]
        with self.assertRaises(tb.BackupError):
            tb.scan(self.media, backup_id)

    def test_writer_failure_stops_local_archive_and_background_reader(self):
        self.seed()
        processes = []
        actual_start = tb.start_archive

        def start(*args):
            process = actual_start(*args)
            processes.append(process)
            return process

        with patch.object(tb, 'start_archive', side_effect=start), \
                patch.object(tb.StreamWriter, 'send', side_effect=tb.BackupError('Writer failed')), \
                self.assertRaisesRegex(tb.BackupError, 'Writer failed'):
            self.create()
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)
        self.assertFalse(any(t.name == 'tape-backup-reader' for t in threading.enumerate()))

    def test_two_incrementals_use_snapshot_on_tape_and_restore_changes(self):
        self.seed()
        full = self.create()
        (self.source/'changed').write_text('first')
        (self.source/'deleted').unlink()
        (self.source/'added').write_text('new')
        (self.source/'old-dir').rename(self.source/'renamed')
        first = self.create(full)
        with tarfile.open(fileobj=io.BytesIO(self.archive_bytes(first))) as archive:
            names = archive.getnames()
        self.assertIn('./changed', names)
        self.assertNotIn('./unchanged', names)
        shutil.rmtree(self.source/'renamed')
        (self.source/'changed').write_text('second')
        (self.source/'added').unlink()
        second = self.create(first)
        self.extract([full, first, second])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_empty_source_and_unchanged_delta(self):
        full = self.create()
        delta = self.create(full)
        self.extract([full, delta])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_metadata_sparse_files_and_xattrs(self):
        path = self.source/'sparse'
        with path.open('wb') as out:
            out.write(b'begin')
            out.seek(5*1024**2)
            out.write(b'end')
        os.setxattr(path, 'user.tape-test', b'value')
        full = self.create()
        self.extract([full])
        restored = self.destination/'sparse'
        self.assertEqual(path.read_bytes(), restored.read_bytes())
        self.assertEqual(path.stat().st_mtime_ns, restored.stat().st_mtime_ns)
        self.assertEqual(os.getxattr(restored, 'user.tape-test'), b'value')
        self.assertLess(restored.stat().st_blocks*512, restored.stat().st_size)

    def test_end_of_tape_and_partial_records_replay_from_ram(self):
        self.seed()
        for short in (False, True):
            with self.subTest(short=short):
                media = FaultMedia(self.root/f'media-{short}', capacity=6*tb.BLOCK_SIZE+100, short=short)
                full = self.create(media=media, limit=None, buffer=2*tb.BLOCK_SIZE)
                summary = tb.scan(media, full)
                self.assertGreater(summary['volumes'], 1)
                dest = self.root/f'restored-{short}'
                tb.restore([full], dest, media, quiet=True)
                self.assertEqual(tree_contents(self.source), tree_contents(dest))

    def test_delayed_commit_error_replays_whole_or_lost_buffered_chunk(self):
        self.seed()
        for lost in (0, 1, 2):
            with self.subTest(lost=lost):
                media = FaultMedia(self.root/f'media-{lost}', fail_commit=True, lost_records=lost)
                full = self.create(media=media, limit=None, buffer=2*tb.BLOCK_SIZE)
                dest = self.root/f'restored-{lost}'
                tb.restore([full], dest, media, quiet=True)
                self.assertEqual(tree_contents(self.source), tree_contents(dest))

    def test_io_error_continues_on_replacement_volume(self):
        self.seed()
        media = FaultMedia(self.media.directory, fail_write=True)
        full = self.create(media=media, limit=None)
        self.assertGreater(tb.scan(media, full)['volumes'], 1)
        self.extract([full], media)
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_no_progress_stops_after_three_tapes(self):
        self.seed()
        media = FaultMedia(self.media.directory, capacity=2*tb.BLOCK_SIZE)
        with self.assertRaisesRegex(tb.BackupError, 'Three volumes'):
            self.create(media=media, limit=None)
        self.assertEqual(len(list(self.media.directory.glob('*.tape'))), 3)

    def test_corrupt_payload_is_rejected_without_publishing_restore(self):
        self.seed()
        full = self.create()
        volume = self.media.directory/f'{full}.0001.tape'
        with volume.open('r+b') as stream:
            stream.seek(2*tb.BLOCK_SIZE+100)
            byte = stream.read(1)
            stream.seek(-1, 1)
            stream.write(bytes([byte[0] ^ 0xff]))
        with self.assertRaisesRegex(tb.BackupError, 'Checksum'):
            self.extract([full])
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob('.restored.restoring-*')))

    def test_wrong_volume_is_rejected(self):
        self.seed()
        full = self.create()
        first = self.media.directory/f'{full}.0001.tape'
        second = self.media.directory/f'{full}.0002.tape'
        second.write_bytes(first.read_bytes())
        with self.assertRaisesRegex(tb.BackupError, 'Wrong'):
            self.extract([full])
        self.assertFalse(self.destination.exists())

    def test_missing_final_marker_is_an_incomplete_backup(self):
        self.seed()
        full = self.create()
        last = sorted(self.media.directory.glob(f'{full}.*.tape'))[-1]
        with last.open('r+b') as stream:
            stream.truncate(last.stat().st_size-2*tb.BLOCK_SIZE)
        with self.assertRaisesRegex(tb.BackupError, 'Incomplete'):
            self.extract([full])
        self.assertFalse(self.destination.exists())

    def test_parent_and_order_validation(self):
        self.seed()
        full = self.create()
        first = self.create(full)
        second = self.create(first)
        for chain in ([first], [full, second], [full, first, first]):
            with self.subTest(chain=chain), self.assertRaises(tb.BackupError):
                self.extract(chain)
        self.assertFalse(self.destination.exists())

    def test_incomplete_parent_cannot_start_incremental(self):
        self.seed()
        full = self.create()
        last = sorted(self.media.directory.glob(f'{full}.*.tape'))[-1]
        last.unlink()
        before = set(self.media.directory.iterdir())
        with self.assertRaisesRegex(tb.BackupError, 'Incomplete'):
            self.create(full)
        self.assertEqual(set(self.media.directory.iterdir()), before)

    def test_restore_after_source_and_everything_except_tapes_are_removed(self):
        self.seed()
        full = self.create()
        expected = tree_contents(self.source)
        shutil.rmtree(self.source)
        self.extract([full])
        self.assertEqual(tree_contents(self.destination), expected)

    def test_inspect_discovers_id_and_skips_payload_but_verify_checks_it(self):
        self.seed()
        full = self.create()
        with patch.object(tb.Volume, 'skip_payload', autospec=True,
                          side_effect=tb.Volume.skip_payload) as skip:
            info = tb.scan(self.media, verify=False)
            self.assertGreater(skip.call_count, 1)
        self.assertEqual(info['id'], full)
        self.assertFalse(info['data_verified'])
        self.assertTrue(tb.scan(self.media, full)['data_verified'])

    def test_bad_source_and_small_buffer_or_volume_rejected(self):
        with self.assertRaises(tb.BackupError):
            tb.backup(self.source, self.media, level='incremental', quiet=True)
        for opts in ({'buffer_size': 1}, {'volume_size': 1024}, {'buffer_size': tb.MAX_BUFFER+1}):
            with self.subTest(opts=opts), self.assertRaises(tb.BackupError):
                tb.backup(self.source, self.media, quiet=True, **opts)
        with self.assertRaisesRegex(tb.BackupError, 'separate'):
            tb.backup(self.source, tb.FileMedia(self.source/'media'), quiet=True)

    def test_large_archive_uses_small_frames_and_supports_incrementals(self):
        source = self.source / 'large'
        block = b'x' * 1024**2
        expected = hashlib.sha256()
        with source.open('wb') as stream:
            for _ in range(257):
                stream.write(block)
                expected.update(block)
        full = tb.backup(self.source, self.media, quiet=True)
        summary = tb.scan(self.media, full)
        self.assertGreater(summary['data_bytes'], 256 * 1024**2)
        self.assertGreater(summary['chunks'], 64)
        with (self.media.directory / f'{full}.0001.tape').open('rb') as stream:
            self.assertEqual(tb.decoded_header(stream.read(tb.BLOCK_SIZE))['format'], 3)
            self.assertEqual(tb.decoded_header(stream.read(tb.BLOCK_SIZE))['length'], tb.FRAME_SIZE)
        self.assertTrue(summary['data_verified'])
        (self.source / 'added').write_text('incremental data')
        delta = self.create(base=full, limit=None)
        self.extract([full, delta])
        with (self.destination / 'large').open('rb') as stream:
            self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), expected.hexdigest())
        self.assertEqual((self.destination / 'added').read_text(), 'incremental data')

    def test_buffer_limit_and_default_from_cli(self):
        parser = tb.make_parser()
        default = parser.parse_args(['backup', '--source', str(self.source)])
        self.assertEqual(default.buffer_size, 1024**3)
        maximum = parser.parse_args(['backup', '--source', str(self.source), '--buffer-size', '10GiB'])
        self.assertEqual(maximum.buffer_size, 10 * 1024**3)
        (self.source / 'small').write_text('Small archives do not allocate the full buffer.')
        full = self.create(buffer=maximum.buffer_size, limit=None)
        self.extract([full])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))
        with self.assertRaisesRegex(tb.BackupError, '64KiB and 10GiB'):
            self.create(buffer=10 * 1024**3 + 1, limit=None)

    def test_existing_destination_is_not_overwritten(self):
        full = self.create()
        self.destination.mkdir()
        (self.destination/'keep').write_text('important')
        with self.assertRaisesRegex(tb.BackupError, 'empty'):
            self.extract([full])
        self.assertEqual((self.destination/'keep').read_text(), 'important')

    def test_actual_short_write_is_recoverable(self):
        stream = Mock()
        stream.write.return_value = 10
        with self.assertRaises(OSError) as error:
            tb.Volume(stream).write(b'x'*tb.BLOCK_SIZE)
        self.assertEqual(error.exception.errno, errno.ENOSPC)

    def test_tape_commit_uses_synchronous_filemark_and_skip_uses_fsf(self):
        stream = Mock()
        stream.fileno.return_value = 42
        volume = tb.Volume(stream, physical=True)
        with patch.object(tb.fcntl, 'ioctl') as ioctl:
            volume.commit()
            ioctl.assert_called_with(42, tb.MTIOCTOP, struct.pack('@hi', tb.MTWEOF, 1))
            volume.skip_payload(128000)
            ioctl.assert_called_with(42, tb.MTIOCTOP, struct.pack('@hi', tb.MTFSF, 1))

    def test_filemarks_are_crossed_only_between_frames(self):
        stream = Mock()
        stream.read.side_effect = [b'', b'x'*tb.BLOCK_SIZE, b'']
        volume = tb.Volume(stream, physical=True)
        self.assertEqual(volume.read(boundary=True), b'x'*tb.BLOCK_SIZE)
        with self.assertRaises(tb.EndVolume):
            volume.read()

    def test_immediate_tape_filemarks_are_disabled_before_rewind(self):
        media = object.__new__(tb.TapeMedia)
        media.device = '/dev/nst1'
        with patch.object(Path, 'read_text', return_value='0x8000'), \
                patch.object(tb, 'run_command') as mt:
            media.mt('rewind')
            self.assertEqual(mt.call_args_list[0].args, (['mt', '-f', '/dev/nst1', 'stclearoptions', '0xa002'],))
            self.assertEqual(mt.call_args_list[1].args, (['mt', '-f', '/dev/nst1', 'rewind'],))

    def test_eta_uses_payload_completion_and_reports_measured_rate(self):
        progress = tb.Progress('Test')
        progress.started = progress.last_time = progress.eta_started = 0
        progress.total_bytes = 200 * 1024**2
        progress.written_bytes = 100 * 1024**2
        progress.transferred = 120 * 1024**2
        output = io.StringIO()
        with patch.object(tb.time, 'monotonic', return_value=60), redirect_stderr(output):
            progress.report()
        self.assertIn('2.0 MiB/s I/O', output.getvalue())
        self.assertIn('ETA ~00:01:00', output.getvalue())

    def test_eta_inventory_reads_metadata_not_file_contents(self):
        self.seed()
        with patch('builtins.open', side_effect=AssertionError('No payload reading during estimate')):
            estimate = tb.estimate_source(self.source)
        self.assertGreater(estimate, 400000)

    def test_parser_does_not_require_state_and_preserves_device_selection(self):
        parser = tb.make_parser()
        args = parser.parse_args(['backup', '--source', '/data', '--device', '/dev/nst1'])
        self.assertEqual(args.device, '/dev/nst1')
        self.assertFalse(hasattr(args, 'state'))
        with self.assertRaises(SystemExit):
            parser.parse_args(['backup', '--source', '/data', '--state', '/state'])

    def test_concurrent_media_operation_is_rejected(self):
        with self.media.lock(), self.assertRaisesRegex(tb.BackupError, 'Another operation'):
            self.create()
        self.assertFalse(list(self.media.directory.glob('*.tape')))

    def test_tape_device_validation_and_selected_drive_loading(self):
        for mode, device in ((stat.S_IFREG, 0), (stat.S_IFCHR, os.makedev(9, 1)),
                             (stat.S_IFCHR, os.makedev(1, 128))):
            info = SimpleNamespace(st_mode=mode, st_rdev=device)
            with patch.object(tb.os, 'stat', return_value=info), self.assertRaises(tb.BackupError):
                tb.TapeMedia('/dev/nst1')
        info = SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=os.makedev(9, 129))
        with patch.object(tb.os, 'stat', return_value=info), \
                patch.object(tb.shutil, 'which', return_value='/usr/bin/mt'):
            media = tb.TapeMedia('/dev/nst1', '/loader')
        with patch.object(tb, 'run_command') as command, patch.object(Path, 'read_text', return_value='0x0'):
            media.load('a'*32, 2, True)
            media.release()
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            ['/loader', 'write', 'a'*32, '2', '/dev/nst1'],
            ['mt', '-f', '/dev/nst1', 'rewind'], ['mt', '-f', '/dev/nst1', 'setblk', '0'],
            ['mt', '-f', '/dev/nst1', 'offline']])

    def test_manual_tape_changes_use_a_nonseekable_terminal(self):
        for writing, response in ((True, b'\n'), (False, b'\n'),
                                  (True, b'q\n'), (True, b'\x04')):
            with self.subTest(writing=writing, response=response):
                master, slave = pty.openpty()
                try:
                    terminal = os.ttyname(slave)
                    media = object.__new__(tb.TapeMedia)
                    media.device = '/dev/nst1'
                    media.media_command = None
                    media.mt = Mock()

                    def terminal_open(path, *args, **kwargs):
                        self.assertEqual(path, '/dev/tty')
                        return open(terminal, *args, **kwargs,
                                    opener=lambda path, flags: os.open(path, flags | os.O_NOCTTY))

                    os.write(master, response)
                    with patch.object(tb, 'open', terminal_open, create=True):
                        if response == b'\n':
                            media.load('a'*32, 2, writing)
                        else:
                            with self.assertRaisesRegex(tb.BackupError, 'cancelled'):
                                media.load('a'*32, 2, writing)
                    self.assertTrue(select.select([master], [], [], 2)[0], 'Missing tape prompt')
                    prompt = os.read(master, 4096).decode()
                    action = 'write' if writing else 'read'
                    self.assertIn(f"Load {'a'*32} volume 2 into /dev/nst1 for {action}", prompt)
                    self.assertIn('Press Enter when ready, or type q to stop:', prompt)
                    self.assertEqual('CONTENTS WILL BE OVERWRITTEN' in prompt, writing)
                    if response == b'\n':
                        self.assertEqual([call.args for call in media.mt.call_args_list],
                                         [('rewind',), ('setblk', '0')])
                    else:
                        media.mt.assert_not_called()
                finally:
                    os.close(master)
                    os.close(slave)

    def test_missing_terminal_reports_cause_without_touching_tape(self):
        media = object.__new__(tb.TapeMedia)
        media.device = '/dev/nst1'
        media.media_command = None
        media.mt = Mock()
        error = OSError(errno.ENXIO, 'No such device or address', '/dev/tty')
        with patch.object(tb, 'open', side_effect=error, create=True), \
                self.assertRaisesRegex(tb.BackupError, 'No terminal.*No such device or address'):
            media.load('a'*32, 1, True)
        media.mt.assert_not_called()


class ReadAheadTests(unittest.TestCase):
    def test_partial_next_chunk_is_visible_while_writer_holds_current_chunk(self):
        progress = tb.Progress('Test', buffer_size=4 * 1024**2)
        partial, resume = threading.Event(), threading.Event()

        class SlowStream:
            reads = 0

            def read(self, size):
                self.reads += 1
                if self.reads <= 5:
                    return b'x' * 1024**2
                partial.set()  # One MiB of the second chunk has been counted.
                if not resume.wait(3):
                    raise AssertionError('Reader was not released')
                return b''

        def source():
            for chunk in tb.read_chunks(SlowStream(), 4 * 1024**2, progress):
                yield 'data', chunk

        reader = tb.ReadAhead(source(), resume.set, progress)
        with reader as frames:
            kind, chunk = next(frames)
            self.assertEqual(len(chunk), 4 * 1024**2)
            self.assertTrue(partial.wait(3))
            self.assertEqual(progress.read_bytes, 5 * 1024**2)
            self.assertEqual(progress.reader_state, 'reading')
            progress.phase = 'writing volume 1, chunk 0'
            output = io.StringIO()
            with redirect_stderr(output):
                progress.report()
            self.assertIn('5.0 MiB read', output.getvalue())
            self.assertIn('writing volume 1, chunk 0', output.getvalue())
            self.assertIn('reader reading', output.getvalue())
            self.assertIn('MiB/s source', output.getvalue())
            del chunk
            resume.set()
            remaining = list(frames)
            self.assertEqual(len(remaining), 1)
            self.assertEqual(len(remaining[0][1]), 1024**2)
        self.assertEqual(progress.read_bytes, 5 * 1024**2)
        self.assertEqual(progress.reader_state, 'finished')

    def test_full_buffers_are_reported_as_reader_waiting(self):
        progress = tb.Progress('Test')

        def source():
            for chunk in tb.read_chunks(io.BytesIO(b'abc'), 1, progress):
                yield 'data', chunk

        reader = tb.ReadAhead(source(), lambda: None, progress)
        with reader as frames:
            self.assertEqual(next(frames), ('data', b'a'))
            with reader.condition:
                ready = reader.condition.wait_for(
                    lambda: progress.reader_state == 'waiting (buffers full)', timeout=3)
            self.assertTrue(ready)
            self.assertEqual(progress.read_bytes, 2)
        self.assertFalse(reader.thread.is_alive())

    def test_source_error_is_reported_after_queued_data(self):
        error = tb.BackupError('Source failed')
        closed = threading.Event()
        progress = tb.Progress('Test')

        def source():
            try:
                for chunk in tb.read_chunks(io.BytesIO(b'first'), 5, progress):
                    yield 'data', chunk
                raise error
            finally:
                closed.set()

        reader = tb.ReadAhead(source(), lambda: None, progress)
        with reader as frames:
            self.assertEqual(next(frames), ('data', b'first'))
            with self.assertRaises(tb.BackupError) as raised:
                next(frames)
            self.assertIs(raised.exception, error)
        self.assertEqual(progress.read_bytes, 5)
        self.assertTrue(closed.is_set())
        self.assertFalse(reader.thread.is_alive())

    def test_cancellation_unblocks_pipe_reader_and_closes_source(self):
        process = subprocess.Popen(
            [sys.executable, '-c', "import os, signal; signal.alarm(10); os.write(1, b'x'); os.read(0, 1)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(tb.stop_process, process)
        blocked, closed = threading.Event(), threading.Event()

        def source():
            try:
                yield 'data', process.stdout.read(1)
                blocked.set()
                yield 'data', process.stdout.read(1)
            finally:
                tb.stop_process(process)
                closed.set()

        reader = tb.ReadAhead(source(), lambda: tb.stop_process(process, close_streams=False),
                              tb.Progress('Test'))
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            with reader as frames:
                self.assertEqual(next(frames), ('data', b'x'))
                self.assertTrue(blocked.wait(3))
                raise KeyboardInterrupt
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(closed.is_set())
        self.assertFalse(reader.thread.is_alive())
        self.assertIsNotNone(process.poll())


if __name__ == '__main__':
    unittest.main()
