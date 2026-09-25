"""Run with TAPE_BACKUP_BINARY=/absolute/path/to/dist/tape-backup."""

import hashlib
import json
import os
import pty
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

import tape_backup as tb


@unittest.skipUnless(os.environ.get("TAPE_BACKUP_BINARY"),
                     "Set TAPE_BACKUP_BINARY to test a built executable")
class BinaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / "tape-backup"
        shutil.copy2(Path(os.environ["TAPE_BACKUP_BINARY"]).resolve(), self.binary)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.tar = str(Path(shutil.which("tar")).resolve())
        (self.tools / "tar").symlink_to(self.tar)
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.tools), PYTHONHOME="/no-external-python",
                        PYTHONPATH="/no-external-python", TAR_OPTIONS="--invalid-option")
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "keep").write_bytes(os.urandom(200_000))
        (self.source / "change").write_text("original")
        (self.source / "remove").write_text("to delete")
        self.media = self.root / "media"
        self.backup_args = ["backup", "--source", self.source, "--media-dir", self.media,
                            "--volume-size", "512KiB", "--buffer-size", "64KiB"]

    def run_binary(self, *args, expected=0, timeout=60):
        result = subprocess.run([str(self.binary), *map(str, args)], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.last_stderr = result.stderr
        return result.stdout.strip()

    def restore(self, backup_ids):
        self.run_binary("restore", "--backup", *backup_ids,
                        "--destination", self.root / "restored",
                        "--media-dir", self.media)
        self.assertIn("./", self.last_stderr)
        self.assertIn("MiB/s", self.last_stderr)
        self.assertIn("ETA", self.last_stderr)
        self.assertIn('Total 100.0% (complete)', self.last_stderr)
        expected = {p.name: p.read_bytes() for p in self.source.iterdir()}
        actual = {p.name: p.read_bytes() for p in (self.root / "restored").iterdir()}
        self.assertEqual(actual, expected)

    def test_relocated_binary_without_python_full_and_incremental_restore(self):
        self.assertEqual(self.run_binary("--version"), "tape-backup 2.3.0")
        self.assertIn("/dev/nst0", self.run_binary("backup", "--help"))
        self.assertNotIn('--volume-size', self.run_binary('backup', '--help'))
        full = self.run_binary(*self.backup_args)
        self.assertIn("./change", self.last_stderr)
        self.assertIn("MiB/s", self.last_stderr)
        self.assertIn("ETA", self.last_stderr)

        self.assertIn('Total 100.0% (complete)', self.last_stderr)
        self.assertGreater(len(list(self.media.glob(f"{full}.*.tape"))), 1)
        names = self.run_binary('list', '--media-dir', self.media).splitlines()
        self.assertEqual(set(names), {'./', './keep', './change', './remove'})
        self.assertIn('complete', self.last_stderr)
        (self.source / "change").write_text("delta")
        (self.source / "remove").unlink()
        (self.source / "added").write_text("new")
        delta = self.run_binary(*self.backup_args, "--level", "incremental", "--base", full)
        names = self.run_binary('list', '--backup', delta, '--media-dir', self.media).splitlines()
        self.assertEqual(set(names), {'./', './change', './added'})
        self.restore([full, delta])
        info = json.loads(self.run_binary("verify", "--backup", delta, "--media-dir", self.media))
        self.assertEqual(info["parent"], full)
        self.assertTrue(info["data_verified"])
        self.assertFalse(list(self.root.rglob("*.tar")))
        self.assertFalse(list(self.root.rglob("*.snar")))
        self.assertEqual(list(self.root.rglob('*.json')),
                         [tb.restore_marker_path(self.root / 'restored')])

    def test_preview_labels_inventory_and_automatic_restore_chain(self):
        inventory = self.root / 'inventory.json'
        preview = json.loads(self.run_binary('backup', '--source', self.source, '--dry-run', '--json'))
        self.assertTrue(preview['dry_run'])
        self.assertFalse(self.media.exists())
        full = self.run_binary(*self.backup_args, '--label-prefix', 'BOOKS', '--inventory', inventory)
        (self.source / 'change').write_text('changed')
        delta = self.run_binary(*self.backup_args, '--level', 'incremental', '--base', full, '--inventory', inventory)
        plan = json.loads(self.run_binary('restore', '--to', delta, '--plan', '--inventory', inventory, '--json'))
        self.assertTrue(plan['plan_complete'])
        self.assertEqual(plan['backup_ids'], [full, delta])
        self.assertTrue(plan['backups'][0]['cartridges'][0]['label'].startswith('BOOKS-'))
        self.run_binary('restore', '--to', delta, '--inventory', inventory,
                        '--media-dir', self.media, '--destination', self.root / 'planned-restore')
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()},
                         {p.name: p.read_bytes() for p in (self.root / 'planned-restore').iterdir()})

    def test_binary_displays_total_bar_for_backup_and_restore_on_a_terminal(self):
        def run(*args):
            master, slave = pty.openpty()
            try:
                result = subprocess.run([str(self.binary), *map(str, args)], cwd=self.root,
                                        env=self.env, stdout=subprocess.PIPE, stderr=slave,
                                        text=True, timeout=30)
                os.close(slave)
                slave = None
                chunks = []
                while True:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    chunks.append(data)
                text = b''.join(chunks).decode()
                self.assertEqual(result.returncode, 0, text)
                self.assertRegex(text, r'Total \[#+\] 100\.0%\s+\(complete\)')
                return result.stdout.strip()
            finally:
                os.close(master)
                if slave is not None:
                    os.close(slave)
        full = run(*self.backup_args, '--quiet')
        run('restore', '--backup', full, '--destination', self.root / 'restored',
            '--media-dir', self.media, '--quiet')

    def test_binary_backup_verify_reaches_complete_only_after_readback(self):
        result = json.loads(self.run_binary(*self.backup_args, '--quiet', '--json', '--verify'))
        before, after = self.last_stderr.split('starting read-back verification', 1)
        self.assertNotIn('100.0%', before)
        self.assertEqual(after.count('Total 100.0% (complete)'), 1)
        self.assertTrue(result['archive_complete'])
        self.assertTrue(result['data_verified'])
        verified = json.loads(self.run_binary('verify', '--backup', result['id'], '--media-dir', self.media))
        self.assertEqual(verified['data_bytes'], result['data_bytes'])
        self.assertIn('Total 100.0% (complete)', self.last_stderr)

    def test_binary_rejects_incomplete_backup_and_can_start_again(self):
        wrapper = self.tools / "tar"
        wrapper.unlink()
        wrapper.write_text("#!/bin/sh\n"
                           'if [ "$1" = "--create" ]; then exit 1; fi\n'
                           f'exec {shlex.quote(self.tar)} "$@"\n')
        wrapper.chmod(0o755)
        self.run_binary(*self.backup_args, expected=1)
        incomplete = next(self.media.glob("*.tape")).name.split(".")[0]
        self.run_binary("verify", "--backup", incomplete, "--media-dir", self.media, expected=1)
        wrapper.unlink()
        wrapper.symlink_to(self.tar)
        backup_id = self.run_binary(*self.backup_args)
        self.restore([backup_id])

    def test_binary_appends_two_incrementals_on_one_cartridge(self):
        args = ['backup', '--source', self.source, '--media-dir', self.media,
                '--volume-size', '20MiB', '--buffer-size', '256KiB', '--quiet']
        full = self.run_binary(*args)
        original_file = self.media / f'{full}.0001.tape'
        original = original_file.read_bytes()
        (self.source / 'change').write_text('first delta')
        (self.source / 'remove').unlink()
        first = self.run_binary(*args, '--level', 'incremental', '--base', full)
        self.assertIn('no archive scan', self.last_stderr)
        (self.source / 'added').write_text('second delta')
        second = self.run_binary(*args, '--level', 'incremental', '--base', first, '--append')
        self.assertEqual(list(self.media.glob('*.tape')), [original_file])
        self.assertEqual(original_file.read_bytes()[:len(original)], original)
        listing = json.loads(self.run_binary('inspect', '--media-dir', self.media))
        self.assertEqual([b['id'] for b in listing['backups']], [full, first, second])
        self.assertTrue(listing['scan_complete'])
        self.assertEqual({b['listing_method'] for b in listing['backups']}, {'catalog'})
        self.assertEqual(json.loads(self.run_binary('inspect', '--all', '--media-dir', self.media)), listing)
        self.assertEqual(json.loads(self.run_binary('inspect', '--first', '--media-dir', self.media))['id'], full)
        self.assertEqual(json.loads(self.run_binary('inspect', '--backup', second,
                                                   '--media-dir', self.media))['parent'], first)
        self.restore([full, first, second])
        info = json.loads(self.run_binary('verify', '--backup', second, '--media-dir', self.media))
        self.assertTrue(info['data_verified'])

    def test_binary_restores_incrementals_across_separate_invocations(self):
        full = self.run_binary(*self.backup_args)
        self.restore([full])
        (self.source / 'change').write_text('first delta')
        (self.source / 'remove').unlink()
        first = self.run_binary(*self.backup_args, '--level', 'incremental', '--base', full)
        self.restore([first])
        (self.source / 'added').write_text('second delta')
        second = self.run_binary(*self.backup_args, '--level', 'incremental', '--base', first)
        self.restore([second])
        self.run_binary('restore', '--backup', first, '--destination', self.root / 'restored',
                        '--media-dir', self.media, expected=1)
        self.assertIn('out-of-order', self.last_stderr)

    def test_binary_adopts_an_older_restore_with_explicit_base(self):
        full = self.run_binary(*self.backup_args)
        self.restore([full])
        tb.restore_marker_path(self.root / 'restored').unlink()
        (self.source / 'change').write_text('incremental')
        delta = self.run_binary(*self.backup_args, '--level', 'incremental', '--base', full)
        self.run_binary('restore', '--backup', delta, '--base', full,
                        '--destination', self.root / 'restored', '--media-dir', self.media)
        self.assertEqual((self.root / 'restored' / 'change').read_text(), 'incremental')

    def test_eject_help_and_device_validation(self):
        self.assertIn('/dev/nst0', self.run_binary('eject', '--help'))
        self.run_binary('eject', '--device', self.source / 'keep', expected=1)
        self.assertIn('tape character device', self.last_stderr)
        self.run_binary('eject', '--media-dir', self.media, expected=2)
        self.assertIn('unrecognized arguments', self.last_stderr)

    def test_wipe_help_rejects_regular_files_and_simulated_media(self):
        help_text = self.run_binary('wipe', '--help')
        for option in ('/dev/nst0', '--long', '--yes'):
            self.assertIn(option, help_text)
        path = self.source / 'keep'
        before = path.read_bytes()
        self.run_binary('wipe', '--device', path, '--yes', expected=1)
        self.assertIn('tape character device', self.last_stderr)
        self.assertEqual(path.read_bytes(), before)
        self.run_binary('wipe', '--media-dir', self.media, '--yes', expected=2)
        self.assertIn('unrecognized arguments', self.last_stderr)

    def test_compression_help_and_invalid_device_and_action(self):
        help_text = self.run_binary('compression', '--help')
        for option in ('/dev/nst0', 'status', 'on', 'off'):
            self.assertIn(option, help_text)
        self.run_binary('compression', 'status', '--device', self.source / 'keep', expected=1)
        self.assertIn('tape character device', self.last_stderr)
        self.run_binary('compression', 'invalid', expected=2)
        self.assertIn('invalid choice', self.last_stderr)
        self.run_binary('compression', 'status', '--media-dir', self.media, expected=2)
        self.assertIn('unrecognized arguments', self.last_stderr)

    def test_inspect_discovers_id_from_header_only_then_verify_rejects_truncation(self):
        full = self.run_binary(*self.backup_args)
        (self.source / 'added').write_text('new')
        delta = self.run_binary(*self.backup_args, '--level', 'incremental', '--base', full)
        for volume in self.media.glob('*.tape'):
            if volume.name != f'{delta}.0001.tape':
                volume.unlink()
        with (self.media / f'{delta}.0001.tape').open('r+b') as first:
            first.truncate(tb.BLOCK_SIZE)
        info = json.loads(self.run_binary('inspect', '--first', '--media-dir', self.media))
        self.assertEqual(info['id'], delta)
        self.assertEqual(info['parent'], full)
        self.assertEqual(info['level'], 'incremental')
        self.assertEqual(info['source'], str(self.source.resolve()))
        self.assertEqual(info['volume'], 1)
        self.assertTrue(info['header_verified'])
        self.assertFalse(info['data_verified'])
        self.assertFalse(info['completion_verified'])
        self.assertNotIn('volumes', info)
        self.run_binary('verify', '--backup', delta, '--media-dir', self.media, expected=1)
        self.assertIn('Incomplete backup', self.last_stderr)

    def test_binary_restores_library_path_for_system_tar(self):
        original = self.root / "original-libraries"
        original.mkdir()
        self.env.update(LD_LIBRARY_PATH=str(original), EXPECTED_LIBRARY_PATH=str(original))
        self.env.pop("LD_LIBRARY_PATH_ORIG", None)
        wrapper = self.tools / "tar"
        wrapper.unlink()
        wrapper.write_text("#!/bin/sh\n"
                           '[ "${LD_LIBRARY_PATH-}" = "$EXPECTED_LIBRARY_PATH" ] || exit 99\n'
                           f'exec {shlex.quote(self.tar)} "$@"\n')
        wrapper.chmod(0o755)
        catalog = self.run_binary(*self.backup_args)
        self.restore([catalog])

    def test_binary_quiet_keeps_rates_and_eta(self):
        full = self.run_binary(*self.backup_args, "--quiet")
        self.assertNotIn("./change", self.last_stderr)
        self.assertIn("MiB/s", self.last_stderr)
        self.assertIn("ETA", self.last_stderr)
        self.run_binary("restore", "--backup", full, "--destination", self.root / "quiet-restored",
                        "--media-dir", self.media, "--quiet")
        self.assertNotIn("./change", self.last_stderr)
        self.assertIn("MiB/s", self.last_stderr)
        self.assertIn("ETA", self.last_stderr)

    def test_binary_exclusions_and_structured_verified_summary(self):
        info = json.loads(self.run_binary(*self.backup_args, '--exclude', 'remove', '--json', '--verify'))
        self.assertTrue(info['archive_complete'])
        self.assertTrue(info['data_verified'])
        names = self.run_binary('list', '--backup', info['id'], '--media-dir', self.media).splitlines()
        self.assertNotIn('./remove', names)
        self.assertIn('./keep', names)
        self.assertIn('Native ZFS', self.run_binary())
        self.assertIn('--snapshot', self.run_binary('help', 'zfs-backup'))

    def test_binary_default_buffer_and_10_gib_limit(self):
        args = ["backup", "--source", self.source, "--media-dir", self.media,
                "--volume-size", "20GiB"]
        self.run_binary(*args)
        self.assertIn("buffer 1024 MiB", self.last_stderr)
        full = self.run_binary(*args, "--buffer-size", "10GiB")
        self.assertIn("buffer 10240 MiB", self.last_stderr)
        self.restore([full])
        self.run_binary(*args, "--buffer-size", str(10 * 1024**3 + 1), expected=1)
        self.assertIn("64KiB and 10GiB", self.last_stderr)

    def test_binary_exclusion_matching_and_readable_info(self):
        excluded = self.source / '1cache'
        excluded.mkdir()
        (excluded / 'secret').write_text('excluded')
        excluded.chmod(0)
        self.addCleanup(excluded.chmod, 0o700)
        full = self.run_binary(*self.backup_args, '--exclude', '[[:digit:]]*')
        listing = self.run_binary('list', '--backup', full, '--media-dir', self.media)
        self.assertNotIn('1cache', listing)
        text = self.run_binary('info', '--backup', full, '--media-dir', self.media, '--text')
        self.assertIn(full, text)
        self.assertIn('Backup metadata', text)
        self.assertIn('Not verified; run verify', text)
        result = json.loads(self.run_binary('info', '--backup', full, '--media-dir', self.media, '--json'))
        self.assertFalse(result['data_verified'])

    @unittest.skipUnless(os.environ.get("TAPE_BACKUP_LARGE_TEST"),
                         "Set TAPE_BACKUP_LARGE_TEST=1 for a 10 GiB streaming round trip")
    def test_full_10_gib_stream_round_trip(self):
        # Requires about 31 GiB of disk; frames stay small even with a 10 GiB budget.
        if shutil.disk_usage(self.root).free < 31 * 1024**3:
            self.skipTest('Need 31 GiB free for the large test; set TMPDIR to a disk-backed directory')
        expected = hashlib.sha256()
        block = b'x' * 1024**2
        with (self.source / 'large').open('wb') as stream:
            for _ in range(10 * 1024):
                stream.write(block)
                expected.update(block)
        full = self.run_binary('backup', '--source', self.source, '--media-dir', self.media,
                               '--volume-size', '20GiB', '--buffer-size', '10GiB', '--quiet', timeout=300)
        with (self.media / f'{full}.0001.tape').open('rb') as stream:
            stream.read(tb.BLOCK_SIZE)  # Volume header.
            head = tb.decoded_header(stream.read(tb.BLOCK_SIZE))
        self.assertEqual(head['length'], tb.FRAME_SIZE)
        info = json.loads(self.run_binary('verify', '--backup', full, '--media-dir', self.media, timeout=300))
        self.assertTrue(info['data_verified'])
        destination = self.root / 'large-restored'
        self.run_binary('restore', '--backup', full, '--media-dir', self.media,
                        '--destination', destination, '--quiet', timeout=300)
        with (destination / 'large').open('rb') as stream:
            self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), expected.hexdigest())
        for name in ('keep', 'change', 'remove'):
            self.assertEqual((destination / name).read_bytes(), (self.source / name).read_bytes())



if __name__ == "__main__":
    unittest.main()
