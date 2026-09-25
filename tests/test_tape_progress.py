"""Tape capacity, separate ETAs, and streaming bottleneck accounting."""
from contextlib import redirect_stderr
import ctypes
import errno
import io
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tape_backup as tb
from test_append import TapeMedia, TapeVolume
from test_cli_status import TerminalOutput
from test_tape_backup import tree_contents


class CapacityTests(unittest.TestCase):
    def response(self, remaining=500, maximum=1000, *, resid=0, status=0, length=8):
        def ioctl(fd, operation, raw, mutate):
            self.assertEqual((fd, operation, mutate), (42, 0x2285, True))
            header = tb.SgIoHeader.from_buffer(raw)
            command = ctypes.string_at(header.cmdp, header.cmd_len)
            self.assertEqual(command, b'\x8c' + bytes(9) + (30).to_bytes(4, 'big') + bytes(2))
            self.assertEqual(header.dxfer_direction, -3)
            payload = struct.pack('>IHBHQHBHQ', 26, 0, 0x80, length, remaining,
                                  1, 0x80, 8, maximum)
            ctypes.memmove(header.dxferp, payload, len(payload))
            header.resid, header.status = resid, status
        return ioctl

    def test_read_only_capacity_attributes_are_converted_from_mib(self):
        with patch.object(tb.fcntl, 'ioctl', self.response()):
            self.assertEqual(tb.tape_capacity(42), (1000 * 1024**2, 500 * 1024**2))

    def test_unsupported_truncated_and_invalid_capacity_are_unknown(self):
        cases = [self.response(resid=1), self.response(status=2), self.response(length=7),
                 self.response(maximum=0), self.response(remaining=2000),
                 self.response(remaining=2**64 - 1, maximum=2**64 - 1)]
        for response in cases:
            with self.subTest(response=response), patch.object(tb.fcntl, 'ioctl', response):
                self.assertIsNone(tb.tape_capacity(42))
        with patch.object(tb.fcntl, 'ioctl', side_effect=OSError(errno.EPERM, 'Unavailable')):
            self.assertIsNone(tb.tape_capacity(42))


class TapeEstimateTests(unittest.TestCase):
    def setUp(self):
        context = patch.object(tb.time, 'monotonic', return_value=0)
        context.start()
        self.addCleanup(context.stop)
        self.unit = tb.BLOCK_SIZE

    def progress(self, *, size=1900, used=0, capacity=1000, writing=True, archives=1, passes=1):
        progress = tb.Progress('Test', archives=archives, passes=passes)
        progress.track_archive(size * self.unit)
        volume = SimpleNamespace(record_bytes=used * self.unit, capacity=Mock(return_value=None))
        progress.start_tape(volume, 1, writing=writing,
                            capacity=capacity * self.unit if capacity else None)
        return progress, volume

    def test_tape_and_job_eta_are_independent_and_final_tape_stops_at_archive_end(self):
        progress, volume = self.progress()
        volume.record_bytes += 250 * self.unit
        progress.advance(250 * self.unit)
        self.assertIn('Tape 1 of ~2', progress.tape_progress(10))
        self.assertIn('Tape ETA ~00:00:30', progress.tape_progress(10))
        self.assertEqual(progress.job_eta(10, 'unused'), '~00:01:06')
        progress.estimate(600 * self.unit)
        self.assertIn('Tape 1 of ~1', progress.tape_progress(10))
        self.assertIn('Tape ETA ~00:00:14', progress.tape_progress(10))

    def test_append_uses_existing_bytes_and_capacity_override_avoids_query(self):
        progress, volume = self.progress(size=500, used=800)
        volume.capacity.assert_not_called()
        volume.record_bytes += 100 * self.unit
        progress.advance(100 * self.unit)
        self.assertIn('Tape 1 of ~2', progress.tape_progress(10))
        self.assertIn('Tape ETA ~00:00:10', progress.tape_progress(10))

    def test_detected_append_capacity_accounts_for_existing_compression(self):
        progress = tb.Progress('Test', archives=1)
        progress.track_archive(1000 * self.unit)
        volume = SimpleNamespace(record_bytes=1000 * self.unit,
                                 capacity=Mock(return_value=(1000 * self.unit, 500 * self.unit)))
        progress.start_tape(volume, 1, writing=True)
        self.assertEqual(progress.tape['capacity'], 2000 * self.unit)
        self.assertIn('estimated append capacity', progress.tape_progress(10))

    def test_unknown_capacity_learns_from_full_tape_but_not_from_io_error(self):
        progress, volume = self.progress(capacity=None)
        self.assertIn('Tape 1 of ?', progress.tape_progress(10))
        self.assertIn('Tape ETA calculating', progress.tape_progress(10))
        volume.record_bytes = 1000 * self.unit
        progress.end_tape()
        self.assertEqual(progress.observed_capacity, {})
        progress.end_tape(full=True)
        next_volume = SimpleNamespace(record_bytes=0, capacity=Mock(return_value=None))
        progress.start_tape(next_volume, 2, writing=True)
        next_volume.capacity.assert_not_called()
        self.assertEqual(progress.tape['capacity'], 1000 * self.unit)
        self.assertIn('observed capacity', progress.tape_progress(10))

    def test_restore_reuses_verified_tape_span_and_counts_shared_cartridges_once(self):
        progress, volume = self.progress(writing=False, archives=2)
        progress.archive_sizes = {0: 1900 * self.unit, 1: 100 * self.unit}
        progress.archive_tapes = {0: 2, 1: 1}
        progress.archive_cartridges = {0: {1: 'first', 2: 'shared'}, 1: {1: 'shared'}}
        volume.record_bytes = 500 * self.unit
        progress.end_tape(full=True)
        progress.track_archive(1900 * self.unit, pass_number=1, stage='restore')
        volume.record_bytes = 0
        progress.start_tape(volume, 1, capacity=1000 * self.unit)
        volume.record_bytes = 250 * self.unit
        progress.advance(250 * self.unit)
        text = progress.tape_progress(10)
        self.assertIn('Tape 1 of 2 (archive 1/2)', text)
        self.assertIn('Tape ETA ~00:00:10', text)
        self.assertIn('Job tapes 2', text)

    def test_total_job_eta_spans_archives_and_verification_without_resetting(self):
        progress, volume = self.progress(size=100, archives=2, passes=2)
        progress.archive_sizes[1] = 300 * self.unit
        progress.advance(50 * self.unit)
        self.assertEqual(progress.job_eta(10, 'unused'), '~00:02:30')
        progress.advance(50 * self.unit)
        progress.finish_archive()
        progress.track_archive(300 * self.unit, archive=1, stage='verification')
        progress.advance(100 * self.unit)
        self.assertEqual(progress.job_eta(40, 'unused'), '~00:02:00')
        self.assertEqual(progress.job_done, 200 * self.unit)
        progress.track_archive(100 * self.unit, pass_number=1, stage='restore',
                               sizes=[100 * self.unit, 300 * self.unit])
        self.assertEqual(progress.archive_sizes, {0: 100 * self.unit, 1: 300 * self.unit})

    def test_media_wait_is_excluded_and_new_tape_gets_its_own_timer(self):
        progress, volume = self.progress()
        progress.advance(250 * self.unit)
        next_volume = SimpleNamespace(record_bytes=0, capacity=Mock(return_value=None))
        with patch.object(tb.time, 'monotonic', return_value=10):
            with progress.changing_media():
                self.assertEqual(progress.job_eta(40, 'unused'), 'waiting for media')
                self.assertIn('Tape ETA waiting for media', progress.tape_progress(40))
                with patch.object(tb.time, 'monotonic', return_value=50):
                    progress.start_tape(next_volume, 2, writing=True, capacity=1000 * self.unit)
                # Complete a simulated 60-second tape change.
                tb.time.monotonic.return_value = 70
        self.assertEqual(progress.job_eta_started, 60)
        self.assertEqual(progress.eta_started, 60)
        self.assertEqual(progress.tape['started'], 70)
        self.assertEqual(progress.job_eta(70, 'unused'), '~00:01:06')

    def test_estimate_exhaustion_does_not_claim_success(self):
        progress, volume = self.progress(size=100)
        progress.advance(150 * self.unit)
        volume.record_bytes = 150 * self.unit
        self.assertIn('finishing (estimate reached)', progress.tape_progress(10))
        self.assertEqual(progress.job_eta(10, 'unused'), 'finishing (estimate reached)')
        progress.finish_archive()
        self.assertEqual(progress.job_eta(10, 'unused'), 'finalizing')
        progress.phase = 'complete'
        self.assertEqual(progress.job_eta(10, 'unused'), '00:00:00')

    def test_known_final_restore_tape_has_eta_without_capacity_telemetry(self):
        progress, volume = self.progress(size=600, capacity=None, writing=False)
        progress.archive_tapes[0] = 1
        volume.record_bytes = 250 * self.unit
        progress.advance(250 * self.unit)
        self.assertIn('Tape 1 of 1', progress.tape_progress(10))
        self.assertIn('Tape ETA ~00:00:14', progress.tape_progress(10))

    def test_inventory_supplies_full_chain_sizes_and_unknown_sizes_stay_unknown(self):
        progress, volume = self.progress(archives=2)
        self.assertIn('sizes unknown', progress.job_eta(10, 'unused'))
        media = SimpleNamespace(inventory_entries=[
            {'id': 'a', 'data_bytes': 200 * self.unit, 'expected_volumes': 1},
            {'id': 'b', 'data_bytes': 100 * self.unit, 'expected_volumes': 1}])
        progress.seed_archives(['a', 'b'], media)
        progress.estimate(500 * self.unit)  # Prefer exact catalog size over a header estimate.
        progress.advance(100 * self.unit)
        self.assertEqual(progress.total_bytes, 200 * self.unit)
        self.assertEqual(progress.job_eta(10, 'unused'), '~00:00:20')

    def test_new_fields_render_in_terminal_and_redirected_logs(self):
        progress, volume = self.progress()
        volume.record_bytes = 250 * self.unit
        progress.advance(250 * self.unit)
        for stream in (TerminalOutput(), io.StringIO()):
            with redirect_stderr(stream), patch.object(tb.time, 'monotonic', return_value=10), \
                    patch.object(tb, 'terminal_width', return_value=60):
                progress.report()
            text = stream.getvalue()
            self.assertIn('Total job ETA ~00:01:06', text)
            self.assertIn('Tape 1 of ~2', text)
            self.assertIn('Tape ETA ~00:00:30', text)
            if stream.isatty():
                self.assertTrue(all(len(line) <= 60 for line in text.splitlines()))

    def test_wait_timing_includes_active_and_failed_operations(self):
        progress = tb.Progress('Test', buffer_size=1024**3)
        with progress.timing('source'):
            self.assertIn('source 3.0s', progress.streaming_waits(3))
            tb.time.monotonic.return_value = 5
        with self.assertRaises(OSError):
            with progress.timing('flush'):
                tb.time.monotonic.return_value = 7
                raise OSError('failed flush')
        self.assertEqual(progress.wait_seconds, {'source': 5, 'flush': 2, 'position': 0})
        self.assertEqual(progress.wait_started, {})


class TapeProgressIntegrationTests(unittest.TestCase):
    def test_backup_restore_and_verification_report_each_tape_without_extra_reads(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), \
                patch.object(tb.os, 'sync'):
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'book').write_bytes(os.urandom(1_000_000))
            media = TapeMedia(capacity=12)
            start, samples, progress_objects = tb.Progress.start_tape, [], []
            def track(progress, volume, number, **kwargs):
                start(progress, volume, number, **kwargs)
                samples.append((progress.overall['stage'], number))
                progress_objects.append(progress)
            with patch.object(tb.Progress, 'start_tape', track), \
                    patch.object(TapeVolume, 'capacity', return_value=(12 * tb.BLOCK_SIZE, 12 * tb.BLOCK_SIZE)):
                backup_id = tb.backup(source, media, buffer_size=256 * 1024, quiet=True, verify=True)
                numbers = list(range(1, len(media.tapes) + 1))
                self.assertEqual(samples, [('backup', n) for n in numbers] +
                                 [('verification', n) for n in numbers])
                self.assertEqual(progress_objects[-1].archive_tapes[0], len(media.tapes))
                samples.clear()
                destination = Path(directory) / 'restored'
                tb.restore([backup_id], destination, media, quiet=True)
                self.assertEqual(samples, [('restore', n) for n in numbers])
                self.assertEqual(tree_contents(source), tree_contents(destination))

    def test_capacity_override_is_advisory_and_cli_accepts_it_for_live_operations(self):
        parser = tb.make_parser()
        for command, extra in [('backup', ['--source', '/source']),
                               ('restore', ['--backup', 'a' * 32, '--destination', '/restore']),
                               ('zfs-backup', ['--snapshot', 'tank/books@daily']),
                               ('zfs-restore', ['--backup', 'a' * 32, '--dataset', 'tank/restore'])]:
            args = parser.parse_args([command, *extra, '--cartridge-capacity', '4GiB'])
            self.assertEqual(args.cartridge_capacity, 4 * 1024**3)
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), \
                patch.object(tb.os, 'sync'):
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'book').write_bytes(os.urandom(1_000_000))
            media = TapeMedia()
            with patch.object(TapeVolume, 'capacity', side_effect=AssertionError('Override should avoid querying')):
                backup_id = tb.backup(source, media, cartridge_capacity=256 * 1024, quiet=True)
                self.assertEqual(len(media.tapes), 1)
                tb.restore([backup_id], Path(directory) / 'restored', media,
                           cartridge_capacity=256 * 1024, quiet=True)


if __name__ == '__main__':
    unittest.main()
