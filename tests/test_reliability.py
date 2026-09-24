"""Read retries, efficient inspection, and device-wide exclusion of competing jobs."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_append import TapeMedia, TapeVolume
from test_tape_backup import tree_contents


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'book').write_bytes(os.urandom(700_000))
        context = redirect_stderr(io.StringIO())
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        for context in (patch.object(tb.os, 'sync'), patch.object(tb, 'DRIVE_LOCK_DIRECTORY', self.root)):
            context.start()
            self.addCleanup(context.stop)

    def create(self, media, base=None, cap=None):
        return tb.backup(self.source, media, level='incremental' if base else 'full',
                         base=base, volume_size=cap, buffer_size=tb.BLOCK_SIZE, quiet=True)

    def test_wrong_read_cartridge_twice_then_correct_continues_same_restore(self):
        class RetryMedia(TapeMedia):
            attempts = 0
            def request(self, backup_id, number, action):
                super().request(backup_id, number, action)
                if action == 'read' and number == 2:
                    self.attempts += 1
                    if self.attempts < 3:
                        self.active = self.tapes[0]
        media = RetryMedia()
        full = self.create(media, cap=8 * tb.BLOCK_SIZE)
        before = [list(tape.records) for tape in media.tapes]
        media.loaded = False
        destination = self.root / 'restored'
        tb.restore([full], destination, media, quiet=True)
        self.assertEqual(media.attempts, 3)
        self.assertEqual(tree_contents(destination), tree_contents(self.source))
        self.assertEqual(before, [tape.records for tape in media.tapes])

    def test_cancel_wrong_read_does_not_loop_or_keep_partial_restore(self):
        class CancelMedia(TapeMedia):
            attempts = 0
            def request(self, backup_id, number, action):
                super().request(backup_id, number, action)
                if action == 'read' and number == 2:
                    self.attempts += 1
                    if self.attempts == 2:
                        raise tb.BackupError('Media change cancelled')
                    self.active = self.tapes[0]
        media = CancelMedia()
        full = self.create(media, cap=8 * tb.BLOCK_SIZE)
        media.loaded = False
        with self.assertRaisesRegex(tb.BackupError, 'cancelled'):
            tb.restore([full], self.root / 'restored', media, quiet=True)
        self.assertEqual(media.attempts, 2)
        self.assertFalse(list(self.root.glob('.restored.restoring-*')))

    def test_catalog_reads_each_header_once_with_one_eod_seek(self):
        media = TapeMedia()
        full = self.create(media)
        (self.source / 'new').write_text('delta')
        delta = self.create(media, base=full)
        operations = []
        original = TapeVolume.control
        def control(volume, operation, count=1):
            operations.append((operation, count))
            return original(volume, operation, count)
        with patch.object(TapeVolume, 'control', control), \
                patch.object(tb.os, 'memfd_create', side_effect=AssertionError('Unneeded snapshot allocation')):
            result = tb.inspect_all(media)
        self.assertEqual([b['id'] for b in result['backups']], [full, delta])
        self.assertEqual(sum(op == tb.MTEOM for op, count in operations), 1)
        positions = [count for op, count in operations if op == tb.MTSEEK]
        self.assertEqual(len(positions), 2)
        self.assertEqual(len(set(positions)), 2)

    def test_inspection_reports_missing_catalog_without_scanning_unless_enabled(self):
        media = TapeMedia()
        with patch.object(tb, 'write_metadata', return_value=False):
            full = self.create(media)
        with patch.object(TapeVolume, 'skip_payload', side_effect=AssertionError('Unexpected archive scan')):
            result = tb.inspect_all(media, allow_scan=False)
        self.assertFalse(result['scan_complete'])
        self.assertTrue(result['errors'][0]['scan_required'])
        self.assertEqual(tb.inspect_all(media)['backups'][0]['id'], full)

    def test_incomplete_inspect_has_nonzero_status_and_retains_discovered_backup(self):
        media = tb.FileMedia(self.root / 'media')
        full = self.create(media)
        tape = next(media.directory.glob('*.tape'))
        with tape.open('ab') as stream:
            stream.write(b'incomplete next backup')
        output = io.StringIO()
        with redirect_stdout(output):
            code = tb.main(['inspect', '--media-dir', str(media.directory)])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result['scan_complete'])
        self.assertEqual(result['backups'][0]['id'], full)

    def test_mode_aliases_and_different_homes_share_one_lock(self):
        with tb.drive_lock(os.makedev(9, 128)):
            with patch.object(Path, 'home', return_value=self.root / 'different-user'):
                for minor in (128, 160, 192, 224):
                    with self.subTest(minor=minor), self.assertRaisesRegex(tb.BackupError, 'Another operation'):
                        with tb.drive_lock(os.makedev(9, minor)):
                            self.fail('Competing drive operation acquired the lock')
            with tb.drive_lock(os.makedev(9, 129)):
                pass  # A separate physical drive is independent.
        with tb.drive_lock(os.makedev(9, 224)):
            pass

    def test_lock_symlink_is_rejected(self):
        (self.root / 'tape-backup-drive-9-0.lock').symlink_to(self.source / 'book')
        with self.assertRaisesRegex(tb.BackupError, 'shared drive lock'):
            with tb.drive_lock(os.makedev(9, 128)):
                self.fail('Followed a lock symlink')


if __name__ == '__main__':
    unittest.main()
