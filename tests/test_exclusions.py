"""Exclusions must agree between GNU tar, inventory, local and SSH sources."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

import tape_backup as tb
from ssh_fixture import SSHServer
from test_tape_backup import tree_contents


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        for directory in ('cache/deep', 'nested/cache', 'config', 'empty'):
            (self.source / directory).mkdir(parents=True)
        for name in ('cache/deep/secret', 'config/private.env', 'nested/cache/keep',
                     'old', 'change', 'a.tmp', 'nested/b.tmp', 'space name'):
            (self.source / name).write_text(name)
        self.media = tb.FileMedia(self.root / 'media')
        self.rules = ['cache', 'config/private.env', '*.tmp', 'space name']
        context = redirect_stderr(io.StringIO())
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        context = patch.object(tb.os, 'sync')
        context.start()
        self.addCleanup(context.stop)

    def create(self, base=None, excludes=None, ssh=None):
        return tb.backup(self.source, self.media, level='incremental' if base else 'full',
                         base=base, excludes=excludes, ssh=ssh, buffer_size=tb.BLOCK_SIZE,
                         volume_size=1024**2, quiet=True)

    def round_trip(self, ssh=None):
        full = self.create(excludes=self.rules, ssh=ssh)
        self.assertEqual(tb.inspect_backup(self.media, full)['excludes'], sorted(self.rules))
        (self.source / 'old').unlink()
        (self.source / 'change').write_text('updated')
        (self.source / 'new').write_text('added')
        (self.source / 'cache/new-secret').write_text('excluded in incremental too')
        delta = self.create(base=full, ssh=ssh)
        self.assertEqual(tb.inspect_backup(self.media, delta)['excludes'], sorted(self.rules))
        destination = self.root / 'restored'
        tb.restore([full, delta], destination, self.media, quiet=True)
        self.assertEqual({p.relative_to(destination).as_posix() for p in destination.rglob('*')},
                         {'config', 'empty', 'nested', 'nested/cache', 'nested/cache/keep', 'change', 'new'})
        self.assertEqual((destination / 'change').read_text(), 'updated')
        self.assertEqual((destination / 'nested/cache/keep').read_text(), 'nested/cache/keep')

    def test_local_full_incremental_inheritance_and_directory_records(self):
        self.round_trip()

    def test_ssh_full_incremental_has_identical_exclusion_semantics(self):
        server = SSHServer()
        self.addCleanup(server.close)
        program = self.root / 'remote-program'
        program.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(Path(tb.__file__).resolve())]) + ' "$@"\n')
        program.chmod(0o700)
        self.round_trip(tb.SSHConfig('backup-source', config=server.config, program=str(program)))

    def test_policy_changes_are_rejected_without_modifying_tape(self):
        full = self.create(excludes=self.rules)
        before = tree_contents(self.media.directory)
        for rules in ([], ['cache'], ['another']):
            with self.subTest(rules=rules), self.assertRaisesRegex(tb.BackupError, 'Exclusions differ'):
                self.create(base=full, excludes=rules)
            self.assertEqual(tree_contents(self.media.directory), before)
        # Ordering, duplicate paths, ./ and trailing directory slashes normalize.
        self.create(base=full, excludes=[*reversed(self.rules), './cache/'])

    def test_exclude_file_preserves_spaces_and_empty_lines_and_fails_before_media(self):
        path = self.root / 'exclusions'
        path.write_bytes(b'cache\r\n\r\nspace name\r\n#literal\r\n')
        self.assertEqual(tb.exclusion_options(['*.tmp'], [path]),
                         ['#literal', '*.tmp', 'cache', 'space name'])
        self.assertIsNone(tb.exclusion_options())
        self.assertEqual(tb.exclusion_options([], []), [])
        self.assertEqual(tb.main(['backup', '--source', str(self.source), '--media-dir', str(self.root / 'new-media'),
                                 '--exclude-from', str(self.root / 'missing')]), 1)
        self.assertFalse((self.root / 'new-media').exists())

    def test_inventory_prunes_excluded_subtrees_and_counts_included_files(self):
        excluded = self.source / 'cache'
        excluded.chmod(0)
        self.addCleanup(excluded.chmod, 0o700)
        # Neither inventory nor tar needs access inside the excluded subtree.
        size = tb.estimate_source(self.source, excludes=self.rules)
        self.assertGreater(size, 0)
        full = self.create(excludes=self.rules)
        self.assertTrue(tb.scan(self.media, full)['data_verified'])

    def test_legacy_parent_without_policy_means_no_exclusions(self):
        full = self.create()
        self.assertNotIn('excludes', tb.inspect_backup(self.media, full))
        with self.assertRaisesRegex(tb.BackupError, 'Exclusions differ'):
            self.create(base=full, excludes=['cache'])
        self.create(base=full)

    def test_invalid_and_oversized_policies_are_rejected(self):
        for patterns in (['/opt/cache'], ['../secret'], ['.'], [''], ['x\0y'], ['a' * 17000]):
            with self.subTest(patterns=str(patterns)[:40]), self.assertRaises(tb.BackupError):
                tb.normalize_exclusions(patterns)


if __name__ == '__main__':
    unittest.main()
