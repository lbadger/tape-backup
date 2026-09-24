"""Cartridge identity, advisory recovery plans, and non-writing previews."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from test_append import TapeMedia
from test_prompt_wipe import terminal
from test_tape_backup import tree_contents


class PlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'book').write_bytes(os.urandom(700000))
        (self.source / 'deleted').write_text('before')
        self.media = tb.FileMedia(self.root / 'media')
        self.inventory = self.root / 'inventory.json'
        for context in (redirect_stderr(io.StringIO()), patch.object(tb.os, 'sync')):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def create(self, base=None, cap=None, **kwargs):
        return tb.backup(self.source, self.media, level='incremental' if base else 'full',
                         base=base, volume_size=cap, buffer_size=tb.BLOCK_SIZE, quiet=True, **kwargs)

    def observe(self):
        return tb.inspect_all(self.media, allow_scan=False, header_fallback=True)['backups']

    def cli(self, *args, expected=0):
        output = io.StringIO()
        with redirect_stdout(output):
            code = tb.main(list(map(str, args)))
        self.assertEqual(code, expected)
        return output.getvalue()

    def test_multivolume_labels_inventory_plan_and_restore(self):
        self.media.label_prefix = 'BOOKS'
        full = self.create(cap=512 * 1024)
        (self.source / 'deleted').unlink()
        (self.source / 'book').write_bytes(os.urandom(800000))
        delta = self.create(full, cap=512 * 1024)
        # Earlier cartridges can contain multiple segments but no final catalog.
        # Explicit scanning discovers those otherwise unknown segment boundaries.
        entries = tb.inspect_all(self.media, allow_scan=True)['backups']
        self.assertGreater(len({e['cartridge_id'] for e in entries}), 2)
        self.assertTrue(all(e['cartridge_label'].startswith('BOOKS-') for e in entries))
        tb.update_inventory(self.inventory, entries)
        before = tree_contents(self.media.directory)
        plan = json.loads(self.cli('restore', '--to', delta, '--plan', '--inventory', self.inventory, '--json'))
        self.assertTrue(plan['plan_complete'])
        self.assertEqual(plan['backup_ids'], [full, delta])
        self.assertFalse(plan['data_verified'])
        self.assertEqual(tree_contents(self.media.directory), before)
        self.cli('restore', '--to', delta, '--inventory', self.inventory,
                 '--media-dir', self.media.directory, '--destination', self.root / 'restored')
        self.assertEqual(tree_contents(self.source), tree_contents(self.root / 'restored'))
        self.inventory.unlink()
        tb.restore([full, delta], self.root / 'without-cache', self.media, quiet=True)
        self.assertEqual(tree_contents(self.source), tree_contents(self.root / 'without-cache'))

    def test_append_keeps_identity_even_when_prefix_changes(self):
        self.media.label_prefix = 'FIRST'
        full = self.create()
        original = self.observe()[0]
        self.media.label_prefix = 'SECOND'
        (self.source / 'added').write_text('delta')
        delta = self.create(full)
        entries = self.observe()
        self.assertEqual([e['id'] for e in entries], [full, delta])
        self.assertEqual({e['cartridge_id'] for e in entries}, {original['cartridge_id']})
        self.assertEqual({e['cartridge_label'] for e in entries}, {'FIRST-001'})
        self.assertTrue(tb.restore_plan(delta, entries)['plan_complete'])

    def test_legacy_cartridge_identity_is_stable_after_append(self):
        encode = tb.encoded_header
        def legacy(value):
            value = dict(value)
            value.pop('cartridge_identity', None)
            return encode(value)
        with patch.object(tb, 'encoded_header', legacy):
            full = self.create()
        original = self.observe()[0]
        self.assertIsNone(original['cartridge_label'])
        delta = self.create(full)
        entries = self.observe()
        self.assertEqual({e['cartridge_id'] for e in entries}, {original['cartridge_id']})
        self.assertTrue(tb.scan(self.media, delta)['data_verified'])

    def test_missing_middle_backup_still_lists_known_full_ancestor(self):
        full = self.create()
        one = self.create(full)
        two = self.create(one)
        entries = [e for e in self.observe() if e['id'] != one]
        plan = tb.restore_plan(two, entries)
        self.assertEqual(plan['backup_ids'], [full, one, two])
        self.assertEqual(plan['missing_backups'], [one])
        self.assertFalse(plan['plan_complete'])
        tb.update_inventory(self.inventory, entries)
        self.cli('restore', '--to', two, '--plan', '--inventory', self.inventory, expected=2)
        destination = self.root / 'not-created'
        self.cli('restore', '--to', two, '--inventory', self.inventory, '--media-dir', self.media.directory,
                 '--destination', destination, expected=1)
        self.assertFalse(destination.exists())

    def test_missing_volume_and_unknown_count_are_explicit(self):
        full = self.create(cap=512 * 1024)
        entries = self.observe()
        total = max(e['volume'] for e in entries)
        plan = tb.restore_plan(full, [e for e in entries if e['volume'] != 1])
        self.assertFalse(plan['plan_complete'])
        self.assertEqual(plan['backups'][0]['missing_volumes'], [1])
        plan = tb.restore_plan(full, [e for e in entries if e['volume'] != total])
        self.assertIsNone(plan['backups'][0]['expected_volumes'])
        self.assertFalse(plan['plan_complete'])

    def test_inventory_merge_is_atomic_and_rejects_conflicts(self):
        self.create(cap=512 * 1024)
        entries = self.observe()
        for entry in entries:
            tb.update_inventory(self.inventory, [entry])
        tb.update_inventory(self.inventory, entries)
        self.assertEqual(len(tb.read_inventory(self.inventory)['backups']), len(entries))
        before = self.inventory.read_bytes()
        conflict = copy.deepcopy(entries[-1])
        conflict['source'] = '/another-source'
        with self.assertRaisesRegex(tb.BackupError, 'Conflicting'):
            tb.update_inventory(self.inventory, [conflict])
        self.assertEqual(self.inventory.read_bytes(), before)
        with patch.object(tb.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                tb.update_inventory(self.inventory, entries)
        self.assertEqual(self.inventory.read_bytes(), before)
        self.assertEqual(list(self.root.glob('.inventory.json-*')), [])

    def test_source_change_cycles_and_malformed_inventory_are_rejected(self):
        full = self.create()
        delta = self.create(full)
        entries = self.observe()
        bad = copy.deepcopy(entries)
        bad[-1]['source'] = '/another-source'
        with self.assertRaisesRegex(tb.BackupError, 'changes source'):
            tb.restore_plan(delta, bad)
        bad = copy.deepcopy(entries)
        for entry in bad:
            entry.pop('ancestors')
            entry.pop('ancestry_complete')
        bad[0].update(level='incremental', parent=delta)
        with self.assertRaisesRegex(tb.BackupError, 'Cycle'):
            tb.restore_plan(delta, bad)
        for content in ('[]', '{', '{"version":1,"backups":[{}]}'):
            self.inventory.write_text(content)
            self.cli('restore', '--to', delta, '--plan', '--inventory', self.inventory, expected=1)

    def test_stale_inventory_cannot_override_actual_tape_identity(self):
        full = self.create()
        tb.update_inventory(self.inventory, self.observe())
        for path in self.media.directory.glob('*.tape'):
            path.unlink()
        unrelated = self.create()
        self.assertNotEqual(full, unrelated)
        self.cli('restore', '--to', full, '--inventory', self.inventory, '--media-dir', self.media.directory,
                 '--destination', self.root / 'restored', expected=1)
        self.assertFalse((self.root / 'restored').exists())

    def test_full_preview_needs_no_drive_and_never_starts_archive(self):
        with patch.object(tb, 'TapeMedia', side_effect=AssertionError('opened drive')), \
                patch.object(tb, 'start_archive', side_effect=AssertionError('started tar')):
            result = json.loads(self.cli('backup', '--source', self.source, '--dry-run', '--json',
                                         '--cartridge-capacity', '512KiB', '--exclude', 'book'))
        self.assertTrue(result['dry_run'])
        self.assertLess(result['estimated_bytes'], 700000)
        self.assertEqual(result['excludes'], ['book'])
        self.assertFalse(result['tape_readiness_checked'])
        self.assertFalse(self.media.directory.exists())

    def test_incremental_preview_inherits_exclusions_and_preserves_tape(self):
        full = self.create(excludes=['deleted'])
        before = tree_contents(self.media.directory)
        with patch.object(tb, 'start_archive', side_effect=AssertionError('started tar')), \
                patch.object(tb, 'prepare_append', side_effect=AssertionError('prepared writer')):
            result = self.create(full, dry_run=True)
            self.assertEqual(result['parent'], full)
            self.assertEqual(result['excludes'], ['deleted'])
            with self.assertRaisesRegex(tb.BackupError, 'Exclusions differ'):
                self.create(full, dry_run=True, excludes=['book'])
        self.assertEqual(tree_contents(self.media.directory), before)

    def test_physical_header_inventory_does_not_scan_archive(self):
        self.media = TapeMedia(capacity=12)
        self.media.label_prefix = 'PHYSICAL'
        full = self.create()
        entries = []
        with patch.object(tb.Volume, 'skip_payload', side_effect=AssertionError('scanned payload')):
            for tape in self.media.tapes:
                self.media.active = tape
                entries.extend(self.observe())
        self.assertTrue(tb.restore_plan(full, entries)['plan_complete'])
        self.assertEqual(len({e['cartridge_id'] for e in entries}), len(self.media.tapes))
        tb.restore([full], self.root / 'restored', self.media, quiet=True)
        self.assertEqual(tree_contents(self.source), tree_contents(self.root / 'restored'))

    def test_inventory_collection_and_plan_from_current_cartridge(self):
        full = self.create()
        delta = self.create(full)
        document = json.loads(self.cli('inventory', '--media-dir', self.media.directory,
                                       '--output', self.inventory, '--json'))
        self.assertTrue(document['advisory'])
        self.assertEqual(len(document['backups']), 2)
        plan = json.loads(self.cli('restore', '--to', delta, '--plan', '--media-dir', self.media.directory, '--json'))
        self.assertTrue(plan['plan_complete'])

    def test_completed_backup_records_all_cartridges_without_readback(self):
        self.media.inventory_output = self.inventory
        full = self.create(cap=512 * 1024)
        delta = self.create(full, cap=512 * 1024)
        entries = tb.read_inventory(self.inventory)['backups']
        self.assertTrue(tb.restore_plan(delta, entries)['plan_complete'])
        self.assertTrue(self.media.last_result['inventory_updated'])
        self.assertTrue(all(e['listing_method'] == 'write' for e in entries))

    def test_inventory_failure_does_not_discard_committed_backup(self):
        self.media.inventory_output = self.inventory
        with patch.object(tb, 'update_inventory', side_effect=OSError('disk unavailable')):
            full = self.create()
        self.assertTrue(self.media.last_result['archive_complete'])
        self.assertFalse(self.media.last_result['inventory_updated'])
        self.assertTrue(self.media.last_result['warnings'])
        self.assertTrue(tb.scan(self.media, full)['data_verified'])

    def test_corrupt_cache_flags_cannot_claim_data_verification(self):
        self.create()
        entry = self.observe()[0]
        entry['data_verified'] = True
        entry['completion_verified'] = True
        tb.update_inventory(self.inventory, [entry])
        result = tb.read_inventory(self.inventory)
        self.assertFalse(result['backups'][0]['data_verified'])
        self.assertFalse(result['backups'][0]['completion_verified'])

    def test_inventory_limit_preserves_old_file(self):
        self.create()
        entries = self.observe()
        tb.update_inventory(self.inventory, entries)
        before = self.inventory.read_bytes()
        # Existing JSON is still readable; a new entry makes the serialized cache too large.
        second = dict(entries[0], cartridge_id='a' * 32)
        with patch.object(tb, 'MAX_CATALOG_BYTES', len(before) + 1):
            with self.assertRaisesRegex(tb.BackupError, 'exceeds'):
                tb.update_inventory(self.inventory, [second])
        self.assertEqual(self.inventory.read_bytes(), before)

    def test_read_prompt_shows_inventory_label_without_changing_requested_identity(self):
        full = self.create()
        media = object.__new__(tb.TapeMedia)
        media.device, media.media_command = '/dev/nst0', None
        media.inventory_entries = self.observe()
        with terminal('\n') as output:
            media.request(full, 1, 'read')
        self.assertIn(media.inventory_entries[0]['cartridge_label'], output.getvalue())
        self.assertIn(full + ' volume 1', output.getvalue())

    def test_preview_and_plan_interruptions_do_not_report_incomplete_backups(self):
        output = io.StringIO()
        with redirect_stderr(output), patch.object(tb, 'estimate_source', side_effect=KeyboardInterrupt):
            self.cli('backup', '--source', self.source, '--dry-run', expected=130)
        self.assertIn('no archive was written', output.getvalue())
        full = self.create()
        tb.update_inventory(self.inventory, self.observe())
        output = io.StringIO()
        with redirect_stderr(output), patch.object(tb, 'restore_plan', side_effect=KeyboardInterrupt):
            self.cli('restore', '--to', full, '--plan', '--inventory', self.inventory, expected=130)
        self.assertIn('no destination was modified', output.getvalue())

    def test_interrupted_inventory_write_reports_already_committed_backup(self):
        output = io.StringIO()
        with redirect_stderr(output), patch.object(tb, 'update_inventory', side_effect=KeyboardInterrupt):
            self.cli('backup', '--source', self.source, '--media-dir', self.media.directory,
                     '--inventory', self.inventory, expected=130)
        self.assertIn('was committed; post-backup reporting interrupted', output.getvalue())
        self.assertNotIn('Start a new full', output.getvalue())
        full = next(self.media.directory.glob('*.tape')).name.split('.')[0]
        self.assertTrue(tb.scan(self.media, full)['data_verified'])


if __name__ == '__main__':
    unittest.main()
