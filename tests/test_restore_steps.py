"""Separate incremental restores, baseline identity, and interrupted apply."""
from contextlib import redirect_stderr
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_tape_backup import tree_contents


class RestoreStepsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'keep').write_bytes(os.urandom(150_000))
        os.link(self.source / 'keep', self.source / 'linked')
        (self.source / 'remove').write_text('delete later')
        (self.source / 'old-dir').mkdir()
        (self.source / 'old-dir' / 'file').write_text('rename me')
        self.media = tb.FileMedia(self.root / 'tapes')
        self.destination = self.root / 'restored'
        self.output = io.StringIO()
        capture = redirect_stderr(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        sync = patch.object(tb.os, 'sync')
        sync.start()
        self.addCleanup(sync.stop)
        self.full = tb.backup(self.source, self.media, quiet=True)
        self.full_tree = tree_contents(self.source)
        (self.source / 'remove').unlink()
        (self.source / 'added').write_text('first change')
        (self.source / 'old-dir').rename(self.source / 'new-dir')
        self.first = tb.backup(self.source, self.media, quiet=True, level='incremental', base=self.full)
        self.first_tree = tree_contents(self.source)
        (self.source / 'added').write_text('second change')
        shutil.rmtree(self.source / 'new-dir')
        (self.source / 'last').write_text('last file')
        self.second = tb.backup(self.source, self.media, quiet=True, level='incremental', base=self.first)
        self.last_tree = tree_contents(self.source)

    def restore(self, ids, **kwargs):
        return tb.restore(ids, self.destination, self.media, quiet=True, **kwargs)

    def state(self):
        return json.loads(tb.restore_marker_path(self.destination).read_text())

    def corrupt(self, backup_id):
        self.media.load(backup_id, 1, False)
        volume = self.media.open(False)
        volume.read()  # Volume header.
        header = tb.decoded_header(volume.read(boundary=True))
        self.assertEqual(header['kind'], 'data')
        offset = volume.stream.tell()
        volume.close()
        with self.media.path.open('r+b') as stream:
            stream.seek(offset)
            value = stream.read(1)
            stream.seek(offset)
            stream.write(bytes([value[0] ^ 1]))

    def test_one_increment_at_a_time_matches_all_at_once(self):
        for backup_id, expected in ((self.full, self.full_tree), (self.first, self.first_tree),
                                    (self.second, self.last_tree)):
            self.restore([backup_id])
            self.assertEqual(tree_contents(self.destination), expected)
            self.assertEqual(self.state()['backup']['id'], backup_id)
            self.assertEqual(self.state()['status'], 'complete')
        together = self.root / 'together'
        tb.restore([self.full, self.first, self.second], together, self.media, quiet=True)
        self.assertEqual(tree_contents(together), tree_contents(self.destination))
        self.assertEqual((self.destination / 'keep').stat().st_ino,
                         (self.destination / 'linked').stat().st_ino)

    def test_multiple_incrementals_apply_to_existing_full_without_copying_tree(self):
        self.restore([self.full])
        inode = (self.destination / 'keep').stat().st_ino
        self.restore([self.first, self.second])
        self.assertEqual(tree_contents(self.destination), self.last_tree)
        self.assertEqual((self.destination / 'keep').stat().st_ino, inode)
        self.assertFalse(list(self.root.glob('.restored.restoring-*')))

    def test_wrong_order_duplicate_and_full_reapply_leave_existing_restore_unchanged(self):
        self.restore([self.full])
        original_state = self.state()
        for ids in ([self.second], [self.full], [self.first, self.first], [self.first, self.full]):
            with self.subTest(ids=ids), self.assertRaises(tb.BackupError):
                self.restore(ids)
            self.assertEqual(tree_contents(self.destination), self.full_tree)
            self.assertEqual(self.state(), original_state)
        self.restore([self.first])
        with self.assertRaises(tb.BackupError):
            self.restore([self.first])
        self.assertEqual(tree_contents(self.destination), self.first_tree)

    def test_corrupt_later_incremental_is_detected_before_any_in_place_changes(self):
        self.restore([self.full])
        original_state = self.state()
        self.corrupt(self.second)
        with self.assertRaisesRegex(tb.BackupError, 'Checksum'):
            self.restore([self.first, self.second])
        self.assertEqual(tree_contents(self.destination), self.full_tree)
        self.assertEqual(self.state(), original_state)

    def test_failed_flush_marks_apply_incomplete_and_base_cannot_bypass_it(self):
        self.restore([self.full])
        with patch.object(tb.os, 'sync', side_effect=OSError(errno.EIO, 'Disk failure')):
            with self.assertRaises(OSError):
                self.restore([self.first])
        self.assertEqual(self.state()['backup']['id'], self.full)
        self.assertEqual(self.state()['status'], 'applying')
        self.assertEqual(self.state()['pending'], self.first)
        for ids, options in (([self.first], {}), ([self.second], {}),
                             ([self.second], {'base': self.first})):
            with self.subTest(ids=ids), self.assertRaisesRegex(tb.BackupError, 'did not finish'):
                self.restore(ids, **options)

    def test_cancelled_apply_does_not_delete_the_existing_restore(self):
        self.restore([self.full])
        with patch.object(tb, 'extract_restore_stream', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.restore([self.first])
        self.assertEqual(tree_contents(self.destination), self.full_tree)
        self.assertEqual(self.state()['status'], 'applying')

    def test_older_restore_can_be_adopted_once_without_extracting_full_again(self):
        self.restore([self.full])
        tb.restore_marker_path(self.destination).unlink()
        with self.assertRaisesRegex(tb.BackupError, '--base'):
            self.restore([self.first])
        with self.assertRaisesRegex(tb.BackupError, 'out-of-order'):
            self.restore([self.first], base=self.second)
        self.assertEqual(tree_contents(self.destination), self.full_tree)
        frames = tb.StreamReader.frames
        visited = []
        def observe(reader, **kwargs):
            visited.append(reader.backup_id)
            yield from frames(reader, **kwargs)
        with patch.object(tb.StreamReader, 'frames', observe):
            self.restore([self.first], base=self.full)
        self.assertEqual(visited, [self.first, self.first])
        self.restore([self.second])
        self.assertEqual(tree_contents(self.destination), self.last_tree)

    def test_different_directory_cannot_inherit_a_restore_history(self):
        self.restore([self.full])
        self.destination.rename(self.root / 'previous')
        self.destination.mkdir()
        (self.destination / 'valuable').write_text('do not replace')
        with self.assertRaises(tb.BackupError):
            self.restore([self.first])
        self.assertEqual((self.destination / 'valuable').read_text(), 'do not replace')

    def test_symlink_destination_and_symlink_history_are_rejected(self):
        self.restore([self.full])
        self.destination.rename(self.root / 'previous')
        self.destination.symlink_to(self.root / 'previous')
        with self.assertRaises(tb.BackupError):
            self.restore([self.first])
        self.destination.unlink()
        (self.root / 'previous').rename(self.destination)
        marker = tb.restore_marker_path(self.destination)
        marker.rename(self.root / 'saved-history')
        marker.symlink_to(self.root / 'saved-history')
        with self.assertRaises(OSError):
            self.restore([self.first])
        self.assertEqual(tree_contents(self.destination), self.full_tree)

    def test_removed_restore_can_start_over_without_using_stale_history(self):
        self.restore([self.full])
        shutil.rmtree(self.destination)
        self.restore([self.full, self.first])
        self.assertEqual(tree_contents(self.destination), self.first_tree)

    def test_base_cannot_override_recorded_parent_or_allow_incremental_without_full(self):
        with self.assertRaisesRegex(tb.BackupError, 'start with a full'):
            self.restore([self.first])
        self.assertFalse(self.destination.exists())
        self.restore([self.full])
        with self.assertRaisesRegex(tb.BackupError, '--base differs'):
            self.restore([self.second], base=self.first)
        self.assertEqual(tree_contents(self.destination), self.full_tree)

    def test_history_cannot_be_marked_complete_if_preapply_state_write_fails(self):
        self.restore([self.full])
        original_state = self.state()
        with patch.object(tb, 'write_restore_marker', side_effect=OSError(errno.ENOSPC, 'No disk space')):
            with self.assertRaises(OSError):
                self.restore([self.first])
        self.assertEqual(tree_contents(self.destination), self.full_tree)
        self.assertEqual(self.state(), original_state)

    def test_same_destination_is_locked_even_when_using_different_media(self):
        self.restore([self.full])
        with tb.restore_lock(self.destination), self.assertRaisesRegex(tb.BackupError, 'Another operation'):
            tb.restore([self.first], self.destination, tb.FileMedia(self.root / 'other-tapes'), quiet=True)
        self.assertEqual(tree_contents(self.destination), self.full_tree)

    def test_competing_process_with_another_home_cannot_change_files_or_history(self):
        self.restore([self.full])
        other_media = self.root / 'other-tapes'
        shutil.copytree(self.media.directory, other_media)
        before = tb.restore_marker_path(self.destination).read_bytes()
        # Independent process, HOME, and media: only the destination lock is shared.
        with tb.restore_lock(self.destination):
            result = subprocess.run([sys.executable, str(Path(tb.__file__).resolve()),
                'restore', '--backup', self.first, '--destination', str(self.destination),
                '--media-dir', str(other_media), '--quiet'], capture_output=True, text=True,
                env={**os.environ, 'HOME': str(self.root / 'other-home')}, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Another operation is restoring', result.stderr)
        self.assertEqual(tree_contents(self.destination), self.full_tree)
        self.assertEqual(tb.restore_marker_path(self.destination).read_bytes(), before)
        self.restore([self.first])  # The lock releases normally.

    def test_changed_history_during_verification_is_rejected_before_applying(self):
        self.restore([self.full])
        frames = tb.StreamReader.frames
        def change_history(reader, **kwargs):
            yield from frames(reader, **kwargs)
            tb.write_restore_marker(self.destination, reader.job)
        with patch.object(tb.StreamReader, 'frames', change_history), \
                patch.object(tb, 'extract_restore_stream', side_effect=AssertionError('Applied stale baseline')):
            with self.assertRaisesRegex(tb.BackupError, 'history changed'):
                self.restore([self.first])
        self.assertEqual(tree_contents(self.destination), self.full_tree)

    def test_changed_destination_during_verification_is_not_adopted_or_modified(self):
        self.restore([self.full])
        frames = tb.StreamReader.frames
        changed = False
        def replace_after_scan(reader, **kwargs):
            nonlocal changed
            yield from frames(reader, **kwargs)
            if not changed:
                changed = True
                self.destination.rename(self.root / 'original')
                self.destination.mkdir()
                (self.destination / 'valuable').write_text('keep')
        with patch.object(tb.StreamReader, 'frames', replace_after_scan):
            with self.assertRaisesRegex(tb.BackupError, 'destination changed'):
                self.restore([self.first])
        self.assertEqual(list(self.destination.iterdir()), [self.destination / 'valuable'])
        self.assertEqual(tree_contents(self.root / 'original'), self.full_tree)


if __name__ == '__main__':
    unittest.main()
