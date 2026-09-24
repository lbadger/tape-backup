"""Overall progress across tapes, restore chains, verification, and finalization."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_cli_status import TerminalOutput
from test_tape_backup import tree_contents


class TotalProgressTests(unittest.TestCase):
    def test_estimated_backup_caps_below_complete_until_commit_and_metadata_finish(self):
        progress = tb.Progress('a' * 32, archives=1)
        progress.track_archive(100)
        progress.advance(25)
        self.assertIn('~25.0%', progress.total_progress())
        progress.advance(175)  # Live files/tar overhead can exceed the estimate.
        progress.finish_archive()
        progress.phase = 'flushing volume 2 (backup completion)'
        output = TerminalOutput()
        with redirect_stderr(output):
            progress.report()
        self.assertIn('~99.9%', output.getvalue())
        self.assertIn('ETA finalizing', output.getvalue())
        self.assertNotIn('100.0%', output.getvalue())
        progress.phase = 'complete'
        self.assertIn('100.0% (complete)', progress.total_progress())

    def test_chain_and_two_pass_progress_do_not_reset_between_archives(self):
        progress = tb.Progress('Restore', archives=2, passes=2)
        progress.track_archive(100, archive=0, stage='verification')
        progress.advance(50)
        self.assertIn('~12.5%', progress.total_progress())
        progress.finish_archive()
        self.assertIn('~25.0%', progress.total_progress())
        progress.track_archive(100, archive=1, stage='verification')
        self.assertIn('~25.0%', progress.total_progress())
        progress.finish_archive()
        self.assertIn('~50.0%', progress.total_progress())
        progress.track_archive(900, archive=0, pass_number=1, stage='restore', sizes=[900, 100])
        progress.advance(450)
        self.assertIn('72.5%', progress.total_progress())
        progress.finish_archive()
        self.assertIn('95.0%', progress.total_progress())
        progress.track_archive(100, archive=1, pass_number=1, stage='restore', sizes=[900, 100])
        self.assertIn('95.0%', progress.total_progress())

    def test_missing_size_is_unknown_and_terminal_bars_fit_narrow_displays(self):
        progress = tb.Progress('Restore', archives=2)
        for size in (None, 0, -1, 'unknown'):
            progress.track_archive(size, stage='restore')
            progress.advance(100)
            self.assertIn('--%', progress.total_progress())
            self.assertIn('size unknown', progress.total_progress())
        progress.finish_archive()
        self.assertIn('~50.0%', progress.total_progress())
        progress.track_archive(None, archive=1, stage='restore')
        self.assertIn('>=50.0%', progress.total_progress())
        for width in (40, 72, 120):
            output = TerminalOutput()
            with redirect_stderr(output), patch.object(tb, 'terminal_width', return_value=width):
                progress.report()
            self.assertTrue(all(len(line) <= width for line in output.getvalue().splitlines()))
            self.assertIn('Total [', output.getvalue())
        output = io.StringIO()
        with redirect_stderr(output):
            progress.report()
        self.assertIn('Total >=50.0%', output.getvalue())
        self.assertNotIn('Total [', output.getvalue())
        self.assertNotIn('\x1b', output.getvalue())
        progress.track_archive(0, sizes=[0, 0], stage='restore')
        self.assertIn('size unknown', progress.total_progress())

    def test_verification_eta_advances_without_claiming_delivered_bytes(self):
        progress = tb.Progress('Restore', archives=1, passes=2)
        with patch.object(tb.time, 'monotonic', return_value=100):
            progress.track_archive(100, stage='verification')
        progress.advance(50)
        output = io.StringIO()
        with redirect_stderr(output), patch.object(tb.time, 'monotonic', return_value=110):
            progress.report()
        self.assertIn('ETA ~00:00:10', output.getvalue())
        self.assertIn('0.0 MiB delivered', output.getvalue())


class RestoreProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.media = tb.FileMedia(self.root / 'media')
        self.states, self.progress = [], []
        states, progress = self.states, self.progress
        class TrackedProgress(tb.Progress):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                progress.append(self)

            def finish_archive(self):
                super().finish_archive()
                states.append((self.label, dict(self.overall), self.total_progress()))
        self.output = TerminalOutput()
        for context in (patch.object(tb, 'Progress', TrackedProgress),
                        patch.object(tb.os, 'sync'), redirect_stderr(self.output)):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def backup(self, base=None):
        backup_id = tb.backup(self.source, self.media, level='incremental' if base else 'full',
                              base=base, buffer_size=128 * 1024, volume_size=512 * 1024, quiet=True)
        return backup_id, self.media.last_result['data_bytes']

    def test_backup_and_fresh_restore_chain_use_unique_payload_and_all_archives(self):
        (self.source / 'book').write_bytes(os.urandom(900_000))
        full, full_size = self.backup()
        self.assertGreater(self.media.last_result['volumes'], 1)
        self.assertEqual(self.states[-1][1]['done'], full_size)
        (self.source / 'new').write_bytes(os.urandom(30_000))
        delta, delta_size = self.backup(full)
        self.states.clear()
        destination = self.root / 'restored'
        tb.restore([full, delta], destination, self.media, quiet=True)
        self.assertEqual(tree_contents(destination), tree_contents(self.source))
        self.assertEqual([s[1]['archive'] for s in self.states], [0, 1])
        self.assertEqual([s[1]['done'] for s in self.states], [full_size, delta_size])
        self.assertIn('~50.0%', self.states[0][2])
        self.assertIn('~99.9%', self.states[1][2])
        final = self.progress[-1]
        self.assertEqual(final.written_bytes, full_size + delta_size)
        self.assertIn('100.0% (complete)', final.total_progress())

    def test_stepwise_restore_total_includes_verify_and_apply(self):
        (self.source / 'book').write_bytes(os.urandom(100_000))
        full, _ = self.backup()
        destination = self.root / 'restored'
        tb.restore([full], destination, self.media, quiet=True)
        (self.source / 'new').write_bytes(os.urandom(700_000))
        delta, delta_size = self.backup(full)
        (self.source / 'last').write_bytes(os.urandom(40_000))
        last, last_size = self.backup(delta)
        self.states.clear()
        tb.restore([delta, last], destination, self.media, quiet=True)
        self.assertEqual(tree_contents(destination), tree_contents(self.source))
        self.assertEqual([s[1]['stage'] for s in self.states], ['verification'] * 2 + ['restore'] * 2)
        self.assertEqual([s[1]['pass'] for s in self.states], [0, 0, 1, 1])
        self.assertEqual(self.states[2][1]['sizes'], [delta_size, last_size])
        final = self.progress[-1]
        self.assertEqual(final.read_bytes, 2 * (delta_size + last_size))
        self.assertEqual(final.written_bytes, delta_size + last_size)
        self.assertIn('100.0% (complete)', final.total_progress())

    def test_failed_restore_never_displays_complete_even_after_all_payload_is_delivered(self):
        (self.source / 'book').write_text('content')
        full, _ = self.backup()
        extract = tb.extract_restore_stream
        def fail(*args):
            extract(*args)
            raise tb.BackupError('Extraction failure')
        self.output.seek(0)
        self.output.truncate()
        with patch.object(tb, 'extract_restore_stream', fail), self.assertRaises(tb.BackupError):
            tb.restore([full], self.root / 'restored', self.media, quiet=True)
        self.assertNotIn('100.0%', self.output.getvalue())
        self.assertFalse((self.root / 'restored').exists())

    def test_backup_verification_is_one_progress_operation_and_retains_media_lock(self):
        (self.source / 'book').write_bytes(os.urandom(700000))
        original_scan = tb.scan
        def verify(media, backup_id, **kwargs):
            self.assertNotIn('100.0%', self.output.getvalue())
            progress = kwargs['progress']
            self.assertIn('50.0%', progress.total_progress())
            self.assertEqual(progress.total_bytes, media.last_result['data_bytes'])
            with self.assertRaises(tb.BackupError), media.lock():
                pass
            return original_scan(media, backup_id, **kwargs)
        with patch.object(tb, 'scan', verify):
            backup_id = tb.backup(self.source, self.media, buffer_size=128 * 1024,
                                  volume_size=512 * 1024, quiet=True, verify=True)
        self.assertTrue(self.media.last_result['data_verified'])
        self.assertEqual(self.output.getvalue().count('100.0% (complete)'), 1)
        self.assertIn(f"Completed {backup_id}: {self.media.last_result['data_bytes']} archive bytes",
                      self.output.getvalue())
        self.assertEqual([state['stage'] for _, state, _ in self.states], ['backup', 'verification'])

    def test_failed_backup_verification_never_displays_complete_and_preserves_commit_result(self):
        (self.source / 'book').write_text('content')
        with patch.object(tb, 'scan', side_effect=tb.BackupError('Checksum failure')), \
                self.assertRaisesRegex(tb.BackupError, 'was committed, but read-back verification failed'):
            tb.backup(self.source, self.media, quiet=True, verify=True)
        self.assertTrue(self.media.last_result['archive_complete'])
        self.assertFalse(self.media.last_result['data_verified'])
        self.assertNotIn('100.0%', self.output.getvalue())

    def test_real_standalone_verify_reports_payload_percentage_and_eta_without_delivery(self):
        (self.source / 'book').write_bytes(os.urandom(700000))
        full, size = self.backup()
        self.output.seek(0)
        self.output.truncate()
        advance, samples = tb.Progress.advance, []
        def report(progress, count):
            advance(progress, count)
            progress.eta_started = tb.time.monotonic() - 10
            progress.report()
            samples.append(progress.overall['done'])
            self.assertEqual(progress.written_bytes, 0)
        with patch.object(tb.Progress, 'advance', report):
            result = tb.scan(self.media, full)
        self.assertTrue(result['data_verified'])
        self.assertEqual(samples[-1], size)
        self.assertGreater(len(samples), 1)
        self.assertIn('ETA ~', self.output.getvalue())
        self.assertIn('(verification, estimated)', self.output.getvalue())
        self.assertIn('100.0% (complete)', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
