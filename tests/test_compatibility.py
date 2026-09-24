"""The current reader must recover tapes written by the actual v1.0.0 release."""
from contextlib import redirect_stderr
import gzip
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb


class CompatibilityTests(unittest.TestCase):
    def test_released_format3_fixture_verifies_and_restores(self):
        fixture = Path(__file__).parent / 'fixtures/v1.0.0-full.tape.gz'
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()), patch.object(tb.os, 'sync'):
            root = Path(temporary)
            media = tb.FileMedia(root / 'media')
            media.directory.mkdir()
            with gzip.open(fixture, 'rb') as stream:
                data = stream.read()
            backup_id = tb.decoded_header(data[:tb.BLOCK_SIZE])['backup']['id']
            (media.directory / f'{backup_id}.0001.tape').write_bytes(data)
            self.assertTrue(tb.scan(media, backup_id)['data_verified'])
            destination = root / 'restored'
            tb.restore([backup_id], destination, media, quiet=True)
            self.assertEqual((destination / 'book.txt').read_text(), 'format-3 compatibility fixture\n')
            self.assertEqual(list((destination / 'empty').iterdir()), [])
            self.assertEqual((destination / 'link').readlink(), Path('book.txt'))


if __name__ == '__main__':
    unittest.main()
