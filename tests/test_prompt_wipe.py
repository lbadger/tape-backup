"""In-process short erase without losing a streaming backup or its prior tapes."""
from contextlib import contextmanager, redirect_stderr
import errno
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_append import Cartridge, TapeMedia, TapeVolume
from test_tape_backup import tree_contents


@contextmanager
def terminal(responses, before_read=None):
    class Input(io.StringIO):
        def readline(self, *args):
            if before_read:
                before_read(self.tell())
            return super().readline(*args)

        def close(self):
            pass

    class Output(io.StringIO):
        def close(self):
            pass

    incoming, outgoing = Input(responses), Output()
    def tty_open(path, mode, *args, **kwargs):
        if path == '/dev/tty':
            return incoming if mode == 'r' else outgoing
        return open(path, mode, *args, **kwargs)
    with patch.object(tb, 'open', tty_open, create=True):
        yield outgoing


class PromptWipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = TapeMedia()
        self.media.media_command = None
        self.media.active = self.used_tape()
        self.original = list(self.media.active.records)
        self.job = 'a' * 32
        self.controls, self.volumes = [], []
        control, raw_open = TapeVolume.control, self.media.raw_open
        def track_control(volume, operation, count=1):
            self.controls.append((operation, count))
            if operation == tb.MTERASE:
                self.assertEqual(volume.position(), 0)
            return control(volume, operation, count)
        def track_open(writing):
            volume = raw_open(writing)
            self.volumes.append(volume)
            return volume
        self.media.raw_open = track_open
        for context in (patch.object(TapeVolume, 'control', track_control),
                        patch.object(tb.os, 'sync'), redirect_stderr(io.StringIO())):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def used_tape(self, backup_id=None, *, zfs=False):
        tape = Cartridge()
        tape.records = [tb.encoded_header({'type': 'volume', 'format': 4 if zfs else 3,
                        'backup': {'id': backup_id or 'b' * 32}, 'volume': 1}),
                        b'old data'.ljust(tb.BLOCK_SIZE, b'\0'), None]
        return tape

    def request(self, responses, action='blank', before_read=None):
        with terminal(responses, before_read) as output:
            tb.TapeMedia.request(self.media, self.job, 2, action)
        self.assertTrue(all(v.stream.closed for v in self.volumes))
        return output.getvalue()

    def test_short_wipe_keeps_drive_lock_and_checks_same_open_descriptor(self):
        number = os.makedev(9, 128)
        def check_lock(_):
            with self.assertRaises(tb.DriveBusy), tb.drive_lock(number):
                pass
        with patch.object(tb, 'DRIVE_LOCK_DIRECTORY', self.root), tb.drive_lock(number):
            output = self.request('wipe\nWIPE\n', before_read=check_lock)
        self.assertEqual(self.media.active.records, [])
        self.assertEqual(self.media.active.cursor, 0)
        self.assertEqual([c for c in self.controls if c[0] == tb.MTERASE], [(tb.MTERASE, 0)])
        self.assertEqual(len(self.volumes), 1)
        self.assertIn('Type WIPE', output)
        self.assertIn('Blank tape verified', output)

    def test_enter_never_erases_and_nonexact_confirmation_returns_to_prompt(self):
        for response in ('\n', 'wipe\n\n\n', 'wipe\nyes\n\n', 'wipe\nq\n\n',
                         'wipe\nwipe\n\n', 'typo\n\n'):
            with self.subTest(response=response):
                output = self.request(response)
                self.assertEqual(self.media.active.records, self.original)
                self.assertFalse(any(c[0] == tb.MTERASE for c in self.controls))
                if response.startswith('wipe'):
                    self.assertIn('Wipe cancelled', output)
                    self.assertEqual(output.count('Press Enter'), 2)

    def test_end_of_input_cancels_without_erasing(self):
        with self.assertRaisesRegex(tb.BackupError, 'Media change cancelled'):
            self.request('wipe\n')
        self.assertEqual(self.media.active.records, self.original)
        self.assertFalse(any(c[0] == tb.MTERASE for c in self.controls))

    def test_read_and_append_prompts_do_not_offer_or_accept_wipe(self):
        for action in ('read', 'append'):
            output = self.request('wipe\n\n', action)
            self.assertNotIn('Type WIPE', output)
            self.assertNotIn('short-erase', output)
            self.assertIn('Unrecognized response', output)
        self.assertEqual(self.media.active.records, self.original)
        self.assertEqual(self.controls, [])

    def test_active_tar_or_zfs_volume_is_refused_without_erase_confirmation(self):
        for zfs in (False, True):
            self.media.active = self.used_tape(self.job, zfs=zfs)
            before = list(self.media.active.records)
            output = self.request('wipe\n\n')
            self.assertIn('contains a volume of the active backup', output)
            self.assertNotIn('Type WIPE', output)
            self.assertEqual(self.media.active.records, before)
        self.assertFalse(any(c[0] == tb.MTERASE for c in self.controls))

    def test_previously_used_cartridge_is_protected_by_first_header(self):
        self.media.protected_headers = {hashlib.sha256(self.original[0]).hexdigest()}
        output = self.request('wipe\n\n')
        self.assertIn('already part of the active backup or its append base', output)
        self.assertNotIn('Type WIPE', output)
        self.assertEqual(self.media.active.records, self.original)

    def test_append_protection_reuses_header_without_extra_tape_traversal(self):
        volume = TapeVolume(self.media.active)
        volume.next_record()
        volume.seek_end()
        self.media.protected_headers = set()
        with patch.object(volume, 'seek_position', side_effect=AssertionError('Unnecessary tape seek')):
            self.media.protect_cartridge(volume)
        self.assertEqual(self.media.protected_headers, {hashlib.sha256(self.original[0]).hexdigest()})
        volume.close()

    def test_an_earlier_base_volume_is_protected_even_if_not_loaded_for_append(self):
        self.media.protected_backup_ids = {self.job, 'b' * 32}
        output = self.request('wipe\n\n')
        self.assertIn('active backup or its base', output)
        self.assertNotIn('Type WIPE', output)
        self.assertEqual(self.media.active.records, self.original)

    def test_confirmation_pauses_progress_until_answered(self):
        entered, release = threading.Event(), threading.Event()
        failures = []
        def wait_for_confirmation(position):
            if position == len('wipe\n'):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('Confirmation was not released')
        def request():
            try:
                self.request('wipe\nWIPE\n', before_read=wait_for_confirmation)
            except BaseException as exc:
                failures.append(exc)
        output = io.StringIO()
        with redirect_stderr(output):
            worker = threading.Thread(target=request)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                progress = tb.Progress('backup', archives=1)
                progress.track_archive(100)
                progress.report()
                self.assertEqual(output.getvalue(), '')
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            progress.report()
            self.assertIn('Total', output.getvalue())

    def test_changed_cartridge_during_confirmation_is_not_erased(self):
        other = self.used_tape('c' * 32)
        def swap(position):
            if position == len('wipe\n'):
                self.media.active.records[:] = other.records
                self.media.active.cursor = 0
        output = self.request('wipe\nWIPE\n\n', before_read=swap)
        self.assertIn('Cartridge changed during wipe confirmation', output)
        self.assertEqual(self.media.active.records, other.records)
        self.assertFalse(any(c[0] == tb.MTERASE for c in self.controls))

    def test_unreadable_or_corrupt_header_returns_to_prompt_without_erasing(self):
        with patch.object(TapeVolume, 'next_record', side_effect=OSError(errno.EIO, 'Unreadable')):
            output = self.request('wipe\n\n')
        self.assertIn('Unreadable', output)
        self.media.active.records[0] = self.original[0].replace(b'volume', b'broken', 1)
        output = self.request('wipe\n\n')
        self.assertIn('Corrupt tape header', output)
        for fields in ({'backup': {'id': []}, 'volume': 1},
                       {'backup': {'id': 'b' * 32}, 'volume': -1}):
            self.media.active.records[0] = tb.encoded_header({'type': 'volume', **fields})
            output = self.request('wipe\n\n')
            self.assertIn('Wipe refused or failed', output)
            self.assertNotIn('Type WIPE', output)
        self.assertFalse(any(c[0] == tb.MTERASE for c in self.controls))

    def test_failed_short_erase_or_blank_check_preserves_waiting_backup(self):
        control = TapeVolume.control
        for failure in ('write-protected', 'unsupported', 'still-recorded'):
            def fail(volume, operation, count=1):
                if operation == tb.MTERASE:
                    self.assertEqual(count, 0)
                    if failure == 'still-recorded':
                        return
                    code = errno.EACCES if failure == 'write-protected' else errno.EINVAL
                    raise OSError(code, failure)
                return control(volume, operation, count)
            with self.subTest(failure=failure), patch.object(TapeVolume, 'control', fail):
                output = self.request('wipe\nWIPE\n\n')
            self.assertIn('Backup remains active', output)
            self.assertNotIn('Blank tape verified', output)
            self.assertEqual(self.media.active.records, self.original)

    def interactive_recycling(self, media):
        """Try a protected cartridge once, then recycle an unrelated used tape."""
        request, attempts, protected = media.request, {}, []
        def load(backup_id, number, action):
            if action != 'blank' or not media.tapes:
                return request(backup_id, number, action)
            attempts[number] = attempts.get(number, 0) + 1
            if attempts[number] == 1:
                media.active = media.tapes[0]
                before = list(media.active.records)
                with terminal('wipe\n\n') as output:
                    tb.TapeMedia.request(media, backup_id, number, action)
                self.assertNotIn('Type WIPE', output.getvalue())
                self.assertIn('cannot be wiped here', output.getvalue())
                self.assertEqual(media.active.records, before)
                protected.append((media.active, before))
            else:
                media.active = self.used_tape()
                media.active.capacity = media.capacity
                media.tapes.append(media.active)
                with terminal('wipe\nWIPE\n') as output:
                    tb.TapeMedia.request(media, backup_id, number, action)
                self.assertIn('Blank tape verified', output.getvalue())
        media.request, media.media_command = load, None
        return protected

    def test_multivolume_backup_wipes_spares_and_restores_without_restarting_source(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'book').write_bytes(os.urandom(1_400_000))
        media = TapeMedia(capacity=12)
        protected = self.interactive_recycling(media)
        with patch.object(tb, 'start_archive', wraps=tb.start_archive) as archive:
            backup_id = tb.backup(source, media, buffer_size=256 * 1024, quiet=True)
        self.assertEqual(archive.call_count, 1)
        self.assertGreaterEqual(len(media.tapes), 3)
        for tape, before in protected:
            self.assertEqual(tape.records, before)
        media.loaded = False
        self.assertTrue(tb.scan(media, backup_id)['data_verified'])
        destination = self.root / 'restored'
        tb.restore([backup_id], destination, media, quiet=True)
        self.assertEqual(tree_contents(destination), tree_contents(source))

    def test_incremental_protects_base_before_and_after_appending(self):
        for append_first in (False, True):
            with self.subTest(append_first=append_first):
                source = self.root / f'source-{append_first}'
                source.mkdir()
                (source / 'book').write_bytes(os.urandom(300_000))
                media = TapeMedia()
                full = tb.backup(source, media, buffer_size=256 * 1024, quiet=True)
                before = list(media.active.records)
                (source / 'new').write_bytes(os.urandom(1_000_000))
                protected = self.interactive_recycling(media)
                limit = (len(before) + (12 if append_first else 0)) * tb.BLOCK_SIZE
                delta = tb.backup(source, media, level='incremental', base=full,
                                  volume_size=limit, buffer_size=256 * 1024, quiet=True)
                self.assertTrue(protected)
                self.assertEqual(media.tapes[0].records[:len(before)], before)
                media.loaded = False
                destination = self.root / f'restored-{append_first}'
                tb.restore([full, delta], destination, media, quiet=True)
                self.assertEqual(tree_contents(destination), tree_contents(source))


if __name__ == '__main__':
    unittest.main()
