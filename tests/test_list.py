"""List real GNU tar archives across framed, appended, and multi-tape backups."""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import tape_backup as tb
from test_append import TapeMedia
from test_tape_backup import tree_contents


class ListTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'book.m4b').write_bytes(os.urandom(300_000))
        (self.source / 'old.txt').write_text('original')
        (self.source / 'space and\nnewline').write_text('odd name')
        (self.source / 'link').symlink_to('book.m4b')
        (self.source / 'empty').mkdir()
        self.media = tb.FileMedia(self.root / 'media')
        self.errors = io.StringIO()
        context = redirect_stderr(self.errors)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def backup(self, media=None, base=None, limit=None):
        return tb.backup(self.source, media or self.media, quiet=True,
                         buffer_size=tb.BLOCK_SIZE, volume_size=limit,
                         level='incremental' if base else 'full', base=base)

    def listing(self, media=None, backup_id=None):
        output = io.StringIO()
        with redirect_stdout(output):
            summary = tb.list_files(media or self.media, backup_id)
        self.assertTrue(summary['data_verified'])
        return output.getvalue().splitlines(), summary

    def test_full_discovery_lists_names_without_extraction_or_media_changes(self):
        full = self.backup()
        before = tree_contents(self.root)
        names, summary = self.listing()
        self.assertEqual(summary['id'], full)
        self.assertEqual(set(names), {'./', './empty/', './book.m4b', './old.txt',
                                     './link', './space and\\nnewline'})
        self.assertEqual(tree_contents(self.root), before)
        self.assertIn('complete', self.errors.getvalue())

    def test_selects_appended_incremental_and_lists_only_archived_changes(self):
        full = self.backup()
        (self.source / 'old.txt').unlink()
        (self.source / 'new.txt').write_text('new')
        delta = self.backup(base=full)
        names, summary = self.listing(backup_id=delta)
        self.assertEqual(summary['id'], delta)
        self.assertIn('./new.txt', names)
        self.assertNotIn('./book.m4b', names)
        self.assertNotIn('./old.txt', names)
        names, _ = self.listing(backup_id=full)
        self.assertIn('./old.txt', names)
        self.assertNotIn('./new.txt', names)

    def test_physical_volume_changes_preserve_records_and_print_names_once(self):
        media = TapeMedia()
        full = self.backup(media, limit=8 * tb.BLOCK_SIZE)
        self.assertGreater(len(media.tapes), 1)
        before = [list(t.records) for t in media.tapes]
        media.requests.clear()
        media.loaded = False
        names, summary = self.listing(media, full)
        self.assertEqual(names.count('./book.m4b'), 1)
        self.assertEqual(summary['volumes'], len(media.tapes))
        self.assertEqual(media.requests, [(full, n, 'read')
                                         for n in range(1, len(media.tapes) + 1)])
        self.assertEqual([t.records for t in media.tapes], before)

    def test_missing_continuation_fails_instead_of_reporting_complete(self):
        full = self.backup(limit=8 * tb.BLOCK_SIZE)
        tapes = sorted(self.media.directory.glob('*.tape'))
        self.assertGreater(len(tapes), 1)
        tapes[-1].unlink()
        with redirect_stdout(io.StringIO()), self.assertRaises(tb.BackupError):
            tb.list_files(self.media, full)

    def test_valid_tar_with_corrupt_completion_is_not_a_successful_listing(self):
        full = self.backup()
        tape = next(self.media.directory.glob('*.tape'))
        data = bytearray(tape.read_bytes())
        for offset in range(0, len(data), tb.BLOCK_SIZE):
            record = data[offset:offset + tb.BLOCK_SIZE]
            if record.startswith(tb.MAGIC):
                head = tb.decoded_header(record)
                if head.get('kind') == 'end':
                    data[offset + tb.BLOCK_SIZE] ^= 1
                    break
        else:
            self.fail('No completion marker')
        tape.write_bytes(data)
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(tb.BackupError, 'Checksum mismatch'):
            tb.list_files(self.media, full)

    def test_cli_honors_media_lock_and_keeps_stdout_for_file_names(self):
        full = self.backup()
        args = ['list', '--media-dir', str(self.media.directory), '--backup', full]
        output = io.StringIO()
        with redirect_stdout(output):
            with self.media.lock():
                self.assertEqual(tb.main(args), 1)
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(tb.main(args), 0)
        self.assertIn('./book.m4b', output.getvalue())
        self.assertNotIn(full, output.getvalue())
        self.assertNotIn('MiB/s', output.getvalue())

    def test_list_stdout_and_stderr_wait_for_media_prompt_lock(self):
        output, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            process = None
            try:
                with tb.OUTPUT_LOCK:
                    process = tb.logged_process([sys.executable, '-c',
                        "import sys; print('./book.m4b'); print('diagnostic', file=sys.stderr)"],
                        result_stdout=True, stdin=subprocess.DEVNULL)
                    self.assertEqual(process.wait(timeout=3), 0)
                    self.assertEqual(output.getvalue(), '')
                    self.assertEqual(diagnostics.getvalue(), '')
            finally:
                tb.stop_process(process)
        self.assertEqual(output.getvalue(), './book.m4b\n')
        self.assertEqual(diagnostics.getvalue(), 'diagnostic\n')


if __name__ == '__main__':
    unittest.main()
