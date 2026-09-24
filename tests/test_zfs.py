"""Native stream framing plus opt-in OpenZFS kernel integration tests."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import tape_backup as tb
from ssh_fixture import SSHServer
from test_tape_backup import tree_contents


def identity(snapshot='tank/books@one', guid='1001', parent=None, raw=False):
    return {'snapshot': snapshot, 'dataset': snapshot.split('@')[0], 'guid': guid,
            'base_snapshot': parent['snapshot'] if parent else None,
            'base_guid': parent['guid'] if parent else None, 'raw': raw}


class ZFSStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = tb.FileMedia(self.root / 'media')
        capture = redirect_stderr(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def create(self, metadata, base=None, limit=None, fail=False):
        def source(value):
            self.assertEqual(value, metadata)
            return tb.logged_process([sys.executable, '-c',
                'import sys; sys.stdout.buffer.write(b"test zfs payload" * 50000); '
                f'sys.exit({1 if fail else 0})'], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        with patch.object(tb, 'prepare_zfs', return_value={'source': metadata['dataset'],
                'estimated_bytes': 800000, 'zfs': metadata}), patch.object(tb, 'start_zfs', source):
            return tb.backup_zfs(metadata['snapshot'], self.media, base=base,
                                 buffer_size=tb.BLOCK_SIZE, volume_size=limit, quiet=True)

    def test_full_and_incremental_multivolume_streams_verify(self):
        first = identity()
        full = self.create(first, limit=512 * 1024)
        second = identity('tank/books@two', '1002', first)
        delta = self.create(second, base=full, limit=512 * 1024)
        for backup_id, expected in ((full, first), (delta, second)):
            info = tb.scan(self.media, backup_id)
            self.assertTrue(info['data_verified'])
            self.assertGreater(info['volumes'], 1)
            self.assertEqual(info['zfs'], expected)
            self.assertEqual(info['archive_type'], 'zfs')
        first_tape = self.media.directory / f'{full}.0001.tape'
        self.assertTrue(first_tape.read_bytes().startswith(tb.ZFS_MAGIC))

    def test_send_failure_never_commits_a_complete_backup(self):
        with self.assertRaisesRegex(tb.BackupError, 'ZFS send failed'):
            self.create(identity(), fail=True)
        backup_id = next(self.media.directory.glob('*.tape')).name.split('.')[0]
        with self.assertRaises(tb.BackupError):
            tb.scan(self.media, backup_id)

    def test_tar_listing_and_restore_reject_native_stream_before_spawning_tar(self):
        full = self.create(identity())
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(tb.BackupError, 'ZFS streams'):
            tb.list_files(self.media, full)
        with patch.object(tb, 'extract_restore_stream', side_effect=AssertionError('Tar was started')):
            with self.assertRaisesRegex(tb.BackupError, 'native ZFS'):
                tb.restore([full], self.root / 'restored', self.media)
        self.assertFalse((self.root / 'restored').exists())

    def test_zfs_parent_cannot_be_used_for_a_tar_incremental(self):
        full = self.create(identity())
        source = self.root / 'source'
        source.mkdir()
        before = tree_contents(self.media.directory)
        with self.assertRaisesRegex(tb.BackupError, 'tar parent'):
            tb.backup(source, self.media, level='incremental', base=full)
        self.assertEqual(before, tree_contents(self.media.directory))

    def test_encryption_requires_explicit_raw_and_base_guid_cannot_change(self):
        metadata = identity()
        def properties(name, keys):
            if '@' not in name:
                return {'type': 'filesystem', 'encryption': 'aes-256-gcm'}
            return {'type': 'snapshot', 'guid': '9999'}
        with patch.object(tb.shutil, 'which', return_value='/zfs'), patch.object(tb, 'zfs_properties', properties):
            with self.assertRaisesRegex(tb.BackupError, 'require --raw'):
                tb.prepare_zfs('tank/books@one')
            metadata['raw'] = True
            with self.assertRaisesRegex(tb.BackupError, 'base snapshot changed'):
                tb.prepare_zfs('tank/books@two', metadata, raw=True)

    def test_zfs_commands_never_force_receive_or_use_shell_interpolation(self):
        parent = identity(raw=True)
        metadata = identity('tank/books@two', '1002', parent, raw=True)
        self.assertEqual(tb.zfs_send_command(metadata),
                         ['zfs', 'send', '-p', '-w', '-i', 'tank/books@one', 'tank/books@two'])
        for name in ('-danger@snap', 'tank/fs;touch x@snap', 'tank/../fs@snap', 'tank/fs'):
            with self.subTest(name=name), self.assertRaises(tb.BackupError):
                tb.zfs_name(name, snapshot=True)

    def test_zfs_restore_uses_a_shared_destination_lock_across_homes(self):
        target = 'tank/recovered'
        with tb.restore_lock(target, zfs=True), \
                patch.object(Path, 'home', return_value=self.root / 'other-home'), \
                patch.object(tb.shutil, 'which', return_value='/zfs'), \
                patch.object(tb, 'zfs_target_exists', side_effect=AssertionError('Touched locked destination')):
            with self.assertRaisesRegex(tb.BackupError, 'Another operation is restoring'):
                tb.restore_zfs(['a' * 32], target, self.media)


class ZFSSSHTests(unittest.TestCase):
    def test_real_ssh_transport_streams_native_full_and_incremental_sources(self):
        # The SSH and framing are real; this controlled executable models the
        # OpenZFS subprocess interface. Kernel behavior is covered separately.
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            root = Path(temporary)
            server = SSHServer()
            self.addCleanup(server.close)
            tools = root / 'tools'
            tools.mkdir()
            fake = tools / 'zfs'
            fake.write_text('#!' + sys.executable + '''
import sys
args = sys.argv[1:]
if args[0] == 'get':
    properties, name = args[-2:]
    values = {'type': 'snapshot' if '@' in name else 'filesystem',
              'encryption': 'off', 'guid': '1002' if name.endswith('@two') else '1001'}
    for prop in properties.split(','):
        print(prop + '\\t' + values[prop])
elif args[0] == 'send' and '-nP' in args:
    print('size\\t800000')
elif args[0] == 'send':
    sys.stdout.buffer.write(b'native SSH stream' * 50000)
else:
    sys.exit(2)
''')
            fake.chmod(0o700)
            helper = root / 'remote helper'
            helper.write_text('#!/bin/sh\nexec env ' + shlex.quote('PATH=' + str(tools) + ':' + os.environ['PATH']) +
                ' ' + shlex.join([sys.executable, str(Path(tb.__file__).resolve())]) + ' "$@"\n')
            helper.chmod(0o700)
            ssh = tb.SSHConfig('backup-source', config=server.config, program=str(helper))
            media = tb.FileMedia(root / 'media')
            full = tb.backup_zfs('tank/books@one', media, ssh=ssh, buffer_size=tb.BLOCK_SIZE,
                                 volume_size=512 * 1024)
            delta = tb.backup_zfs('tank/books@two', media, ssh=ssh, base=full,
                                  buffer_size=tb.BLOCK_SIZE, volume_size=512 * 1024)
            info = tb.scan(media, delta)
            self.assertTrue(info['data_verified'])
            self.assertEqual(info['parent'], full)
            self.assertEqual(info['zfs']['base_guid'], '1001')
            self.assertEqual(info['zfs']['guid'], '1002')
            self.assertEqual(info['ssh']['host'], 'backup-source')


@unittest.skipUnless(os.environ.get('TAPE_BACKUP_ZFS_TEST_POOL'),
                     'Set TAPE_BACKUP_ZFS_TEST_POOL to a dedicated scratch pool for real ZFS tests')
class ZFSKernelTests(unittest.TestCase):
    def setUp(self):
        pool = os.environ['TAPE_BACKUP_ZFS_TEST_POOL']
        tb.zfs_name(pool)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = pool + '/tape-backup-test-' + uuid.uuid4().hex
        subprocess.run(['zfs', 'create', '-o', 'mountpoint=none', self.dataset], check=True)
        self.addCleanup(subprocess.run, ['zfs', 'destroy', '-r', self.dataset], check=True)
        self.source_dataset = self.dataset + '/source'
        self.source = self.root / 'source'
        subprocess.run(['zfs', 'create', '-o', f'mountpoint={self.source}', '-o', 'compression=lz4',
                        self.source_dataset], check=True)
        (self.source / 'book').write_bytes(os.urandom(700_000))
        (self.source / 'deleted').write_text('original')
        (self.source / 'empty').mkdir()
        (self.source / 'link').symlink_to('book')
        self.media = tb.FileMedia(self.root / 'media')

    def snapshot(self, name):
        snapshot = self.source_dataset + '@' + name
        subprocess.run(['zfs', 'snapshot', snapshot], check=True)
        return snapshot

    def view(self, dataset, name):
        path = self.root / name
        subprocess.run(['zfs', 'set', f'mountpoint={path}', 'canmount=on', dataset], check=True)
        if tb.zfs_properties(dataset, ['mounted'])['mounted'] != 'yes':
            subprocess.run(['zfs', 'mount', dataset], check=True)
        return path

    def test_real_full_incremental_and_stepwise_receive_across_volumes(self):
        full = tb.backup_zfs(self.snapshot('one'), self.media, buffer_size=tb.BLOCK_SIZE,
                             volume_size=512 * 1024)
        self.assertTrue(tb.scan(self.media, full)['data_verified'])
        first_tree = tree_contents(self.source)
        (self.source / 'book').write_text('changed')
        (self.source / 'deleted').unlink()
        (self.source / 'added').write_text('new')
        delta = tb.backup_zfs(self.snapshot('two'), self.media, base=full, buffer_size=tb.BLOCK_SIZE,
                              volume_size=512 * 1024)
        destination = self.dataset + '/all'
        tb.restore_zfs([full, delta], destination, self.media)
        self.assertEqual(tree_contents(self.view(destination, 'all')), tree_contents(self.source))
        steps = self.dataset + '/steps'
        tb.restore_zfs([full], steps, self.media)
        self.assertEqual(tree_contents(self.view(steps, 'steps')), first_tree)
        subprocess.run(['zfs', 'unmount', steps], check=True)
        tb.restore_zfs([delta], steps, self.media)
        self.assertEqual(tree_contents(self.view(steps, 'steps')), tree_contents(self.source))
        with self.assertRaises(tb.BackupError):
            tb.restore_zfs([full], destination, self.media)

    def test_real_raw_encrypted_snapshot_round_trip(self):
        encrypted = self.dataset + '/encrypted'
        key = self.root / 'key'
        key.write_text(uuid.uuid4().hex)
        source = self.root / 'encrypted'
        subprocess.run(['zfs', 'create', '-o', 'encryption=on', '-o', 'keyformat=passphrase',
                        '-o', f'keylocation=file://{key}', '-o', f'mountpoint={source}', encrypted], check=True)
        (source / 'secret').write_text('encrypted payload')
        snapshot = encrypted + '@one'
        subprocess.run(['zfs', 'snapshot', snapshot], check=True)
        with self.assertRaisesRegex(tb.BackupError, '--raw'):
            tb.backup_zfs(snapshot, self.media)
        full = tb.backup_zfs(snapshot, self.media, raw=True, buffer_size=tb.BLOCK_SIZE)
        destination = self.dataset + '/raw-restored'
        tb.restore_zfs([full], destination, self.media)
        self.assertNotEqual(tb.zfs_properties(destination, ['encryption'])['encryption'], 'off')
        subprocess.run(['zfs', 'load-key', '-L', f'file://{key}', destination], check=True)
        self.assertEqual(tree_contents(self.view(destination, 'raw-restored')), tree_contents(source))

    @unittest.skipUnless(os.environ.get('TAPE_BACKUP_BINARY'), 'Set TAPE_BACKUP_BINARY for native ZFS binary tests')
    def test_real_standalone_binary_zfs_full_and_incremental_restore(self):
        binary = str(Path(os.environ['TAPE_BACKUP_BINARY']).resolve())
        def run(*args):
            result = subprocess.run([binary, *map(str, args)], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()
        common = ['--media-dir', self.media.directory, '--buffer-size', '64KiB', '--volume-size', '512KiB']
        full = json.loads(run('zfs-backup', '--snapshot', self.snapshot('one'), *common, '--json', '--verify'))
        self.assertTrue(full['data_verified'])
        (self.source / 'deleted').unlink()
        (self.source / 'new').write_text('binary incremental')
        delta = run('zfs-backup', '--snapshot', self.snapshot('two'), '--base', full['id'], *common)
        target = self.dataset + '/binary-restored'
        run('zfs-restore', '--backup', full['id'], delta, '--dataset', target,
            '--media-dir', self.media.directory)
        self.assertEqual(tree_contents(self.view(target, 'binary-restored')), tree_contents(self.source))


if __name__ == '__main__':
    unittest.main()
