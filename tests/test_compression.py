"""Hardware compression commands and parsing real SCSI response layouts."""
from contextlib import redirect_stderr, redirect_stdout
import ctypes
import errno
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import tape_backup as tb


def mode_response(enabled=True, supported=True, block_descriptors=0):
    page = bytes([0x0f, 14, (0x80 if enabled else 0) | (0x40 if supported else 0), 0x80])
    page += (1).to_bytes(4, 'big') * 2 + bytes(4)
    return bytes([3 + block_descriptors + len(page), 0, 0, block_descriptors]) + bytes(block_descriptors) + page


class CompressionStatusTests(unittest.TestCase):
    def read_response(self, response, **status):
        def ioctl(fd, operation, raw, mutate):
            self.assertEqual((fd, operation, mutate), (42, 0x2285, True))
            header = tb.SgIoHeader.from_buffer(raw)
            self.assertEqual(ctypes.string_at(header.cmdp, header.cmd_len), bytes([0x1a, 0, 0x0f, 0, 255, 0]))
            self.assertEqual(header.dxfer_direction, -3)
            ctypes.memmove(header.dxferp, response, len(response))
            header.resid = 255 - len(response)
            for name, value in status.items():
                setattr(header, name, value)
        with patch.object(tb.fcntl, 'ioctl', ioctl):
            return tb.compression_status(42)

    def test_current_flags_with_and_without_block_descriptors(self):
        for enabled in (True, False):
            for descriptors in (0, 8, 16):
                with self.subTest(enabled=enabled, descriptors=descriptors):
                    self.assertEqual(self.read_response(mode_response(enabled, block_descriptors=descriptors)),
                                     {'enabled': enabled, 'supported': True})
        self.assertEqual(self.read_response(mode_response(False, False)),
                         {'enabled': False, 'supported': False})

    def test_truncated_or_wrong_mode_page_is_not_reported_as_compression_off(self):
        valid = mode_response()
        bad = [b'', valid[:3], valid[:-1], bytes([255]) + valid[1:],
               valid[:3] + bytes([255]) + valid[4:], valid[:4] + bytes([0x10]) + valid[5:],
               valid[:4] + bytes([0x4f]) + valid[5:], valid[:5] + bytes([13]) + valid[6:]]
        for response in bad:
            with self.subTest(response=response), self.assertRaises(tb.BackupError):
                self.read_response(response)

    def test_scsi_and_permission_errors_do_not_claim_compression_is_disabled(self):
        for status in ({'status': 2}, {'host_status': 1}, {'driver_status': 8}, {'resid': -1}):
            with self.subTest(status=status), self.assertRaises(tb.BackupError):
                self.read_response(mode_response(), **status)
        with patch.object(tb.fcntl, 'ioctl', side_effect=OSError(errno.EPERM, 'Not permitted')):
            with self.assertRaisesRegex(OSError, 'Cannot read drive compression state'):
                tb.compression_status(42)


class CompressionCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.media = object.__new__(tb.TapeMedia)
        self.media.device = '/dev/nst1'
        self.media.device_number = os.makedev(9, 129)
        self.volume = Mock()
        self.volume.stream.fileno.return_value = 42
        self.media.raw_open = Mock(return_value=self.volume)
        self.media.mt = Mock(side_effect=AssertionError('Compression must not move tape'))
        context = patch.object(Path, 'home', return_value=Path(temporary.name))
        context.start()
        self.addCleanup(context.stop)

    def command(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(tb, 'TapeMedia', return_value=self.media) as factory, \
                redirect_stdout(output), redirect_stderr(errors):
            code = tb.main(['compression', *args, '--device', self.media.device])
        factory.assert_called_once_with(self.media.device)
        self.output, self.errors = output.getvalue(), errors.getvalue()
        return code

    def test_status_is_default_and_opens_read_only_without_setting_compression(self):
        for args, supported, enabled, expected in (([], True, True, 'ON'),
                                                  (['status'], True, False, 'OFF'),
                                                  ([], False, False, 'UNSUPPORTED')):
            with self.subTest(args=args, expected=expected):
                self.media.raw_open.reset_mock()
                with patch.object(tb, 'compression_status', return_value={'supported': supported, 'enabled': enabled}):
                    self.assertEqual(self.command(*args), 0, self.errors)
                self.media.raw_open.assert_called_once_with(False)
                self.volume.control.assert_not_called()
                self.assertEqual(self.output.strip(), f'Hardware compression on /dev/nst1: {expected}')
                self.volume.close.assert_called_once()

    def test_on_and_off_use_driver_control_and_verify_result(self):
        for action, enabled in (('on', True), ('off', False)):
            with self.subTest(action=action):
                self.volume.reset_mock()
                self.volume.stream.fileno.return_value = 42
                self.media.raw_open.reset_mock()
                with patch.object(tb, 'compression_status', side_effect=[
                        {'supported': True, 'enabled': not enabled},
                        {'supported': True, 'enabled': enabled}]) as status:
                    self.assertEqual(self.command(action), 0, self.errors)
                self.media.raw_open.assert_called_once_with(True)
                self.volume.control.assert_called_once_with(tb.MTCOMPRESSION, int(enabled))
                self.assertEqual(status.call_count, 2)
                self.volume.close.assert_called_once()
                self.assertIn(action.upper(), self.output)

    def test_unsupported_drive_is_not_changed(self):
        with patch.object(tb, 'compression_status', return_value={'supported': False, 'enabled': False}):
            self.assertEqual(self.command('on'), 1)
        self.volume.control.assert_not_called()
        self.volume.close.assert_called_once()
        self.assertIn('unsupported', self.errors)

    def test_readback_mismatch_or_failure_is_not_reported_as_success(self):
        for result in ({'supported': True, 'enabled': False}, OSError(errno.EIO, 'Query failed')):
            with self.subTest(result=result):
                self.volume.reset_mock()
                with patch.object(tb, 'compression_status', side_effect=[
                        {'supported': True, 'enabled': False}, result]):
                    self.assertEqual(self.command('on'), 1)
                self.assertEqual(self.output, '')
                self.volume.close.assert_called_once()
                with self.media.lock():
                    pass

    def test_active_drive_lock_blocks_status_and_changes(self):
        for action in ('status', 'on', 'off'):
            with self.subTest(action=action), self.media.lock():
                self.assertEqual(self.command(action), 1)
                self.assertIn('Another operation', self.errors)
        self.media.raw_open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
