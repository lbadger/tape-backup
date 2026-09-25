"""Automatic and explicit unloads retain the operation and its drive lock."""
from contextlib import redirect_stderr
import errno
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import tape_backup as tb
from test_append import TapeMedia
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

    def test_multivolume_backup_auto_ejects_and_restore_can_eject_without_losing_data(self):
        self.round_trip()

    def test_failed_auto_eject_can_be_retried_at_the_prompt_without_losing_data(self):
        for failure in (tb.BackupError('Drive refuses unload'), OSError(errno.EIO, 'Unload failed')):
            with self.subTest(failure=failure):
                self.round_trip(failure)

    def test_auto_eject_precedes_loader_and_failure_still_allows_loading(self):
        for failure in (None, tb.BackupError('Drive refuses unload')):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, \
                    redirect_stderr(io.StringIO()), patch.object(tb.os, 'sync'):
                source = Path(directory) / 'source'
                source.mkdir()
                (source / 'book').write_bytes(os.urandom(1_000_000))
                media = TapeMedia(capacity=12)
                media.media_command = '/loader'
                media.eject = Mock(wraps=media.eject, side_effect=failure)
                request, run_command = media.request, tb.run_command
                media.request = tb.TapeMedia.request.__get__(media)
                def command(args):
                    if args[0] != '/loader':
                        return run_command(args)
                    action, backup_id, number, device = args[1:]
                    self.assertEqual(device, media.device)
                    self.assertEqual(action, 'blank')
                    self.assertEqual(media.eject.call_count, int(number) - 1)
                    if failure and int(number) > 1:
                        self.assertIsNotNone(media.active)
                    else:
                        self.assertIsNone(media.active)
                    request(backup_id, int(number), action)
                with patch.object(tb, 'run_command', command):
                    tb.backup(source, media, buffer_size=256 * 1024, quiet=True)
                self.assertGreaterEqual(len(media.tapes), 3)
                self.assertEqual(media.eject.call_count, len(media.tapes) - 1)
                self.assertIs(media.active, media.tapes[-1])

    def round_trip(self, failure=None):
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(errors), \
                patch.object(tb.os, 'sync'):
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'book').write_bytes(os.urandom(1_000_000))
            media = TapeMedia(capacity=12)
            media.media_command = None
            media.device_number = os.makedev(9, 128)
            media.lock = tb.TapeMedia.lock.__get__(media)
            request, command, raw_open = media.request, media.mt, media.raw_open
            streams, ejections, failed = [], [], set()
            def open_volume(writing):
                volume = raw_open(writing)
                streams.append(volume.stream)
                return volume
            def mt(*args):
                if args in (('unlock',), ('offline',)):
                    self.assertTrue(all(stream.closed for stream in streams))
                    with self.assertRaises(tb.DriveBusy), media.lock():
                        pass
                    if args == ('offline',):
                        if failure and media.active not in failed:
                            failed.add(media.active)
                            raise failure
                        ejections.append(media.active)
                return command(*args)
            def load(backup_id, number, action):
                if number <= 1:
                    return request(backup_id, number, action)
                response = 'eject\n\n' if action == 'read' or failure else '\n'
                if action == 'blank' and not failure:
                    self.assertIsNone(media.active)
                    self.assertFalse(media.loaded)
                def insert(position):
                    if position == len(response) - 1:
                        self.assertIsNone(media.active)
                        request(backup_id, number, action)
                with terminal(response, insert):
                    tb.TapeMedia.request(media, backup_id, number, action)
            media.mt, media.request, media.raw_open = mt, load, open_volume
            with patch.object(tb, 'DRIVE_LOCK_DIRECTORY', Path(directory)), \
                    patch.object(tb, 'start_archive', wraps=tb.start_archive) as archive:
                backup_id = tb.backup(source, media, buffer_size=256 * 1024, quiet=True)
                self.assertEqual(archive.call_count, 1)
                self.assertGreaterEqual(len(media.tapes), 3)
                self.assertEqual(ejections, media.tapes[:-1])
                self.assertIs(media.active, media.tapes[-1])
                self.assertTrue(media.loaded)
                self.assertEqual(errors.getvalue().count('Automatic eject failed'),
                                 len(media.tapes) - 1 if failure else 0)
                before = [list(tape.records) for tape in media.tapes]
                media.loaded = False
                restored = Path(directory) / 'restored'
                tb.restore([backup_id], restored, media, quiet=True)
            self.assertEqual(tree_contents(source), tree_contents(restored))
            self.assertEqual(before, [tape.records for tape in media.tapes])
            self.assertEqual(len(ejections), 2 * (len(media.tapes) - 1))


if __name__ == '__main__':
    unittest.main()
