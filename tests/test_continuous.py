"""Continuous writes, delayed durability, and format validation."""
from collections import defaultdict
from contextlib import redirect_stderr
import ctypes
import errno
import hashlib
import io
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import tape_backup as tb
from test_tape_backup import tree_contents


class BufferedMedia(tb.FileMedia):
    """Model accepted records ahead of durable records, including lost tails."""

    def __init__(self, directory, *, lag=6, fail_write=False, fail_commit=False,
                 lose_tail=True, telemetry=True, fail_position=False, fail_headers=()):
        super().__init__(directory)
        self.lag, self.telemetry = lag, telemetry
        self.fail_write, self.fail_commit, self.lose_tail = fail_write, fail_commit, lose_tail
        self.fail_position, self.fail_headers = fail_position, fail_headers
        self.commits = defaultdict(int)
        self.positions = []
        self.max_recovery = 0
        self.failures = 0

    def load(self, backup_id, number, writing):
        super().load(backup_id, number, writing)
        self.number = number

    def open(self, writing):
        volume = super().open(writing)
        if not writing:
            return volume
        owner, number = self, self.number

        class BufferedVolume(tb.Volume):
            def lose(self):
                owner.failures += 1
                if owner.lose_tail:
                    self.stream.truncate(self.durable_objects * tb.BLOCK_SIZE)
                raise OSError(errno.ENOSPC, 'Injected loss of buffered tail')

            def write(self, record):
                if owner.fail_write and number == 1 and self.objects >= 25:
                    self.lose()
                super().write(record)
                if self.progress:
                    owner.max_recovery = max(owner.max_recovery, self.progress.retry_bytes)

            def commit(self):
                owner.commits[number] += 1
                if number in owner.fail_headers and owner.commits[number] == 1:
                    self.lose()
                if owner.fail_commit and number == 1 and owner.commits[number] == 2:
                    self.lose()
                super().commit()

            def durable_position(self):
                if owner.fail_position and number == 1 and self.objects >= 25:
                    self.lose()
                if not owner.telemetry:
                    return None
                self.durable_objects = max(self.durable_objects, self.objects - owner.lag)
                owner.positions.append((number, self.objects, self.durable_objects))
                return self.durable_objects

        return BufferedVolume(volume.stream)


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        # Distinct blocks make reordered or omitted data detectable.
        (self.source / 'book').write_bytes(b''.join(bytes([i]) * 65536 for i in range(48)))
        self.output = io.StringIO()
        context = redirect_stderr(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        for name, value in [('FRAME_SIZE', 2 * tb.BLOCK_SIZE), ('POSITION_INTERVAL', 2 * tb.BLOCK_SIZE)]:
            context = patch.object(tb, name, value)
            context.start()
            self.addCleanup(context.stop)

    def round_trip(self, media, *, buffer=1024**2, cap=None):
        backup_id = tb.backup(self.source, media, buffer_size=buffer, volume_size=cap, quiet=True)
        self.assertTrue(tb.scan(media, backup_id)['data_verified'])
        dest = self.root / ('restored-' + media.directory.name)
        tb.restore([backup_id], dest, media, quiet=True)
        self.assertEqual(tree_contents(self.source), tree_contents(dest))
        self.assertLessEqual(media.max_recovery, max(2 * tb.BLOCK_SIZE, buffer))
        return backup_id

    def test_continuous_writes_retire_durable_frames_without_per_frame_commits(self):
        media = BufferedMedia(self.root / 'media')
        self.round_trip(media)
        # Header, archive completion, and metadata file; ~26 archive frames.
        self.assertEqual(dict(media.commits), {1: 3})
        self.assertGreater(len(media.positions), 20)
        self.assertTrue(any(accepted > durable > 1 for _, accepted, durable in media.positions))
        self.assertIn('recovery 0.0 MiB', self.output.getvalue())
        self.assertIn('committed 3.0 MiB', self.output.getvalue())

    def test_write_failure_replays_multiple_lost_or_duplicate_frames(self):
        for lose in (False, True):
            with self.subTest(lose=lose):
                media = BufferedMedia(self.root / f'media-{lose}', fail_write=True, lose_tail=lose)
                full = self.round_trip(media)
                self.assertEqual(media.failures, 1)
                with (media.directory / f'{full}.0002.tape').open('rb') as stream:
                    head = tb.decoded_header(stream.read(tb.BLOCK_SIZE))
                self.assertGreater(head['sequence'], 0)  # Confirmed prefix was discarded safely.
                self.assertIn('replaying 3 uncommitted chunk(s)', self.output.getvalue())

    def test_deferred_position_error_replays_unconfirmed_tail(self):
        media = BufferedMedia(self.root / 'media', fail_position=True)
        self.round_trip(media)
        self.assertEqual(media.failures, 1)

    def test_replacement_header_failure_aborts_without_publishing_success(self):
        media = BufferedMedia(self.root / 'media', fail_write=True, fail_headers=(2,))
        with self.assertRaises(OSError):
            tb.backup(self.source, media, buffer_size=1024**2, quiet=True)
        self.assertEqual(media.failures, 2)
        self.assertNotIn('Completed', self.output.getvalue())
        self.assertEqual(len(list(media.directory.glob('*.tape'))), 2)
        with self.assertRaises(tb.BackupError):
            tb.scan(media)

    def test_completion_is_flushed_even_if_position_already_confirmed_every_frame(self):
        media = BufferedMedia(self.root / 'media', lag=0)
        self.round_trip(media)
        self.assertEqual(dict(media.commits), {1: 3})

    def test_final_flush_failure_replays_entire_window_without_telemetry(self):
        for lose in (False, True):
            with self.subTest(lose=lose):
                media = BufferedMedia(self.root / f'media-{lose}', fail_commit=True,
                                      lose_tail=lose, telemetry=False)
                full = self.round_trip(media, buffer=8 * 1024**2)
                self.assertEqual(media.failures, 1)
                with (media.directory / f'{full}.0002.tape').open('rb') as stream:
                    head = tb.decoded_header(stream.read(tb.BLOCK_SIZE))
                self.assertEqual(head['sequence'], 0)

    def test_position_lag_or_missing_telemetry_uses_bounded_commit_fallback(self):
        for telemetry in (False, True):
            with self.subTest(telemetry=telemetry):
                media = BufferedMedia(self.root / f'media-{telemetry}', lag=1000, telemetry=telemetry)
                self.round_trip(media)
                self.assertGreater(media.commits[1], 2)
                self.assertLess(media.commits[1], 12)  # Commits cover several frames.

    def test_planned_volumes_commit_before_switching(self):
        media = BufferedMedia(self.root / 'media')
        self.round_trip(media, cap=1024**2)
        self.assertGreater(len(media.commits), 1)
        self.assertTrue(all(count == 2 for n, count in media.commits.items() if n != media.number))
        self.assertIn(media.commits[media.number], (2, 3))  # Footer when space permits.
        self.assertTrue(all(p.stat().st_size <= 1024**2 for p in media.directory.glob('*.tape')))

    def test_replayed_frame_with_valid_but_changed_checksum_is_rejected(self):
        media = BufferedMedia(self.root / 'media', fail_write=True, lose_tail=False)
        full = self.round_trip(media)
        second = media.directory / f'{full}.0002.tape'
        with second.open('r+b') as stream:
            stream.seek(tb.BLOCK_SIZE)
            head = tb.decoded_header(stream.read(tb.BLOCK_SIZE))
            payload = bytearray(stream.read(head['length']))
            payload[0] ^= 1
            head['sha256'] = hashlib.sha256(payload).hexdigest()
            stream.seek(tb.BLOCK_SIZE)
            stream.write(tb.encoded_header(head))
            stream.write(payload)
        with self.assertRaisesRegex(tb.BackupError, 'replayed chunk differs'):
            tb.scan(media, full)

    def test_non_aligned_buffer_size_is_a_budget_not_a_frame_length(self):
        media = BufferedMedia(self.root / 'media')
        self.round_trip(media, buffer=3 * tb.BLOCK_SIZE + 123)

    def test_small_frame_writes_start_before_read_ahead_budget_fills(self):
        media = BufferedMedia(self.root / 'media')
        actual_send, actual_frames = tb.StreamWriter.send, tb.archive_frames
        first_write = threading.Event()

        def source(*args):
            for index, frame in enumerate(actual_frames(*args)):
                if index == 1:
                    self.assertTrue(first_write.wait(3), 'Writer waited for the large buffer to fill')
                yield frame

        def send(writer, kind, payload):
            first_write.set()
            return actual_send(writer, kind, payload)

        with patch.object(tb, 'archive_frames', source), patch.object(tb.StreamWriter, 'send', send):
            self.round_trip(media, buffer=tb.MAX_BUFFER)

    def test_reader_refills_each_free_slot_while_writer_keeps_working(self):
        progress = tb.Progress('queue')
        read_four, read_five = threading.Event(), threading.Event()

        def source():
            for i in range(8):
                if i == 3:
                    read_four.set()
                if i == 4:
                    read_five.set()
                yield 'data', bytes([i])

        reader = tb.ReadAhead(source(), lambda: None, progress, slots=4)
        with reader as frames:
            self.assertEqual(next(frames)[1], b'\0')
            self.assertTrue(read_four.wait(3))
            self.assertFalse(read_five.is_set())
            self.assertEqual(next(frames)[1], b'\1')
            self.assertTrue(read_five.wait(3))
            self.assertLessEqual(progress.queued_bytes, 3)
        self.assertEqual(progress.queued_bytes, 0)

    def test_unsupported_header_prefixes_are_rejected(self):
        media = BufferedMedia(self.root / 'media')
        backup_id = tb.backup(self.source, media, quiet=True)
        first = media.directory / f'{backup_id}.0001.tape'
        original = first.read_bytes()
        for magic in (b'TAPE-STREAM-1\n', b'TAPE-STREAM-2\n', b'TAPE-STREAM-4\n', b'TAPE-STREAM-5\n'):
            with self.subTest(magic=magic):
                first.write_bytes(magic + original[len(tb.MAGIC):])
                for verify in (False, True):
                    with self.assertRaisesRegex(tb.BackupError, 'Wrong tape format|Corrupt tape header'):
                        tb.scan(media, backup_id, verify=verify)
                destination = self.root / 'unsupported-restore'
                with self.assertRaisesRegex(tb.BackupError, 'Wrong tape format|Corrupt tape header'):
                    tb.restore([backup_id], destination, media, quiet=True)
                self.assertFalse(destination.exists())

    def test_noncurrent_format_declarations_are_rejected_on_every_volume(self):
        media = BufferedMedia(self.root / 'media')
        backup_id = tb.backup(self.source, media, volume_size=1024**2, quiet=True)
        for number in (1, 2):
            volume = media.directory / f'{backup_id}.{number:04d}.tape'
            original = volume.read_bytes()
            for version in (1, 2, 4):
                with self.subTest(volume=number, version=version):
                    header = tb.decoded_header(original[:tb.BLOCK_SIZE])
                    header['format'] = version
                    volume.write_bytes(tb.encoded_header(header) + original[tb.BLOCK_SIZE:])
                    for verify in (False, True):
                        with self.assertRaisesRegex(tb.BackupError, 'unsupported tape format|archive type does not match'):
                            tb.scan(media, backup_id, verify=verify)
            volume.write_bytes(original)


class PositionTests(unittest.TestCase):
    def test_routine_polling_is_less_frequent_and_buffer_pressure_still_checks(self):
        job = {'id': 'a' * 32, 'level': 'full', 'parent': None}
        for budget, expected_queries in ((128 * 1024**2, 1), (16 * 1024**2, 5)):
            with self.subTest(budget=budget):
                progress = tb.Progress('test')
                writer = tb.StreamWriter(Mock(), job, None, progress, budget)
                volume = writer.volume = Mock(objects=0)
                def write(record):
                    volume.objects += 1
                volume.write.side_effect = write
                volume.durable_position.side_effect = lambda: volume.objects
                payload = b'a' * (4 * 1024**2)
                for _ in range(16):
                    writer.send('data', payload)
                    self.assertLessEqual(writer.pending_bytes, budget)
                self.assertEqual(volume.durable_position.call_count, expected_queries)
                volume.commit.assert_not_called()
                self.assertEqual(progress.written_bytes, 64 * 1024**2)

    def ioctl(self, first=20, last=10, flags=0, resid=0, sense=None):
        def invoke(fd, operation, raw, mutate):
            self.assertEqual((fd, operation, mutate), (42, 0x2285, True))
            header = tb.SgIoHeader.from_buffer(raw)
            self.assertEqual(ctypes.string_at(header.cmdp, header.cmd_len), b'\x34' + bytes(9))
            self.assertEqual((header.interface_id, header.dxfer_direction, header.dxfer_len), (ord('S'), -3, 20))
            response = bytearray(20)
            response[0] = flags
            struct.pack_into('>II', response, 4, first, last)
            ctypes.memmove(header.dxferp, bytes(response), 20)
            header.resid = resid
            if sense:
                header.status = 2
                header.driver_status = 8
                header.sb_len_wr = len(sense)
                ctypes.memmove(header.sbp, sense, len(sense))
        return invoke

    def test_position_uses_medium_location_even_when_buffer_counts_are_unknown(self):
        with patch.object(tb.fcntl, 'ioctl', self.ioctl(flags=0x30)):
            self.assertEqual(tb.tape_position(42), (20, 10))

    def test_unsupported_unknown_truncated_and_inconsistent_positions_are_not_trusted(self):
        cases = [self.ioctl(flags=0x04), self.ioctl(flags=0x02), self.ioctl(resid=1),
                 self.ioctl(first=10, last=20), self.ioctl(sense=b'\x70\0\x05' + bytes(15)),
                 self.ioctl(sense=b'\x72\x05' + bytes(6))]
        for fake in cases:
            with self.subTest(fake=fake), patch.object(tb.fcntl, 'ioctl', fake):
                self.assertIsNone(tb.tape_position(42))
        with patch.object(tb.fcntl, 'ioctl', side_effect=OSError(errno.EPERM, 'No raw I/O capability')):
            self.assertIsNone(tb.tape_position(42))

    def test_deferred_errors_are_propagated_instead_of_confirming_data(self):
        for sense in (b'\x71\0\x03' + bytes(15), b'\x73\x03' + bytes(6)):
            with patch.object(tb.fcntl, 'ioctl', self.ioctl(sense=sense)), self.assertRaises(OSError):
                tb.tape_position(42)

    def test_volume_checks_host_count_and_monotonic_medium_position(self):
        for position in ((19, 10), (20, 4), None):
            with self.subTest(position=position):
                volume = tb.Volume(Mock(), physical=True)
                volume.objects, volume.durable_objects = 20, 5
                with patch.object(tb, 'tape_position', return_value=position), patch.object(tb, 'log'):
                    self.assertIsNone(volume.durable_position())
                self.assertTrue(volume.position_disabled)
                self.assertEqual(volume.durable_objects, 5)

    def test_format_3_metadata_scan_reads_records_instead_of_spacing_filemarks(self):
        stream = Mock()
        stream.read.return_value = b'x' * tb.BLOCK_SIZE
        with patch.object(tb.fcntl, 'ioctl') as ioctl:
            tb.Volume(stream, physical=True).skip_payload(2 * tb.BLOCK_SIZE + 1)
        ioctl.assert_not_called()
        self.assertEqual(stream.read.call_count, 3)


if __name__ == '__main__':
    unittest.main()
