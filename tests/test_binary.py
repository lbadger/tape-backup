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
        self.state = self.root / "state"
        self.media = self.root / "media"
        self.backup_args = ["backup", "--source", self.source, "--state", self.state,
                            "--media-dir", self.media, "--volume-size", "64KiB"]

    def run_binary(self, *args, expected=0):
        result = subprocess.run([str(self.binary), *map(str, args)], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stdout.strip()

    def restore(self, catalogs):
        self.run_binary("restore", "--catalog", *catalogs,
                        "--destination", self.root / "restored", "--work-dir", self.root / "work",
                        "--media-dir", self.media)
        expected = {p.name: p.read_bytes() for p in self.source.iterdir()}
        actual = {p.name: p.read_bytes() for p in (self.root / "restored").iterdir()}
        self.assertEqual(actual, expected)

    def test_relocated_binary_without_python_full_and_incremental_restore(self):
        self.assertEqual(self.run_binary("--version"), "tape-backup 0.1.0")
        self.assertIn("/dev/nst0", self.run_binary("backup", "--help"))
        full = self.run_binary(*self.backup_args)
        self.assertGreater(len(json.loads(Path(full).read_text())["volumes"]), 1)
        (self.source / "change").write_text("delta")
        (self.source / "remove").unlink()
        (self.source / "added").write_text("new")
        delta = self.run_binary(*self.backup_args, "--level", "incremental")
        self.restore([full, delta])
        status = json.loads(self.run_binary("status", "--state", self.state))
        self.assertIsNone(status["pending"])
        self.assertEqual(status["head"], json.loads(Path(delta).read_text())["id"])

    def test_binary_resumes_failed_backup(self):
        wrapper = self.tools / "tar"
        wrapper.unlink()
        wrapper.write_text("#!/bin/sh\n"
                           'if [ "$1" = "--create" ]; then exit 1; fi\n'
                           f'exec {shlex.quote(self.tar)} "$@"\n')
        wrapper.chmod(0o755)
        self.run_binary(*self.backup_args, expected=1)
        status = json.loads(self.run_binary("status", "--state", self.state))
        self.assertIsNotNone(status["pending"])
        self.assertIsNone(status["head"])
        wrapper.unlink()
        wrapper.symlink_to(self.tar)
        catalog = self.run_binary("resume", "--state", self.state, "--media-dir", self.media)
        self.restore([catalog])

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


if __name__ == "__main__":
    unittest.main()
