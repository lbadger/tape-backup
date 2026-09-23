"""Run with TAPE_BACKUP_BINARY=/absolute/path/to/dist/tape-backup."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


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

    def run_binary(self, *args, expected=0):
        result = subprocess.run([str(self.binary), *map(str, args)], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.last_stderr = result.stderr
        return result.stdout.strip()

    def restore(self, backup_ids):
        self.run_binary("restore", "--backup", *backup_ids,
                        "--destination", self.root / "restored",
                        "--media-dir", self.media)
        self.assertIn("./change", self.last_stderr)
        self.assertIn("MiB/s", self.last_stderr)
        self.assertIn("ETA", self.last_stderr)
        expected = {p.name: p.read_bytes() for p in self.source.iterdir()}
        actual = {p.name: p.read_bytes() for p in (self.root / "restored").iterdir()}
        self.assertEqual(actual, expected)

    def test_relocated_binary_without_python_full_and_incremental_restore(self):
        self.assertEqual(self.run_binary("--version"), "tape-backup 0.2.0")
        self.assertIn("/dev/nst0", self.run_binary("backup", "--help"))
        full = self.run_binary(*self.backup_args)
        self.assertIn("./change", self.last_stderr)
        self.assertIn("MiB/s", self.last_stderr)
        self.assertIn("ETA", self.last_stderr)
        self.assertGreater(len(list(self.media.glob(f"{full}.*.tape"))), 1)
        (self.source / "change").write_text("delta")
        (self.source / "remove").unlink()
        (self.source / "added").write_text("new")
        delta = self.run_binary(*self.backup_args, "--level", "incremental", "--base", full)
        self.restore([full, delta])
        info = json.loads(self.run_binary("verify", "--backup", delta, "--media-dir", self.media))
        self.assertEqual(info["parent"], full)
        self.assertTrue(info["data_verified"])
        self.assertFalse(list(self.root.rglob("*.tar")))
        self.assertFalse(list(self.root.rglob("*.snar")))
        self.assertFalse(list(self.root.rglob("*.json")))

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

    def test_binary_still_restores_legacy_tapes(self):
        import legacy_v1
        catalog = legacy_v1.backup(self.root / "legacy-state", self.source, "full",
                                   legacy_v1.FileMedia(self.media), 128 * 1024)
        self.run_binary("legacy-restore", "--catalog", catalog, "--destination", self.root / "restored",
                        "--work-dir", self.root / "legacy-work", "--media-dir", self.media)
        self.assertEqual((self.root / "restored" / "keep").read_bytes(),
                         (self.source / "keep").read_bytes())


if __name__ == "__main__":
    unittest.main()
