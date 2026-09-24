"""Same-cartridge chains and a record/filemark/EOD tape simulator."""
from contextlib import contextmanager, redirect_stderr
import errno
import io
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

import tape_backup as tb
from test_tape_backup import tree_contents


class Cartridge:
    def __init__(self, capacity=None):
        self.records = []
        self.cursor = 0
        self.capacity = capacity
        self.reads = 0
        self.fail_commit = False


class TapeStream:
    def __init__(self, cartridge):
        self.tape = cartridge
        self.closed = False

    def read(self, size):
        assert not self.closed and size == tb.BLOCK_SIZE
        tape = self.tape
        tape.reads += 1
        if tape.cursor == len(tape.records):
            return b''
        record = tape.records[tape.cursor]
        tape.cursor += 1
        return b'' if record is None else record

    def write(self, record):
        assert not self.closed
        tape = self.tape
        if tape.capacity is not None and sum(r is not None for r in tape.records[:tape.cursor]) >= tape.capacity:
            raise OSError(errno.ENOSPC, 'End of tape')
        # A wrong append position really destroys the remaining simulated tape.
        del tape.records[tape.cursor:]
        tape.records.append(bytes(record))
        tape.cursor += 1
        return len(record)

    def close(self):
        self.closed = True


class TapeVolume(tb.Volume):
    def __init__(self, tape):
        super().__init__(TapeStream(tape), physical=True)

    def position(self):
        return self.stream.tape.cursor

    def start_appending(self):
        self.objects = self.durable_objects = self.position()

    def durable_position(self):
        if self.objects != self.position():
            raise AssertionError('Absolute durability position diverged')
        self.durable_objects = self.objects
        return self.objects

    def control(self, operation, count=1):
        tape = self.stream.tape
        if operation == tb.MTWEOF:
            if tape.fail_commit:
                tape.fail_commit = False
                raise OSError(errno.EIO, 'Delayed commit failure')
            del tape.records[tape.cursor:]
            tape.records.extend([None] * count)
            tape.cursor += count
        elif operation == tb.MTEOM:
            tape.cursor = len(tape.records)
        elif operation == tb.MTSEEK:
            if not 0 <= count <= len(tape.records):
                raise OSError(errno.EIO, 'Invalid position')
            tape.cursor = count
        elif operation == tb.MTREW:
            tape.cursor = 0
        elif operation == tb.MTERASE:
            assert count in (0, 1)
            tape.records.clear()
            tape.cursor = 0
        elif operation == tb.MTBSFM:
            marks = [i for i, record in enumerate(tape.records[:tape.cursor]) if record is None]
            if len(marks) < count:
                raise OSError(errno.EIO, 'Beginning of tape')
            tape.cursor = marks[-count] + 1
        elif operation == tb.MTFSF and count == 0:
            pass
        else:
            raise AssertionError((operation, count))


class TapeMedia(tb.TapeMedia):
    def __init__(self, capacity=None):
        self.capacity = capacity
        self.tapes = []
        self.active = None
        self.loaded = False
        self.prepared = self.append_volume = None
        self.entries = []
        self.append_mode = False
        self.requests = []
        self.device = '/dev/simulated-tape'

    @contextmanager
    def lock(self):
        yield

    def request(self, backup_id, number, action):
        self.requests.append((backup_id, number, action))
        if action == 'read' and number == 0:
            if self.active is None:
                raise tb.BackupError('No cartridge loaded')
            return
        if action in ('write', 'blank'):
            self.active = Cartridge(self.capacity)
            self.tapes.append(self.active)
            return
        candidates = []
        for tape in self.tapes:
            for record in tape.records:
                if record is None or not record.startswith((tb.MAGIC, tb.ZFS_MAGIC)):
                    continue
                try:
                    head = tb.decoded_header(record)
                except tb.BackupError:
                    continue
                if (head.get('type') == 'volume' and
                        (backup_id is None or head['backup']['id'] == backup_id) and
                        (action == 'append' or head['volume'] == number)):
                    candidates.append(tape)
                    break
        if not candidates:
            raise tb.BackupError('Incomplete backup: missing cartridge')
        self.active = candidates[-1] if action == 'append' else candidates[0]

    def mt(self, *args):
        if args == ('rewind',):
            self.active.cursor = 0
        elif args != ('setblk', '0'):
            raise AssertionError('Unexpected tape operation: ' + repr(args))

    def raw_open(self, writing):
        return TapeVolume(self.active)


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'book').write_bytes(os.urandom(300_000))
        (self.source / 'remove').write_text('original')
        self.output = io.StringIO()
        context = redirect_stderr(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        context = patch.object(tb.os, 'sync')
        context.start()
        self.addCleanup(context.stop)

    def backup(self, media, base=None, **kwargs):
        return tb.backup(self.source, media, level='incremental' if base else 'full',
                         base=base, append=base is not None, quiet=True,
                         buffer_size=512 * 1024, **kwargs)

    def restore(self, media, ids, expected):
        destination = self.root / ('restore-' + str(len(list(self.root.glob('restore-*')))))
        tb.restore(ids, destination, media, quiet=True)
        self.assertEqual(tree_contents(destination), expected)

    def test_file_cartridge_two_appends_preserve_bytes_and_all_recovery_points(self):
        media = tb.FileMedia(self.root / 'media')
        full = self.backup(media)
        first_tree = tree_contents(self.source)
        tape = next(media.directory.glob('*.tape'))
        original = tape.read_bytes()
        (self.source / 'remove').unlink()
        (self.source / 'new').write_text('new file')
        one = self.backup(media, full)
        second_tree = tree_contents(self.source)
        self.assertEqual(tape.read_bytes()[:len(original)], original)
        self.assertEqual(len(list(media.directory.glob('*.tape'))), 1)
        (self.source / 'book').write_text('changed')
        two = self.backup(media, one)
        self.assertIn('no archive scan', self.output.getvalue())
        for ids, expected in (([full], first_tree), ([full, one], second_tree),
                              ([full, one, two], tree_contents(self.source))):
            self.restore(media, ids, expected)
            self.assertTrue(tb.scan(media, ids[-1])['data_verified'])
        listing = tb.inspect_all(media)
        self.assertEqual([x['id'] for x in listing['backups']], [full, one, two])
        self.assertEqual(tb.inspect_backup(media, two)['id'], two)

    def test_physical_cartridge_two_appends_and_restore_without_reload(self):
        media = TapeMedia()
        full = self.backup(media)
        original = list(media.tapes[0].records)
        (self.source / 'new').write_text('first incremental')
        one = self.backup(media, full)
        (self.source / 'book').write_text('second incremental')
        two = self.backup(media, one)
        self.assertEqual(media.tapes[0].records[:len(original)], original)
        self.assertEqual(len(media.tapes), 1)
        media.loaded = False
        media.requests.clear()
        self.restore(media, [full, one, two], tree_contents(self.source))
        self.assertEqual(media.requests, [(full, 1, 'read')])

    def test_incremental_without_append_option_preserves_full_and_prior_deltas(self):
        media = TapeMedia()
        full = self.backup(media)
        original = list(media.active.records)
        request = media.request
        def leave_loaded(backup_id, number, action):
            if action in ('write', 'blank'):
                media.requests.append((backup_id, number, action))
            else:
                request(backup_id, number, action)
        media.request = leave_loaded
        ids = [full]
        for index in range(2):
            (self.source / 'new').write_text(f'incremental {index}')
            ids.append(tb.backup(self.source, media, level='incremental', base=ids[-1], quiet=True))
            self.assertEqual(media.active.records[:len(original)], original)
            original = list(media.active.records)
        self.assertEqual(len(media.tapes), 1)
        media.loaded = False
        self.restore(media, ids, tree_contents(self.source))

    def test_physical_tape_supports_separate_incremental_restore_commands(self):
        media = TapeMedia(capacity=30)
        full = self.backup(media)
        first_tree = tree_contents(self.source)
        (self.source / 'new').write_bytes(os.urandom(1_000_000))
        first = self.backup(media, full)
        second_tree = tree_contents(self.source)
        (self.source / 'remove').unlink()
        second = self.backup(media, first)
        destination = self.root / 'separate-restore'
        for backup_id, expected in ((full, first_tree), (first, second_tree),
                                    (second, tree_contents(self.source))):
            media.loaded = False
            tb.restore([backup_id], destination, media, quiet=True)
            self.assertEqual(tree_contents(destination), expected)

    def test_incremental_rollover_rejects_the_still_loaded_base_cartridge(self):
        media = TapeMedia()
        full = self.backup(media)
        original = list(media.active.records)
        media.active.capacity = sum(r is not None for r in original) + 5
        (self.source / 'new').write_bytes(os.urandom(200_000))
        request = media.request
        attempts = 0
        def leave_loaded(backup_id, number, action):
            nonlocal attempts
            if action == 'blank':
                attempts += 1
                if attempts == 2:
                    raise tb.BackupError('Media change cancelled')
            if action not in ('write', 'blank'):
                request(backup_id, number, action)
        media.request = leave_loaded
        with self.assertRaisesRegex(tb.BackupError, 'cancelled'):
            self.backup(media, full)
        self.assertEqual(attempts, 2)
        self.assertIn('recorded data', self.output.getvalue())
        self.assertEqual(media.active.records[:len(original)], original)
        media.loaded = False
        self.assertTrue(tb.scan(media, full)['data_verified'])

    def test_legacy_full_without_footer_uses_scan_then_indexes_incremental(self):
        media = tb.FileMedia(self.root / 'media')
        with patch.object(tb, 'write_metadata', return_value=False):
            full = self.backup(media)
        tape = next(media.directory.glob('*.tape'))
        original = tape.read_bytes()
        (self.source / 'new').write_text('new')
        one = self.backup(media, full)
        self.assertIn('scanning the base', self.output.getvalue())
        self.assertEqual(tape.read_bytes()[:len(original)], original)
        self.restore(media, [full, one], tree_contents(self.source))

    def test_wrong_base_and_source_never_change_existing_records(self):
        for physical in (False, True):
            with self.subTest(physical=physical):
                media = TapeMedia() if physical else tb.FileMedia(self.root / 'media')
                full = self.backup(media)
                one = self.backup(media, full)
                def contents():
                    return ([list(t.records) for t in media.tapes] if physical else
                            [p.read_bytes() for p in sorted(media.directory.glob('*.tape'))])
                before = contents()
                with self.assertRaises(tb.BackupError):
                    self.backup(media, full)
                other = self.root / 'other'
                other.mkdir(exist_ok=True)
                with self.assertRaisesRegex(tb.BackupError, 'source differs'):
                    tb.backup(other, media, level='incremental', base=one, append=True, quiet=True)
                self.assertIsNone(media.append_volume)
                self.assertEqual(contents(), before)

    def test_stale_footer_followed_by_partial_append_is_not_used(self):
        media = tb.FileMedia(self.root / 'media')
        full = self.backup(media)
        tape = next(media.directory.glob('*.tape'))
        with tape.open('ab') as stream:
            stream.write(b'partial backup header')
        before = tape.read_bytes()
        with self.assertRaises(tb.BackupError):
            self.backup(media, full)
        self.assertEqual(tape.read_bytes(), before)
        self.restore(media, [full], tree_contents(self.source))

    def test_rollover_during_append_preserves_full_and_restores_chain(self):
        media = TapeMedia(capacity=30)
        full = self.backup(media)
        original = list(media.tapes[0].records)
        (self.source / 'new').write_bytes(os.urandom(2_000_000))
        delta = self.backup(media, full)
        self.assertGreater(len(media.tapes), 1)
        self.assertEqual(media.tapes[0].records[:len(original)], original)
        media.loaded = False
        self.restore(media, [full, delta], tree_contents(self.source))

    def test_capacity_limit_counts_bytes_already_on_cartridge(self):
        media = tb.FileMedia(self.root / 'media')
        full = self.backup(media)
        first = next(media.directory.glob('*.tape'))
        before = first.read_bytes()
        limit = len(before) + 4 * tb.BLOCK_SIZE
        (self.source / 'new').write_bytes(os.urandom(600_000))
        delta = self.backup(media, full, volume_size=limit)
        self.assertLessEqual(first.stat().st_size, limit)
        self.assertEqual(first.read_bytes()[:len(before)], before)
        self.assertGreater(len(list(media.directory.glob('*.tape'))), 1)
        self.restore(media, [full, delta], tree_contents(self.source))

    def test_test_capacity_forces_physical_tape_changes_before_actual_eom(self):
        media = TapeMedia()  # No physical capacity limit or injected EOM errors.
        (self.source / 'book').write_bytes(os.urandom(2_000_000))
        limit = 512 * 1024
        full = self.backup(media, volume_size=limit)
        self.assertGreaterEqual(len(media.tapes), 3)
        for number, tape in enumerate(media.tapes, 1):
            self.assertLessEqual(sum(len(r) for r in tape.records if r is not None), limit)
            header = tb.decoded_header(tape.records[0])
            self.assertEqual(header['volume'], number)
            self.assertEqual(header['backup']['id'], full)
        self.assertEqual(media.requests, [(full, n, 'blank')
                                         for n in range(1, len(media.tapes) + 1)])
        media.loaded = False
        self.assertTrue(tb.scan(media, full)['data_verified'])
        self.restore(media, [full], tree_contents(self.source))

    def test_two_terminal_filemarks_do_not_hide_metadata_or_next_backup(self):
        media = TapeMedia()
        full = self.backup(media)
        media.active.records.append(None)
        original = list(media.active.records)
        one = self.backup(media, full)
        self.assertEqual(media.active.records[:len(original)], original)
        media.loaded = False
        self.restore(media, [full, one], tree_contents(self.source))

    def test_failed_source_during_append_preserves_original_backup(self):
        media = tb.FileMedia(self.root / 'media')
        full = self.backup(media)
        tape = next(media.directory.glob('*.tape'))
        before = tape.read_bytes()
        with patch.object(tb, 'start_archive', side_effect=tb.BackupError('Source failure')):
            with self.assertRaisesRegex(tb.BackupError, 'Source failure'):
                self.backup(media, full)
        self.assertEqual(tape.read_bytes()[:len(before)], before)
        self.restore(media, [full], tree_contents(self.source))
        with self.assertRaises(tb.BackupError):
            self.backup(media, full)

    def test_cached_snapshot_does_not_read_archive_payloads(self):
        media = TapeMedia()
        full = self.backup(media)
        original_read = TapeStream.read
        reads = []
        def observe(stream, size):
            result = original_read(stream, size)
            reads.append(result)
            return result
        with patch.object(TapeStream, 'read', observe):
            delta = self.backup(media, full)
        # Metadata includes a snapshot copy, but no randomized audiobook payload.
        book = (self.source / 'book').read_bytes()
        self.assertFalse(any(book[100_000:100_100] in r for r in reads))
        self.assertIn('no archive scan', self.output.getvalue())
        self.assertTrue(tb.scan(media, delta)['data_verified'])

    def test_tape_already_full_before_append_header_preserves_completed_full(self):
        media = TapeMedia()
        full = self.backup(media)
        original = list(media.active.records)
        media.active.capacity = sum(r is not None for r in original)
        with self.assertRaises(OSError):
            self.backup(media, full)
        self.assertEqual(media.active.records, original)
        media.loaded = False
        self.restore(media, [full], tree_contents(self.source))

    def test_header_commit_failure_does_not_destroy_prior_backup(self):
        media = TapeMedia()
        full = self.backup(media)
        original = list(media.active.records)
        media.active.fail_commit = True
        with self.assertRaises(OSError):
            self.backup(media, full)
        self.assertEqual(media.active.records[:len(original)], original)
        media.loaded = False
        self.restore(media, [full], tree_contents(self.source))

    def test_partial_metadata_file_does_not_hide_successful_archive(self):
        media = TapeMedia()
        original_metadata = tb.write_metadata
        def limited(volume, *args):
            tape = volume.stream.tape
            tape.capacity = sum(r is not None for r in tape.records) + 2
            return original_metadata(volume, *args)
        with patch.object(tb, 'write_metadata', limited):
            full = self.backup(media)
        self.assertIn('metadata file could not be completed', self.output.getvalue())
        before = list(media.active.records)
        media.loaded = False
        self.restore(media, [full], tree_contents(self.source))
        # A second read starts before the incomplete footer. Rewind to the
        # selected archive instead of letting unrelated trailing data hide it.
        media.requests.clear()
        self.restore(media, [full], tree_contents(self.source))
        self.assertEqual(media.requests, [])
        with self.assertRaises(tb.BackupError):
            self.backup(media, full)
        self.assertEqual(media.active.records, before)

    def test_base_spanning_cartridges_can_append_to_its_final_cartridge(self):
        media = TapeMedia(capacity=20)
        (self.source / 'book').write_bytes(os.urandom(1_000_000))
        full = self.backup(media)
        self.assertGreater(len(media.tapes), 1)
        originals = [list(t.records) for t in media.tapes]
        (self.source / 'new').write_text('incremental')
        delta = self.backup(media, full)
        for tape, original in zip(media.tapes, originals):
            self.assertEqual(tape.records[:len(original)], original)
        media.loaded = False
        self.restore(media, [full, delta], tree_contents(self.source))

    def test_mutated_catalog_location_falls_back_or_refuses_without_writing(self):
        media = tb.FileMedia(self.root / 'media')
        full = self.backup(media)
        tape = next(media.directory.glob('*.tape'))
        data = bytearray(tape.read_bytes())
        data[-2 * tb.BLOCK_SIZE + 12] ^= 1  # Snapshot copy, after archive completion.
        tape.write_bytes(data)
        with self.assertRaises(tb.BackupError):
            self.backup(media, full)
        self.assertEqual(tape.read_bytes(), data)
        self.restore(media, [full], tree_contents(self.source))

    def test_first_header_inspection_still_only_reads_one_record(self):
        media = TapeMedia()
        full = self.backup(media)
        media.loaded = False
        before = media.active.reads
        self.assertEqual(tb.inspect_backup(media)['id'], full)
        self.assertEqual(media.active.reads - before, 1)

    def test_listing_continuation_cartridge_and_interrupted_tail(self):
        media = TapeMedia(capacity=20)
        (self.source / 'book').write_bytes(os.urandom(1_000_000))
        full = self.backup(media)
        listing = tb.inspect_all(media, use_catalog=False)
        self.assertTrue(listing['scan_complete'])
        self.assertEqual(listing['backups'][0]['id'], full)
        self.assertGreater(listing['backups'][0]['volume'], 1)
        self.assertTrue(listing['backups'][0]['completion_marker_present'])
        media.active.records.append(b'partial tail')
        listing = tb.inspect_all(media)
        self.assertFalse(listing['scan_complete'])
        self.assertEqual(listing['backups'][0]['id'], full)
        self.assertIn('Truncated tape record', listing['errors'][0]['error'])

    def test_file_listing_handles_multiple_cartridges(self):
        media = tb.FileMedia(self.root / 'media')
        full = self.backup(media, volume_size=8 * tb.BLOCK_SIZE)
        listing = tb.inspect_all(media, use_catalog=False)
        self.assertTrue(listing['scan_complete'])
        self.assertGreater(len(listing['backups']), 1)
        self.assertEqual({b['id'] for b in listing['backups']}, {full})
        self.assertEqual(sum(b['completion_marker_present'] for b in listing['backups']), 1)

    def test_catalog_lists_full_and_incrementals_without_reading_archive_records(self):
        media = TapeMedia()
        ids = [self.backup(media)]
        for index in range(2):
            (self.source / 'new').write_text(f'incremental {index}')
            ids.append(self.backup(media, ids[-1]))
        original = list(media.active.records)
        read = TapeStream.read
        def metadata_only(stream, size):
            tape = stream.tape
            if tape.cursor < len(tape.records):
                record = tape.records[tape.cursor]
                if record is not None and record.startswith(tb.MAGIC):
                    self.assertNotEqual(tb.decoded_header(record)['type'], 'chunk',
                                        'Catalog inspection must not read archive frames')
            return read(stream, size)
        with patch.object(TapeStream, 'read', metadata_only), \
                patch.object(TapeVolume, 'skip_payload', side_effect=AssertionError('Archive scan')):
            listing = tb.inspect_all(media)
        self.assertTrue(listing['scan_complete'])
        self.assertEqual([b['id'] for b in listing['backups']], ids)
        self.assertEqual([b['parent'] for b in listing['backups']], [None, *ids[:-1]])
        self.assertEqual({b['listing_method'] for b in listing['backups']}, {'catalog'})
        self.assertTrue(all(b['completion_marker_present'] is None for b in listing['backups']))
        self.assertFalse(listing['data_verified'])
        self.assertEqual(media.active.records, original)

    def test_missing_catalog_lists_all_backups_by_scanning(self):
        media = TapeMedia()
        with patch.object(tb, 'write_metadata', return_value=False):
            full = self.backup(media)
            (self.source / 'new').write_text('delta')
            delta = self.backup(media, full)
        listing = tb.inspect_all(media)
        self.assertEqual([b['id'] for b in listing['backups']], [full, delta])
        self.assertEqual({b['listing_method'] for b in listing['backups']}, {'scan'})
        self.assertTrue(listing['scan_complete'])
        self.assertTrue(all(b['completion_marker_present'] for b in listing['backups']))

    def test_partial_tail_cannot_hide_later_backup_behind_older_catalog(self):
        media = TapeMedia()
        full = self.backup(media)
        (self.source / 'new').write_text('delta')
        with patch.object(tb, 'write_metadata', return_value=False):
            delta = self.backup(media, full)
        media.active.records.append(b'partial next header')
        listing = tb.inspect_all(media)
        self.assertEqual([b['id'] for b in listing['backups']], [full, delta])
        self.assertFalse(listing['scan_complete'])
        self.assertEqual({b['listing_method'] for b in listing['backups']}, {'scan'})
        self.assertIn('Truncated tape record', listing['errors'][0]['error'])

    def test_invalid_catalog_locations_fall_back_without_duplicate_entries(self):
        media = TapeMedia()
        full = self.backup(media)
        (self.source / 'new').write_text('delta')
        delta = self.backup(media, full)
        latest = tb.latest_metadata
        def bad_location(volume, *args):
            cached = latest(volume, *args)
            cached['entries'][-1]['header_sha256'] = 'f' * 64
            return cached
        with patch.object(tb, 'latest_metadata', bad_location):
            listing = tb.inspect_all(media)
        self.assertEqual([b['id'] for b in listing['backups']], [full, delta])
        self.assertTrue(listing['scan_complete'])
        self.assertEqual({b['listing_method'] for b in listing['backups']}, {'scan'})

    def test_catalog_lists_continuation_cartridge_without_other_volumes(self):
        media = TapeMedia(capacity=30)
        (self.source / 'book').write_bytes(os.urandom(2_000_000))
        full = self.backup(media)
        media.active.capacity = None  # Leave room for the incremental's complete catalog.
        (self.source / 'new').write_text('delta')
        delta = self.backup(media, full)
        self.assertGreater(len(media.tapes), 1)
        media.tapes = [media.active]
        listing = tb.inspect_all(media)
        self.assertTrue(listing['scan_complete'])
        self.assertEqual([b['id'] for b in listing['backups']], [full, delta])
        self.assertGreater(listing['backups'][0]['volume'], 1)
        self.assertEqual({b['listing_method'] for b in listing['backups']}, {'catalog'})

    def test_position_change_after_preflight_aborts_before_writing(self):
        media = TapeMedia()
        full = self.backup(media)
        original = list(media.active.records)
        estimate = tb.estimate_source
        def changed(*args):
            media.active.records.append(b'new tail'.ljust(tb.BLOCK_SIZE, b'\0'))
            return estimate(*args)
        with patch.object(tb, 'estimate_source', changed):
            with self.assertRaisesRegex(tb.BackupError, 'end changed'):
                self.backup(media, full)
        self.assertEqual(media.active.records[:-1], original)

    def test_append_final_commit_failure_rolls_over_and_keeps_full(self):
        media = TapeMedia()
        full = self.backup(media)
        original = list(media.active.records)
        (self.source / 'new').write_text('new file')
        commit = TapeVolume.commit
        calls = 0
        def fail_final(volume):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, 'Final commit failure')
            return commit(volume)
        with patch.object(TapeVolume, 'commit', fail_final):
            delta = self.backup(media, full)
        self.assertGreater(len(media.tapes), 1)
        self.assertEqual(media.tapes[0].records[:len(original)], original)
        media.loaded = False
        self.restore(media, [full, delta], tree_contents(self.source))

    def test_eio_boundary_requires_end_of_data_status(self):
        stream = Mock()
        stream.read.side_effect = OSError(errno.EIO, 'Tape error')
        volume = tb.Volume(stream, physical=True)
        volume.position = Mock(return_value=42)
        def status(flags):
            def ioctl(fd, operation, raw, mutate):
                raw[:] = struct.pack('@5l2i', 0, 0, 0, flags, 0, 0, 0)
            return ioctl
        with patch.object(tb.fcntl, 'ioctl', status(0)):
            with self.assertRaises(OSError):
                volume.next_record()
        with patch.object(tb.fcntl, 'ioctl', status(0x08000000)):
            self.assertIsNone(volume.next_record())

    def test_append_origin_and_unsupported_position_fallback(self):
        volume = tb.Volume(Mock(), physical=True)
        with patch.object(tb, 'tape_position', return_value=(123, 123)):
            volume.start_appending()
        self.assertEqual((volume.objects, volume.durable_objects), (123, 123))
        with patch.object(tb, 'tape_position', return_value=None):
            volume.start_appending()
        self.assertTrue(volume.position_disabled)
        self.assertEqual((volume.objects, volume.durable_objects), (0, 0))

    def test_linux_logical_address_setup_supports_full_append_restore(self):
        media = TapeMedia()
        media.mt = tb.TapeMedia.mt.__get__(media)
        options = 0
        original_run, original_position = tb.run_command, TapeVolume.position
        def command(args):
            nonlocal options
            if args[0] != 'mt':
                return original_run(args)
            if args[3:] == ['stsetoptions', 'scsi2logical']:
                options |= 0x800
            elif args[3:] == ['stclearoptions', '0xa002']:
                options &= ~0xa002
            else:
                TapeMedia.mt(media, *args[3:])
        def position(volume):
            if not options & 0x800:
                raise OSError(errno.EIO, 'Drive rejects legacy device-dependent position form')
            return original_position(volume)
        with patch.object(tb, 'run_command', command), \
                patch.object(Path, 'read_text', side_effect=lambda: hex(options)), \
                patch.object(TapeVolume, 'position', position):
            full = self.backup(media)
            (self.source / 'new').write_text('incremental')
            delta = self.backup(media, full)
            self.assertTrue(tb.scan(media, delta)['data_verified'])
            media.loaded = False
            self.restore(media, [full, delta], tree_contents(self.source))

    def test_full_first_cartridge_refuses_unknown_blank_state_before_writing(self):
        media = TapeMedia()
        original_position = TapeVolume.position
        def position(volume):
            if not volume.stream.tape.records:
                raise OSError(errno.EIO, 'Position unavailable on unwritten tape')
            return original_position(volume)
        with patch.object(TapeVolume, 'position', position):
            with self.assertRaisesRegex(OSError, 'opening volume 1'):
                self.backup(media)
        self.assertEqual(len(media.tapes), 1)
        self.assertEqual(media.active.records, [])

    def test_startup_io_errors_identify_device_operation_and_preserve_errno(self):
        cases = [('raw_open', 'opening', TapeMedia),
                 ('write', 'writing header for', TapeVolume),
                 ('commit', 'committing header for', TapeVolume),
                 ('durable_position', 'checking write position for', TapeVolume)]
        for method, phase, owner in cases:
            with self.subTest(method=method):
                media = TapeMedia()
                with patch.object(owner, method, side_effect=OSError(errno.EIO, 'Injected startup failure')):
                    with self.assertRaises(OSError) as error:
                        self.backup(media)
                self.assertEqual(error.exception.errno, errno.EIO)
                self.assertIn(phase + ' volume 1', str(error.exception))
                self.assertIn(media.device, str(error.exception))
                self.assertEqual(sum(r is not None for t in media.tapes for r in t.records),
                                 0 if method in ('raw_open', 'write') else 1)

    def test_position_error_names_ioctl(self):
        volume = tb.Volume(Mock(), physical=True)
        with patch.object(tb.fcntl, 'ioctl', side_effect=OSError(errno.EIO, 'Drive error')):
            with self.assertRaisesRegex(OSError, 'MTIOCPOS tape-position query failed') as error:
                volume.position()
        self.assertEqual(error.exception.errno, errno.EIO)

    def test_blank_check_rejects_records_filemarks_and_unknown_position_without_writes(self):
        for records in ([b'unrelated data'.ljust(tb.BLOCK_SIZE, b'\0')], [None], [None, None]):
            with self.subTest(records=len(records)):
                tape = Cartridge()
                tape.records = list(records)
                with self.assertRaisesRegex(tb.BackupError, 'recorded data'):
                    TapeVolume(tape).require_blank()
                self.assertEqual(tape.records, records)
        tape = Cartridge()
        volume = TapeVolume(tape)
        with patch.object(volume, 'position', side_effect=OSError(errno.EIO, 'Cannot establish EOD')):
            with self.assertRaises(OSError):
                volume.require_blank()
        self.assertEqual(tape.records, [])

    def test_incremental_size_cap_rollover_cannot_overwrite_the_base(self):
        media = TapeMedia()
        full = self.backup(media)
        before = list(media.active.records)
        request = media.request
        attempts = 0
        def leave_loaded(backup_id, number, action):
            nonlocal attempts
            if action == 'blank':
                attempts += 1
                if attempts == 2:
                    raise tb.BackupError('Media change cancelled')
            if action not in ('write', 'blank'):
                request(backup_id, number, action)
        media.request = leave_loaded
        with self.assertRaisesRegex(tb.BackupError, 'cancelled'):
            self.backup(media, full, volume_size=4 * tb.BLOCK_SIZE)
        self.assertEqual(attempts, 2)
        self.assertEqual(media.active.records, before)

    def test_full_rollover_retries_recorded_tapes_without_losing_stream_or_recovery(self):
        media = TapeMedia(capacity=20)
        (self.source / 'book').write_bytes(os.urandom(2_000_000))
        used = Cartridge()
        used.records = [b'older backup'.ljust(tb.BLOCK_SIZE, b'\0'), None]
        original_used = list(used.records)
        first_records = None
        streams, requests = [], []
        request, raw_open = media.request, media.raw_open
        attempts = 0
        def track_open(writing):
            volume = raw_open(writing)
            streams.append(volume.stream)
            return volume
        def load_wrong_then_blank(backup_id, number, action):
            nonlocal attempts, first_records
            if action == 'blank' and number == 2:
                self.assertTrue(all(s.closed for s in streams), 'Close rejected tape before requesting another')
                attempts += 1
                requests.append((backup_id, number, action))
                if attempts == 1:
                    first_records = list(media.active.records)
                    return  # Volume 1 is still in the drive.
                if attempts == 2:
                    media.active = used
                    return  # A different used tape is inserted.
            request(backup_id, number, action)
        media.request, media.raw_open = load_wrong_then_blank, track_open
        media.eject = Mock(side_effect=AssertionError('Automatic ejection'))
        with patch.object(tb, 'start_archive', wraps=tb.start_archive) as source:
            full = self.backup(media)
        self.assertEqual(source.call_count, 1)
        self.assertEqual(requests, [(full, 2, 'blank')] * 3)
        self.assertEqual(media.tapes[0].records, first_records)
        self.assertEqual(used.records, original_used)
        self.assertTrue(all(s.closed for s in streams))
        self.assertEqual(self.output.getvalue().count('Backup remains active'), 2)
        media.loaded = False
        self.assertTrue(tb.scan(media, full)['data_verified'])
        self.restore(media, [full], tree_contents(self.source))
        media.eject.assert_not_called()

    def test_incremental_can_retry_blank_first_volume_after_size_cap(self):
        media = TapeMedia()
        full = self.backup(media)
        first = media.active
        before = list(first.records)
        (self.source / 'new').write_text('delta')
        request = media.request
        requests = []
        def leave_base_once(backup_id, number, action):
            if action == 'blank' and number == 1:
                requests.append((backup_id, number, action))
                if len(requests) == 1:
                    return
            request(backup_id, number, action)
        media.request = leave_base_once
        delta = self.backup(media, full, volume_size=len(before) * tb.BLOCK_SIZE)
        self.assertEqual(requests, [(delta, 1, 'blank')] * 2)
        self.assertEqual(first.records, before)
        media.loaded = False
        self.assertTrue(tb.scan(media, delta)['data_verified'])
        self.restore(media, [full, delta], tree_contents(self.source))

    def test_unknown_position_during_continuation_check_still_aborts_without_writing(self):
        media = TapeMedia(capacity=12)
        (self.source / 'book').write_bytes(os.urandom(1_000_000))
        require_blank = TapeVolume.require_blank
        def unknown(volume):
            if len(media.tapes) == 1:
                return require_blank(volume)
            raise OSError(errno.EIO, 'Cannot determine blank state')
        with patch.object(TapeVolume, 'require_blank', unknown):
            with self.assertRaisesRegex(OSError, 'opening volume 2'):
                self.backup(media)
        self.assertEqual(len(media.tapes), 2)
        self.assertEqual(media.tapes[1].records, [])

    def test_continuation_header_failure_is_not_retried_as_wrong_media(self):
        media = TapeMedia(capacity=12)
        (self.source / 'book').write_bytes(os.urandom(1_000_000))
        commit = TapeVolume.commit
        def fail_new_header(volume):
            if len(media.tapes) == 2:
                raise OSError(errno.EIO, 'Failed volume header commit')
            commit(volume)
        with patch.object(TapeVolume, 'commit', fail_new_header):
            with self.assertRaisesRegex(OSError, 'committing header for volume 2'):
                self.backup(media)
        self.assertEqual(len(media.tapes), 2)
        self.assertEqual(len(media.tapes[1].records), 1)


if __name__ == '__main__':
    unittest.main()
