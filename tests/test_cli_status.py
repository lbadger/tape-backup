"""Operator-facing outcomes, help, and non-mutating drive diagnostics."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import struct
import signal
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tape_backup as tb


class CLIStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def backup(self, *extra):
        source = self.root / 'source'
        source.mkdir(exist_ok=True)
        (source / 'book').write_text('content')
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = tb.main(['backup', '--source', str(source), '--media-dir', str(self.root / 'media'),
                            '--buffer-size', '64KiB', '--json', *extra])
        return code, json.loads(output.getvalue())

    def test_json_distinguishes_committed_data_from_verified_data(self):
        code, result = self.backup('--verify')
        self.assertEqual(code, 0)
        self.assertTrue(result['archive_complete'])
        self.assertTrue(result['metadata_complete'])
        self.assertTrue(result['append_ready'])
        self.assertTrue(result['data_verified'])
        self.assertEqual(result['warnings'], [])

    def test_metadata_failure_is_reported_without_mislabeling_the_committed_archive(self):
        with patch.object(tb, 'write_metadata', side_effect=OSError(errno.EIO, 'Injected footer failure')):
            code, result = self.backup()
        self.assertEqual(code, 0)
        self.assertTrue(result['archive_complete'])
        self.assertFalse(result['metadata_complete'])
        self.assertIsNone(result['append_ready'])
        self.assertFalse(result['data_verified'])
        self.assertIn('Injected footer failure', result['warnings'][0])
        with redirect_stderr(io.StringIO()):
            self.assertTrue(tb.scan(tb.FileMedia(self.root / 'media'), result['id'])['data_verified'])

    def test_missing_catalog_due_to_test_capacity_is_distinct_from_metadata_failure(self):
        with patch.object(tb, 'write_metadata', return_value=False):
            code, result = self.backup()
        self.assertEqual(code, 0)
        self.assertFalse(result['metadata_complete'])
        self.assertTrue(result['append_ready'])
        self.assertIn('did not fit', result['warnings'][0])

    def test_empty_command_displays_workflows_without_accessing_tape(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(tb, 'TapeMedia', side_effect=AssertionError('Device access')):
            self.assertEqual(tb.main([]), 0)
        for name in ('File backup', 'Native ZFS', 'wipe (destructive)', 'COMMAND --help', 'blank'):
            self.assertIn(name, output.getvalue())

    def test_busy_status_uses_only_passive_statistics(self):
        @contextmanager
        def busy():
            raise tb.DriveBusy('Another operation')
            yield
        media = SimpleNamespace(device='/dev/nst0', lock=busy)
        with patch.object(tb.os, 'open', side_effect=AssertionError('Opened busy device')):
            result = tb.device_status(media)
        self.assertTrue(result['busy'])
        self.assertIsNone(result['state'])
        self.assertIn('passive', result['warnings'][0])

    def test_status_opens_readonly_nonblocking_and_sends_no_movement_commands(self):
        device = self.root / 'device'
        device.touch()
        @contextmanager
        def unlocked():
            yield
        media = SimpleNamespace(device=str(device), lock=unlocked)
        opened = []
        actual_open = os.open
        def open_device(path, flags):
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
            self.assertTrue(flags & os.O_NONBLOCK)
            fd = actual_open(path, flags)
            opened.append(fd)
            return fd
        def ioctl(fd, operation, data, mutate):
            self.assertEqual(operation & 0xffff, 0x6d02)  # MTIOCGET only.
            data[:] = struct.pack('@5l2i', 0, 0, 0, 0x45000000, 0, 2, 4)
        with patch.object(tb.os, 'open', open_device), patch.object(tb.fcntl, 'ioctl', ioctl), \
                patch.object(tb, 'compression_status', return_value={'enabled': True, 'supported': True}), \
                patch.object(tb, 'tape_position', return_value=(5, 5)):
            result = tb.device_status(media)
        self.assertTrue(result['state']['online'])
        self.assertTrue(result['state']['write_protected'])
        self.assertTrue(result['state']['beginning_of_tape'])
        self.assertEqual(result['position'], (5, 5))
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_sigterm_stops_source_and_reports_an_incomplete_backup(self):
        tools = self.root / 'tools'
        tools.mkdir()
        tar = tools / 'tar'
        tar.write_text('#!/bin/sh\nif [ "$1" = "--create" ]; then exec /bin/sleep 30; fi\n'
                       'exec ' + shlex.quote(shutil.which('tar')) + ' "$@"\n')
        tar.chmod(0o700)
        source = self.root / 'source'
        source.mkdir()
        (source / 'book').write_text('content')
        env = {**os.environ, 'PATH': str(tools)}
        media_path = self.root / 'media'
        process = subprocess.Popen([sys.executable, str(Path(tb.__file__).resolve()), 'backup',
            '--source', str(source), '--media-dir', str(media_path)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not list(media_path.glob('*.tape')):
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail('Backup did not initialize its first volume')
                time.sleep(0.01)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, errors)
            self.assertEqual(output, '')
            self.assertIn('Interrupted', errors)
            backup_id = next(media_path.glob('*.tape')).name.split('.')[0]
            with redirect_stderr(io.StringIO()), self.assertRaises(tb.BackupError):
                tb.scan(tb.FileMedia(media_path), backup_id)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == '__main__':
    unittest.main()
