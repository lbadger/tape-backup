"""Tape prompts stay visible while progress, tar, and SSH output are pending."""
from contextlib import redirect_stderr
import io
import os
import pty
import select
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import tape_backup as tb


class PromptLoggingTests(unittest.TestCase):
    def test_progress_and_child_output_pause_until_enter_or_cancel(self):
        for response in (b'\n', b'q\n'):
            with self.subTest(response=response):
                master, slave = pty.openpty()
                terminal = os.ttyname(slave)
                media = object.__new__(tb.TapeMedia)
                media.device, media.media_command = '/dev/nst0', None
                output, failures = io.StringIO(), []
                def terminal_open(path, *args, **kwargs):
                    self.assertEqual(path, '/dev/tty')
                    return open(terminal, *args, **kwargs,
                                opener=lambda p, flags: os.open(p, flags | os.O_NOCTTY))
                def prompt():
                    try:
                        media.request('a' * 32, 2, 'blank')
                    except BaseException as exc:
                        failures.append(exc)
                waiter, process = None, None
                try:
                    with redirect_stderr(output), patch.object(tb, 'open', terminal_open, create=True):
                        process = tb.logged_process([sys.executable, '-c',
                            "import sys; sys.stdin.buffer.read(1); "
                            "sys.stderr.write('./remote-book.m4b\\n'); sys.stderr.flush(); "
                            "sys.stdout.write('done'); sys.stdout.flush()"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                        waiter = threading.Thread(target=prompt)
                        waiter.start()
                        self.assertTrue(select.select([master], [], [], 3)[0], 'Missing tape-change prompt')
                        prompt_text = os.read(master, 4096).decode()
                        self.assertIn('Press Enter', prompt_text)
                        process.stdin.write(b'g')
                        process.stdin.flush()
                        self.assertTrue(select.select([process.stdout], [], [], 3)[0])
                        self.assertEqual(process.stdout.read(4), b'done')
                        progress = tb.Progress('backup')
                        progress.report()
                        self.assertEqual(output.getvalue(), '', 'Logging disturbed the waiting prompt')
                        os.write(master, response)
                        waiter.join(3)
                        self.assertFalse(waiter.is_alive())
                        tb.stop_process(process)
                        self.assertIn('./remote-book.m4b', output.getvalue())
                        progress.report()
                        self.assertIn('MiB/s', output.getvalue())
                        if response == b'q\n':
                            self.assertEqual(len(failures), 1)
                            self.assertIsInstance(failures[0], tb.BackupError)
                        else:
                            self.assertEqual(failures, [])
                finally:
                    if waiter is not None and waiter.is_alive():
                        os.write(master, b'q\n')
                        waiter.join(3)
                    tb.stop_process(process)
                    os.close(master)
                    os.close(slave)

    def test_loader_wait_also_suppresses_progress_and_releases_output_on_error(self):
        entered, release = threading.Event(), threading.Event()
        media = object.__new__(tb.TapeMedia)
        media.device, media.media_command = '/dev/nst0', '/loader'
        failures = []
        def loader(args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Loader was not released')
            raise tb.BackupError('Loader cancelled')
        def prompt():
            try:
                media.request('a' * 32, 2, 'blank')
            except BaseException as exc:
                failures.append(exc)
        output = io.StringIO()
        with redirect_stderr(output), patch.object(tb, 'run_command', loader):
            worker = threading.Thread(target=prompt)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                progress = tb.Progress('backup')
                progress.report()
                self.assertEqual(output.getvalue(), '')
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            progress.report()
            self.assertIn('MiB/s', output.getvalue())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], tb.BackupError)


if __name__ == '__main__':
    unittest.main()
