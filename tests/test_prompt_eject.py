"""Explicit unload at media prompts retains the operation and its drive lock."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import tape_backup as tb
from test_append import Cartridge, TapeMedia
from test_prompt_wipe import terminal
from test_tape_backup import tree_contents


class PromptEjectTests(unittest.TestCase):
    def test_eject_unlocks_then_unloads_under_the_same_drive_lock(self):
        media = object.__new__(tb.TapeMedia)
        media.device, media.media_command = '/dev/nst0', None
        media.device_number, media.loaded = os.makedev(9, 128), True
        calls = []
        def command(args):
            with self.assertRaises(tb.DriveBusy), media.lock():
                pass
            calls.append(args)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tb, 'DRIVE_LOCK_DIRECTORY', Path(directory)), \
                patch.object(tb, 'run_command', command), media.lock(), \
                terminal('eject\n\n') as output:
            media.request('a' * 32, 2, 'blank')
        self.assertEqual(calls, [['mt', '-f', media.device, 'unlock'],
                                 ['mt', '-f', media.device, 'offline']])
        self.assertFalse(media.loaded)
        self.assertEqual(output.getvalue().count('volume 2 into'), 2)

    def test_read_and_append_prompts_accept_eject_and_failures_remain_at_prompt(self):
        for action in ('read', 'append', 'blank'):
            for failure in (None, tb.BackupError('Drive refuses unload')):
                media = object.__new__(tb.TapeMedia)
                media.device, media.media_command = '/dev/nst0', None
                media.eject = Mock(side_effect=failure)
                with self.subTest(action=action, failure=failure), terminal('eject\n\n') as output:
                    media.request('a' * 32, 2, action)
                media.eject.assert_called_once_with()
                self.assertEqual(output.getvalue().count('Press Enter'), 2)
                self.assertIn('Eject failed' if failure else 'Tape ejected', output.getvalue())

    def test_multivolume_backup_and_restore_can_eject_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(io.StringIO()), \
                patch.object(tb.os, 'sync'):
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'book').write_bytes(os.urandom(1_000_000))
            media = TapeMedia(capacity=12)
            media.media_command = None
            request, command, raw_open = media.request, media.mt, media.raw_open
            streams, ejections = [], []
            def open_volume(writing):
                volume = raw_open(writing)
                streams.append(volume.stream)
                return volume
            def mt(*args):
                if args in (('unlock',), ('offline',)):
                    self.assertTrue(all(stream.closed for stream in streams))
                    if args == ('offline',):
                        ejections.append(media.active)
                        media.active = None
                    return
                return command(*args)
            def load(backup_id, number, action):
                if number <= 1:
                    return request(backup_id, number, action)
                def insert(position):
                    if position == len('eject\n'):
                        self.assertIsNone(media.active)
                        request(backup_id, number, action)
                with terminal('eject\n\n', insert):
                    tb.TapeMedia.request(media, backup_id, number, action)
            media.mt, media.request, media.raw_open = mt, load, open_volume
            with patch.object(tb, 'start_archive', wraps=tb.start_archive) as archive:
                backup_id = tb.backup(source, media, buffer_size=256 * 1024, quiet=True)
            self.assertEqual(archive.call_count, 1)
            before = [list(tape.records) for tape in media.tapes]
            media.loaded = False
            restored = Path(directory) / 'restored'
            tb.restore([backup_id], restored, media, quiet=True)
            self.assertEqual(tree_contents(source), tree_contents(restored))
            self.assertEqual(before, [tape.records for tape in media.tapes])
            self.assertEqual(len(ejections), 2 * (len(media.tapes) - 1))


if __name__ == '__main__':
    unittest.main()
