"""Persisted ancestry must protect required cartridges after process changes."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_append import Cartridge, TapeMedia
from test_tape_backup import tree_contents


class ChainProtectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'book').write_bytes(os.urandom(150000))
        self.media = TapeMedia()
        for context in (redirect_stderr(io.StringIO()), patch.object(tb.os, 'sync')):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def backup(self, base=None, cap=None):
        return tb.backup(self.source, self.media, level='incremental' if base else 'full',
                         base=base, volume_size=cap, buffer_size=tb.BLOCK_SIZE, quiet=True)

    def chain(self, old=False):
        with patch.object(tb, 'child_ancestry', return_value={}) if old else patch.object(
                tb, 'child_ancestry', wraps=tb.child_ancestry):
            full = self.backup()
            self.full_tape = self.media.tapes[0]
            (self.source / 'increment').write_text('first delta')
            first = self.backup(full, len(self.full_tape.records) * tb.BLOCK_SIZE)
            self.assertIsNot(self.full_tape, self.media.active)
        return full, first

    def test_oldest_full_on_another_cartridge_is_protected_after_reopening_media(self):
        full, first = self.chain()
        before = list(self.full_tape.records)
        cap = len(self.media.active.records) * tb.BLOCK_SIZE
        new_media = TapeMedia()
        new_media.tapes = self.media.tapes
        self.media = new_media
        request = self.media.request
        rejections = []
        def load(backup_id, number, action):
            if action == 'blank':
                self.media.active = self.full_tape
                with self.assertRaisesRegex(tb.BackupError, 'ancestors'):
                    self.media.wipe_at_prompt(backup_id, io.StringIO('WIPE\n'), io.StringIO())
                rejections.append(number)
            return request(backup_id, number, action)
        self.media.request = load
        (self.source / 'later').write_text('second delta')
        second = self.backup(first, cap)
        self.assertTrue(rejections)
        self.assertEqual(self.full_tape.records, before)
        self.assertEqual(self.media.last_result['ancestors'], [first, full])
        self.assertTrue(self.media.last_result['ancestry_complete'])
        summary = tb.scan(self.media, second)
        self.assertEqual(summary['ancestors'], [first, full])
        tb.restore([full, first, second], self.root / 'restored', self.media, quiet=True)
        self.assertEqual(tree_contents(self.root / 'restored'), tree_contents(self.source))

    def test_older_incremental_chain_still_appends_but_cannot_wipe_recorded_media(self):
        full, first = self.chain(old=True)
        cap = len(self.media.active.records) * tb.BLOCK_SIZE
        request = self.media.request
        rejected = []
        def load(backup_id, number, action):
            if action == 'blank':
                self.media.active = self.full_tape
                with self.assertRaisesRegex(tb.BackupError, 'ancestor chain is unknown'):
                    self.media.wipe_at_prompt(backup_id, io.StringIO('WIPE\n'), io.StringIO())
                rejected.append(number)
            return request(backup_id, number, action)
        self.media.request = load
        (self.source / 'later').write_text('second delta')
        second = self.backup(first, cap)
        self.assertTrue(rejected)
        self.assertFalse(self.media.last_result['ancestry_complete'])
        self.assertEqual(self.media.last_result['ancestors'], [first, full])
        tb.restore([full, first, second], self.root / 'restored', self.media, quiet=True)
        self.assertEqual(tree_contents(self.root / 'restored'), tree_contents(self.source))

    def test_old_full_has_complete_ancestry_and_can_start_a_protected_chain(self):
        full = {'id': 'a' * 32, 'level': 'full', 'parent': None}
        self.assertEqual(tb.child_ancestry(full), {'ancestors': [full['id']], 'ancestry_complete': True})

    def test_ancestry_bound_does_not_block_backup_but_disables_recorded_wipes(self):
        parent = {'id': 'f' * 32, 'level': 'incremental', 'parent': f'{1:032x}',
                  'ancestors': [f'{i:032x}' for i in range(1, tb.MAX_ANCESTORS + 1)],
                  'ancestry_complete': True}
        fields = tb.child_ancestry(parent)
        self.assertEqual(len(fields['ancestors']), tb.MAX_ANCESTORS)
        self.assertFalse(fields['ancestry_complete'])
        job = {'id': 'e' * 32, 'level': 'incremental', 'parent': parent['id'], **fields}
        tb.StreamWriter(self.media, job, None, tb.Progress('test'))
        recorded = Cartridge()
        recorded.records = [b'previous contents'.ljust(tb.BLOCK_SIZE, b'\0')]
        self.media.active = recorded
        with self.assertRaisesRegex(tb.BackupError, 'ancestor chain is unknown'):
            self.media.wipe_at_prompt(job['id'], io.StringIO('WIPE\n'), io.StringIO())
        self.assertTrue(recorded.records)

    def test_invalid_ancestry_is_rejected_instead_of_silently_weakening_protection(self):
        job = {'id': 'a' * 32, 'level': 'incremental', 'parent': 'b' * 32}
        for fields in ({'ancestors': ['b' * 32]},
                       {'ancestors': ['b' * 32, 'a' * 32], 'ancestry_complete': True},
                       {'ancestors': ['b' * 32] * 2, 'ancestry_complete': True},
                       {'ancestors': ['c' * 32], 'ancestry_complete': True},
                       {'ancestors': [[]], 'ancestry_complete': False}):
            with self.subTest(fields=fields), self.assertRaises(tb.BackupError):
                tb.child_ancestry({**job, **fields})


if __name__ == '__main__':
    unittest.main()
