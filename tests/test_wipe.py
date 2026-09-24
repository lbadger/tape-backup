"""Explicit tape initialization, confirmation, locking, and continuation reuse."""
from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import os
from pathlib import Path
import pty
import tempfile
import unittest
from unittest.mock import Mock, patch

import tape_backup as tb
from test_append import Cartridge, TapeMedia, TapeVolume
from test_tape_backup import tree_contents


class WipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tape = Cartridge()
        self.tape.records = [b'old backup'.ljust(tb.BLOCK_SIZE, b'\0'), None]
        self.tape.cursor = len(self.tape.records)
        self.original = list(self.tape.records)
        self.media = object.__new__(tb.TapeMedia)
        self.media.device = '/dev/nst1'
        self.media.device_number = os.makedev(9, 129)
        self.commands, self.controls, self.volumes = [], [], []
        owner = self
        class TrackedVolume(TapeVolume):
            def control(self, operation, count=1):
                owner.controls.append((operation, count))
                if operation == tb.MTERASE:
                    owner.assertEqual(self.stream.tape.cursor, 0, 'Erase must begin at BOT')
                return super().control(operation, count)
        def raw_open(writing):
            self.assertTrue(writing)
            volume = TrackedVolume(self.tape)
            self.volumes.append(volume)
            return volume
        self.media.raw_open = Mock(side_effect=raw_open)
        run_command = tb.run_command
        def command(args):
            if args[0] != 'mt':
                return run_command(args)
            self.commands.append(args)
            self.assertEqual(args[:3], ['mt', '-f', self.media.device])
            if args[3:] == ['rewind']:
                self.tape.cursor = 0
            elif args[3:] not in (['stclearoptions', '0xa002'],
                                  ['stsetoptions', 'scsi2logical'], ['setblk', '0']):
                raise AssertionError('Unexpected mt operation: ' + repr(args))
            return ''
        for context in (patch.object(tb, 'run_command', side_effect=command),
                        patch.object(Path, 'home', return_value=self.root),
                        patch.object(Path, 'read_text', return_value='0xa002')):
            context.start()
            self.addCleanup(context.stop)

    def run_wipe(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(tb, 'TapeMedia', return_value=self.media) as factory, \
                redirect_stdout(output), redirect_stderr(errors):
            code = tb.main(['wipe', '--device', self.media.device, *args])
        factory.assert_called_once_with(self.media.device)
        self.output, self.errors = output.getvalue(), errors.getvalue()
        return code

    def test_short_erase_is_synchronous_verified_rewound_and_left_loaded(self):
        self.assertEqual(self.run_wipe('--yes'), 0, self.errors)
        self.assertIn((tb.MTERASE, 0), self.controls)
        self.assertNotIn((tb.MTERASE, 1), self.controls)
        self.assertEqual(self.tape.records, [])
        self.assertEqual(self.tape.cursor, 0)
        self.assertEqual(self.commands, [
            ['mt', '-f', '/dev/nst1', 'stclearoptions', '0xa002'],
            ['mt', '-f', '/dev/nst1', 'stsetoptions', 'scsi2logical'],
            ['mt', '-f', '/dev/nst1', 'rewind'],
            ['mt', '-f', '/dev/nst1', 'setblk', '0']])
        self.assertTrue(all(v.stream.closed for v in self.volumes))
        self.assertIn('blank verified, rewound, and left loaded', self.output)
        volume = TapeVolume(self.tape)
        volume.require_blank()  # Exactly the check used for continuation media.
        volume.close()

    def test_long_erase_requires_explicit_option(self):
        self.assertEqual(self.run_wipe('--long', '--yes'), 0, self.errors)
        self.assertEqual([c for c in self.controls if c[0] == tb.MTERASE], [(tb.MTERASE, 1)])
        self.assertEqual(self.tape.records, [])

    def test_terminal_confirmation_accepts_only_explicit_wipe(self):
        for response in (b'\n', b'q\n', b'yes\n', b'\x04', b'WIPE\n'):
            with self.subTest(response=response):
                master, slave = pty.openpty()
                try:
                    terminal = os.ttyname(slave)
                    def terminal_open(path, *args, **kwargs):
                        if str(path) == '/dev/tty':
                            return open(terminal, *args, **kwargs,
                                        opener=lambda p, flags: os.open(p, flags | os.O_NOCTTY))
                        return open(path, *args, **kwargs)
                    os.write(master, response)
                    with patch.object(tb, 'open', terminal_open, create=True):
                        result = self.run_wipe()
                    prompt = os.read(master, 4096).decode()
                    self.assertIn('/dev/nst1', prompt)
                    self.assertIn('Type WIPE', prompt)
                    if response == b'WIPE\n':
                        self.assertEqual(result, 0, self.errors)
                        self.assertEqual(self.tape.records, [])
                    else:
                        self.assertEqual(result, 1)
                        self.assertEqual(self.commands, [])
                        self.assertEqual(self.controls, [])
                        self.media.raw_open.assert_not_called()
                        self.assertEqual(self.tape.records, self.original)
                        self.assertEqual(self.output, '')
                finally:
                    os.close(master)
                    os.close(slave)

    def test_missing_terminal_refuses_without_modifying_tape(self):
        def no_terminal(path, *args, **kwargs):
            if str(path) == '/dev/tty':
                raise OSError(errno.ENXIO, 'No terminal')
            return open(path, *args, **kwargs)
        with patch.object(tb, 'open', no_terminal, create=True):
            self.assertEqual(self.run_wipe(), 1)
        self.assertIn('--yes', self.errors)
        self.assertEqual(self.commands, [])
        self.media.raw_open.assert_not_called()
        self.assertEqual(self.tape.records, self.original)

    def test_active_backup_lock_blocks_wipe_before_confirmation_or_drive_commands(self):
        with self.media.lock(), patch.object(self.media, 'wipe') as wipe:
            self.assertEqual(self.run_wipe('--yes'), 1)
        wipe.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.controls, [])
        self.assertEqual(self.tape.records, self.original)
        self.assertIn('Another operation', self.errors)

    def test_write_protection_and_unsupported_erase_do_not_fall_back_to_long_erase(self):
        control = TapeVolume.control
        for code in (errno.EACCES, errno.EINVAL):
            with self.subTest(code=code):
                def fail_erase(volume, operation, count=1):
                    if operation == tb.MTERASE:
                        raise OSError(code, 'Erase refused')
                    return control(volume, operation, count)
                self.controls.clear()
                with patch.object(TapeVolume, 'control', fail_erase):
                    self.assertEqual(self.run_wipe('--yes'), 1)
                self.assertEqual([c for c in self.controls if c[0] == tb.MTERASE], [(tb.MTERASE, 0)])
                self.assertEqual(self.tape.records, self.original)
                self.assertEqual(self.output, '')
                self.assertIn('short erase', self.errors)
                self.assertTrue(all(v.stream.closed for v in self.volumes))
                with self.media.lock():
                    pass

    def test_successful_erase_command_without_blank_media_does_not_report_success(self):
        control = TapeVolume.control
        def ignore_erase(volume, operation, count=1):
            if operation != tb.MTERASE:
                return control(volume, operation, count)
        with patch.object(TapeVolume, 'control', ignore_erase):
            self.assertEqual(self.run_wipe('--yes'), 1)
        self.assertIn('blank verification failed', self.errors)
        self.assertEqual(self.output, '')
        self.assertEqual(self.tape.records, self.original)
        self.assertTrue(all(v.stream.closed for v in self.volumes))

    def test_unknown_position_cannot_certify_initialized_tape(self):
        with patch.object(TapeVolume, 'position', side_effect=OSError(errno.EIO, 'Position unknown')):
            self.assertEqual(self.run_wipe('--yes'), 1)
        self.assertEqual(self.output, '')
        self.assertIn('blank verification failed', self.errors)
        self.assertTrue(all(v.stream.closed for v in self.volumes))

    def test_interrupted_wipe_closes_device_and_does_not_claim_backup_or_wipe_success(self):
        with patch.object(TapeVolume, 'control', side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_wipe('--yes'), 130)
        self.assertEqual(self.output, '')
        self.assertIn('Wipe interrupted', self.errors)
        self.assertNotIn('Start a new full backup', self.errors)
        self.assertTrue(all(v.stream.closed for v in self.volumes))
        with self.media.lock():
            pass

    def test_wiped_backup_cartridge_can_be_reused_for_continuation(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'book').write_bytes(os.urandom(300_000))
        media = TapeMedia()
        with redirect_stderr(io.StringIO()), patch.object(tb.os, 'sync'):
            tb.backup(source, media, quiet=True)
            recycled = media.active
            self.assertGreater(len(recycled.records), 1)
            media.wipe(yes=True)
            self.assertEqual(recycled.records, [])
            media = TapeMedia(capacity=12)
            request = media.request
            def reuse_tape(backup_id, number, action):
                if action == 'blank' and number == 2:
                    media.requests.append((backup_id, number, action))
                    media.active = recycled
                    media.tapes.append(recycled)
                    return
                request(backup_id, number, action)
            media.request = reuse_tape
            (source / 'book').write_bytes(os.urandom(1_000_000))
            full = tb.backup(source, media, buffer_size=256 * 1024, quiet=True)
            self.assertEqual(len(media.tapes), 2)
            media.loaded = False
            self.assertTrue(tb.scan(media, full)['data_verified'])
            destination = self.root / 'restored'
            tb.restore([full], destination, media, quiet=True)
            self.assertEqual(tree_contents(destination), tree_contents(source))


if __name__ == '__main__':
    unittest.main()
