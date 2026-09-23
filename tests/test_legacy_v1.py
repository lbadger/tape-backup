"""Integration tests use real GNU tar and record-oriented fault-injected media."""

from contextlib import contextmanager, redirect_stderr
import errno
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import legacy_v1 as tb


class FaultMedia(tb.FileMedia):
    def __init__(self, directory, *, capacity=None, short=False, fail_volume=None,
                 close_loss=0, close_error=False, corrupt=False):
        super().__init__(directory)
        self.capacity = capacity
        self.short = short
        self.fail_volume = fail_volume
        self.close_loss = close_loss
        self.close_error = close_error
        self.corrupt = corrupt
        self.loads = []

    def load(self, job, number, writing):
        super().load(job, number, writing)
        self.number = number
        self.loads.append((job["id"], number, writing))

    @contextmanager
    def writer(self):
        owner = self
        with super().writer() as stream:
            class Writer:
                def write(self, data):
                    if owner.fail_volume == owner.number and stream.tell() >= tb.BLOCK_SIZE:
                        raise OSError(errno.EIO, "Injected drive failure")
                    if owner.capacity is not None and stream.tell() + len(data) > owner.capacity:
                        left = max(0, owner.capacity - stream.tell())
                        if left:
                            stream.write(data[:left])
                        if owner.short:
                            return left
                        raise OSError(errno.ENOSPC, "Injected end of tape")
                    return stream.write(data)

            yield Writer()
        if self.number == 1 and self.close_error:
            if self.close_loss:
                with open(self.path, "r+b") as stream:
                    stream.truncate(self.path.stat().st_size - self.close_loss)
            raise OSError(errno.ENOSPC, "Injected delayed close failure")

    def reader(self):
        if self.corrupt:
            with open(self.path, "r+b") as stream:
                stream.seek(tb.BLOCK_SIZE + 100)
                byte = stream.read(1)
                stream.seek(-1, os.SEEK_CUR)
                stream.write(bytes([byte[0] ^ 0xFF]))
        return super().reader()


def tree_contents(root):
    result = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            result[name] = ("link", os.readlink(path))
        elif path.is_dir():
            result[name] = ("dir", stat.S_IMODE(path.stat().st_mode))
        else:
            result[name] = ("file", path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return result


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.state = self.root / "state"
        self.media_dir = self.root / "media"
        self.media = FaultMedia(self.media_dir)
        self.destination = self.root / "restored"
        self.work = self.root / "restore-work"
        self.quiet = redirect_stderr(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        # Keep tests from flushing unrelated host filesystems. Production uses
        # os.sync only when publishing a successfully extracted restore tree.
        self.sync = patch.object(tb.os, "sync")
        self.sync.start()
        self.addCleanup(self.sync.stop)

    def create(self, level="full", media=None, size=128 * 1024):
        return tb.backup(self.state, self.source, level, media or self.media, size)

    def extract(self, catalogs, media=None, destination=None, work=None):
        return tb.restore(catalogs, destination or self.destination,
                          work or self.work, media or self.media)

    def seed(self):
        (self.source / "unchanged").write_bytes(os.urandom(310_000))
        (self.source / "changed").write_text("original\n")
        (self.source / "deleted").write_text("delete me")
        (self.source / "old-dir").mkdir()
        (self.source / "old-dir" / "child").write_text("child")
        (self.source / "link").symlink_to("unchanged")
        (self.source / "executable").write_text("#!/bin/sh\nexit 0\n")
        (self.source / "executable").chmod(0o751)
        os.link(self.source / "executable", self.source / "hardlink")
        (self.source / "space and\nnewline").write_text("odd filename")
        (self.source / "empty-dir").mkdir()

    def test_full_backup_spans_volumes_and_restores_metadata(self):
        self.seed()
        catalog = self.create()
        job = tb.read_json(catalog)
        self.assertGreater(len(job["volumes"]), 2)
        self.assertEqual(job["status"], "complete")
        self.extract([catalog])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))
        self.assertEqual((self.destination / "executable").stat().st_ino,
                         (self.destination / "hardlink").stat().st_ino)
        self.assertEqual((self.source / "changed").stat().st_mtime_ns,
                         (self.destination / "changed").stat().st_mtime_ns)

    def test_full_and_two_incremental_deltas_restore_exact_tree(self):
        self.seed()
        full = self.create()
        (self.source / "changed").write_text("first change\n")
        (self.source / "deleted").unlink()
        (self.source / "added").write_text("new file")
        (self.source / "old-dir").rename(self.source / "renamed-dir")
        first = self.create("incremental")
        with tarfile.open(first.parent / "archive.tar") as archive:
            names = archive.getnames()
        self.assertIn("./changed", names)
        self.assertIn("./added", names)
        self.assertNotIn("./unchanged", names)
        self.assertNotIn("./deleted", names)
        shutil.rmtree(self.source / "renamed-dir")
        (self.source / "changed").write_text("second change\n")
        (self.source / "added").unlink()
        (self.source / "final").write_bytes(os.urandom(170_000))
        second = self.create("incremental")
        self.assertLess(tb.read_json(first)["archive_size"], tb.read_json(full)["archive_size"])
        self.extract([full, first, second])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_empty_and_unchanged_incremental(self):
        full = self.create()
        delta = self.create("incremental")
        self.extract([full, delta])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_sparse_file_and_xattrs(self):
        path = self.source / "sparse"
        with open(path, "wb") as stream:
            stream.write(b"start")
            stream.seek(4 * 1024 * 1024)
            stream.write(b"end")
        os.setxattr(path, "user.backup-test", b"preserved")
        catalog = self.create()
        self.extract([catalog])
        restored = self.destination / "sparse"
        self.assertEqual(path.read_bytes(), restored.read_bytes())
        self.assertEqual(os.getxattr(restored, "user.backup-test"), b"preserved")
        self.assertLess(restored.stat().st_blocks * 512, restored.stat().st_size)

    def test_enospc_and_short_writes_roll_over_without_data_loss(self):
        self.seed()
        for short in (False, True):
            with self.subTest(short=short):
                media = FaultMedia(self.media_dir / str(short),
                                   capacity=3 * tb.BLOCK_SIZE + 100, short=short)
                state = self.root / f"state-{short}"
                catalog = tb.backup(state, self.source, "full", media)
                job = tb.read_json(catalog)
                self.assertGreater(len(job["volumes"]), 1)
                self.assertEqual(job["volumes"][0]["size"], 2 * tb.BLOCK_SIZE)
                self.extract([catalog], media, self.root / f"dest-{short}",
                             self.root / f"work-{short}")
                self.assertEqual(tree_contents(self.source),
                                 tree_contents(self.root / f"dest-{short}"))

    def test_delayed_close_enospc_replays_lost_buffered_tail(self):
        self.seed()
        media = FaultMedia(self.media_dir, close_error=True, close_loss=tb.BLOCK_SIZE)
        catalog = self.create(media=media, size=3 * tb.BLOCK_SIZE)
        job = tb.read_json(catalog)
        self.assertEqual(job["volumes"][0]["size"], 2 * tb.BLOCK_SIZE)
        self.extract([catalog])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_delayed_close_error_with_all_data_durable(self):
        (self.source / "small").write_text("small")
        catalog = self.create(media=FaultMedia(self.media_dir, close_error=True))
        self.assertEqual(len(tb.read_json(catalog)["volumes"]), 1)
        self.extract([catalog])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_failure_resumes_from_last_verified_volume(self):
        self.seed()
        broken = FaultMedia(self.media_dir, fail_volume=2)
        with self.assertRaises(OSError):
            self.create(media=broken)
        index = tb.index_for(self.state)
        self.assertIsNone(index["head"])
        pending = tb.job_path(self.state, index["pending"]) / "catalog.json"
        self.assertEqual(len(tb.read_json(pending)["volumes"]), 1)
        first = self.media_dir / f"{index['pending']}.0001.tape"
        first_digest = tb.digest_file(first)
        catalog = tb.resume(self.state, self.media)
        self.assertEqual(self.media.loads[0][1], 2)
        self.assertEqual(tb.digest_file(first), first_digest)
        self.extract([catalog])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_resume_uses_staged_archive_when_source_is_unavailable(self):
        self.seed()
        expected = tree_contents(self.source)
        with self.assertRaises(OSError):
            self.create(media=FaultMedia(self.media_dir, fail_volume=2))
        shutil.rmtree(self.source)
        catalog = tb.resume(self.state, self.media)
        self.extract([catalog])
        self.assertEqual(expected, tree_contents(self.destination))

    def test_failed_incremental_does_not_advance_snapshot(self):
        self.seed()
        full = self.create()
        snapshot_before = (full.parent / "snapshot.snar").read_bytes()
        (self.source / "changed").write_text("included in staged delta")
        with self.assertRaises(OSError):
            self.create("incremental", FaultMedia(self.media_dir, fail_volume=1))
        self.assertEqual(tb.index_for(self.state)["head"], tb.read_json(full)["id"])
        self.assertEqual((full.parent / "snapshot.snar").read_bytes(), snapshot_before)
        (self.source / "after-failure").write_text("must be in next delta")
        first = tb.resume(self.state, self.media)
        with tarfile.open(first.parent / "archive.tar") as archive:
            self.assertNotIn("./after-failure", archive.getnames())
        second = self.create("incremental")
        self.extract([full, first, second])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_interrupted_tar_creation_restarts_from_committed_snapshot(self):
        self.seed()
        full = self.create()
        (self.source / "changed").write_text("a change")
        real_run = tb.run_command

        def fail_tar(command):
            output = real_run(command)
            if "--create" in command:
                raise tb.BackupError("Injected tar failure after updating working snapshot")
            return output

        with patch.object(tb, "run_command", side_effect=fail_tar):
            with self.assertRaises(tb.BackupError):
                self.create("incremental")
        (self.source / "later").write_text("added before retry")
        delta = tb.resume(self.state, self.media)
        with tarfile.open(delta.parent / "archive.tar") as archive:
            self.assertIn("./changed", archive.getnames())
            self.assertIn("./later", archive.getnames())
        self.extract([full, delta])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_interrupted_final_commit_is_idempotent(self):
        self.seed()
        real_save = tb.atomic_json

        def fail_final(path, data):
            if path.name == "index.json" and data.get("head"):
                raise OSError(errno.EIO, "Injected checkpoint failure")
            real_save(path, data)

        with patch.object(tb, "atomic_json", side_effect=fail_final):
            with self.assertRaises(OSError):
                self.create()
        self.media.loads.clear()
        catalog = tb.resume(self.state, self.media)
        self.assertEqual(self.media.loads, [])
        self.assertIsNone(tb.index_for(self.state)["pending"])
        self.extract([catalog])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_interrupted_volume_checkpoint_replays_only_uncommitted_volume(self):
        self.seed()
        real_save = tb.atomic_json

        def fail_second_volume(path, data):
            if path.name == "catalog.json" and len(data.get("volumes", [])) == 2:
                raise OSError(errno.EIO, "Injected checkpoint failure")
            real_save(path, data)

        with patch.object(tb, "atomic_json", side_effect=fail_second_volume):
            with self.assertRaises(OSError):
                self.create()
        self.media.loads.clear()
        catalog = tb.resume(self.state, self.media)
        self.assertEqual(self.media.loads[0][1], 2)
        self.extract([catalog])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_corrupt_readback_never_commits_volume(self):
        self.seed()
        with self.assertRaisesRegex(tb.BackupError, "verification"):
            self.create(media=FaultMedia(self.media_dir, corrupt=True))
        index = tb.index_for(self.state)
        self.assertIsNone(index["head"])
        job = tb.read_json(tb.job_path(self.state, index["pending"]) / "catalog.json")
        self.assertEqual(job["volumes"], [])
        tb.resume(self.state, self.media)

    def test_zero_progress_stops_instead_of_requesting_tapes_forever(self):
        self.seed()
        with self.assertRaisesRegex(tb.BackupError, "no complete data"):
            self.create(media=FaultMedia(self.media_dir, capacity=tb.BLOCK_SIZE))
        self.assertIsNone(tb.index_for(self.state)["head"])

    def test_corrupt_staged_archive_refuses_resume(self):
        self.seed()
        with self.assertRaises(OSError):
            self.create(media=FaultMedia(self.media_dir, fail_volume=2))
        pending = tb.index_for(self.state)["pending"]
        with open(tb.job_path(self.state, pending) / "archive.tar", "ab") as stream:
            stream.write(b"corruption")
        with self.assertRaisesRegex(tb.BackupError, "corrupt"):
            tb.resume(self.state, self.media)

    def test_missing_or_corrupt_tape_preserves_destination_and_restore_resumes(self):
        self.seed()
        catalog = self.create()
        job = tb.read_json(catalog)
        missing = self.media_dir / f"{job['id']}.0002.tape"
        original = missing.read_bytes()
        missing.unlink()
        with self.assertRaises(OSError):
            self.extract([catalog])
        self.assertFalse(self.destination.exists())
        missing.write_bytes(original)
        self.media.loads.clear()
        self.extract([catalog])
        self.assertEqual(self.media.loads[0][1], 2)
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_corrupt_and_wrong_tapes_are_rejected_before_extraction(self):
        self.seed()
        catalog = self.create()
        job = tb.read_json(catalog)
        volume = self.media_dir / f"{job['id']}.0001.tape"
        original = volume.read_bytes()
        for location, message in [(tb.BLOCK_SIZE + 100, "Checksum"), (0, "Wrong or corrupt")]:
            with self.subTest(location=location):
                damaged = bytearray(original)
                damaged[location] ^= 0xFF
                volume.write_bytes(damaged)
                with self.assertRaisesRegex(tb.BackupError, message):
                    self.extract([catalog])
                self.assertFalse(self.destination.exists())
        volume.write_bytes(original)
        self.extract([catalog])

    def test_truncated_tape_is_rejected(self):
        self.seed()
        catalog = self.create()
        volume = self.media_dir / f"{tb.read_json(catalog)['id']}.0001.tape"
        with open(volume, "r+b") as stream:
            stream.truncate(tb.BLOCK_SIZE + 100)
        with self.assertRaisesRegex(tb.BackupError, "Truncated"):
            self.extract([catalog])
        self.assertFalse(self.destination.exists())

    def test_extraction_failure_replays_chain_in_private_tree(self):
        self.seed()
        full = self.create()
        (self.source / "changed").write_text("new")
        delta = self.create("incremental")
        real_run = tb.run_command
        extracts = 0

        def fail_extract(command):
            nonlocal extracts
            if "--extract" in command:
                extracts += 1
                if extracts == 2:
                    raise tb.BackupError("Injected extraction failure")
            return real_run(command)

        with patch.object(tb, "run_command", side_effect=fail_extract):
            with self.assertRaisesRegex(tb.BackupError, "Injected"):
                self.extract([full, delta])
        self.assertFalse(self.destination.exists())
        self.media.loads.clear()
        self.extract([full, delta])
        self.assertEqual(self.media.loads, [])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))

    def test_interrupted_restore_publication_is_idempotent(self):
        self.seed()
        catalog = self.create()
        real_save = tb.atomic_json

        def fail_done(path, data):
            if path.name == "restore.json" and data.get("phase") == "done":
                raise OSError(errno.EIO, "Injected final restore checkpoint failure")
            real_save(path, data)

        with patch.object(tb, "atomic_json", side_effect=fail_done):
            with self.assertRaises(OSError):
                self.extract([catalog])
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))
        self.media.loads.clear()
        self.extract([catalog])
        self.assertEqual(self.media.loads, [])

    def test_invalid_volume_offsets_and_incomplete_catalog_are_rejected(self):
        self.seed()
        catalog = self.create()
        job = tb.read_json(catalog)
        job["volumes"][1]["offset"] += 1
        tb.atomic_json(catalog, job)
        with self.assertRaisesRegex(tb.BackupError, "volumes"):
            self.extract([catalog])
        job["status"] = "ready"
        tb.atomic_json(catalog, job)
        with self.assertRaisesRegex(tb.BackupError, "incomplete"):
            self.extract([catalog])

    def test_chain_order_and_missing_parent_rejected(self):
        self.seed()
        full = self.create()
        first = self.create("incremental")
        second = self.create("incremental")
        for chain in ([first], [first, full], [full, second], [full, first, first]):
            with self.subTest(chain=chain), self.assertRaises(tb.BackupError):
                self.extract(chain)
        self.assertFalse(self.destination.exists())

    def test_source_mismatch_and_nested_state_rejected(self):
        self.seed()
        self.create()
        other = self.root / "other-source"
        other.mkdir()
        with self.assertRaisesRegex(tb.BackupError, "source differs"):
            tb.backup(self.state, other, "incremental", self.media)
        with self.assertRaisesRegex(tb.BackupError, "separate"):
            tb.backup(self.source / "state", self.source, "full", self.media)

    def test_new_incremental_without_full_and_concurrent_backup_rejected(self):
        with self.assertRaisesRegex(tb.BackupError, "requires"):
            self.create("incremental")
        with tb.locked(self.state), self.assertRaisesRegex(tb.BackupError, "Another operation"):
            self.create()

    def test_restore_does_not_overwrite_existing_files(self):
        self.seed()
        catalog = self.create()
        self.destination.mkdir()
        (self.destination / "keep").write_text("untouched")
        with self.assertRaisesRegex(tb.BackupError, "empty directory"):
            self.extract([catalog])
        self.assertEqual((self.destination / "keep").read_text(), "untouched")

    def test_device_default_override_and_mutual_exclusion(self):
        parser = tb.make_parser()
        base = ["backup", "--state", "state", "--source", "source"]
        self.assertEqual(parser.parse_args(base).device, "/dev/nst0")
        self.assertEqual(parser.parse_args(base + ["--device", "/dev/nst1"]).device, "/dev/nst1")
        with self.assertRaises(SystemExit):
            parser.parse_args(base + ["--device", "/dev/nst1", "--media-dir", "media"])
        for command in ("resume", "restore"):
            args = [command, "--state", "state"] if command == "resume" else [
                command, "--catalog", "catalog", "--destination", "dest", "--work-dir", "work"]
            self.assertEqual(parser.parse_args(args + ["--device", "/dev/nst2"]).device, "/dev/nst2")

    def test_external_commands_receive_original_library_path_in_binary(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        for frozen, original in [(True, None), (True, "/opt/system-libs"),
                                 (True, ""), (False, None)]:
            with self.subTest(frozen=frozen, original=original):
                env = {"LD_LIBRARY_PATH": "/tmp/_MEI-bundled-libs", "TAR_OPTIONS": "--invalid"}
                if original is not None:
                    env["LD_LIBRARY_PATH_ORIG"] = original
                with patch.dict(tb.os.environ, env, clear=True), \
                        patch.object(tb.sys, "frozen", frozen, create=True), \
                        patch.object(tb.subprocess, "run", return_value=completed) as run:
                    tb.run_command(["tar", "--version"])
                    passed = run.call_args.kwargs["env"]
                    self.assertNotIn("TAR_OPTIONS", passed)
                    self.assertEqual(passed.get("LD_LIBRARY_PATH"),
                                     original if frozen else env["LD_LIBRARY_PATH"])
                    self.assertNotIn("LD_LIBRARY_PATH_ORIG", passed)
                    self.assertEqual(tb.os.environ["LD_LIBRARY_PATH"], env["LD_LIBRARY_PATH"])

    def test_tape_adapter_loads_selected_drive_and_rewinds_for_verification(self):
        info = SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=os.makedev(9, 129))
        with patch.object(tb.os, "stat", return_value=info), patch.object(tb.shutil, "which", return_value="/bin/mt"):
            media = tb.TapeMedia("/dev/nst1", "/usr/local/bin/load-tape")
        with patch.object(tb, "run_command", return_value="") as command:
            media.load({"id": "backup-id"}, 3, True)
            self.assertEqual(command.call_args_list[0].args[0], [
                "/usr/local/bin/load-tape", "write", "backup-id", "3", "/dev/nst1"])
            self.assertEqual(command.call_args_list[1].args[0], ["mt", "-f", "/dev/nst1", "rewind"])
            self.assertEqual(command.call_args_list[2].args[0], ["mt", "-f", "/dev/nst1", "setblk", "0"])
            with patch("builtins.open", return_value=io.BytesIO()) as opened:
                media.reader().close()
                opened.assert_called_once_with("/dev/nst1", "rb", buffering=0)
            self.assertEqual(command.call_args.args[0], ["mt", "-f", "/dev/nst1", "rewind"])
            media.release()
            self.assertEqual(command.call_args.args[0], ["mt", "-f", "/dev/nst1", "offline"])

    def test_rewinding_tape_device_is_rejected(self):
        info = SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=os.makedev(9, 0))
        with patch.object(tb.os, "stat", return_value=info):
            with self.assertRaisesRegex(tb.BackupError, "non-rewinding"):
                tb.TapeMedia("/dev/st0")

    def test_cli_full_incremental_restore_and_status(self):
        script = str(Path(tb.__file__).resolve())

        def run(*args):
            result = subprocess.run([sys.executable, script, *map(str, args)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        self.seed()
        common = ["--state", self.state, "--source", self.source,
                  "--media-dir", self.media_dir, "--volume-size", "128KiB"]
        self.assertEqual(run("--version"), "legacy_v1.py 0.1.0")
        full = run("backup", *common)
        (self.source / "changed").write_text("CLI delta")
        (self.source / "deleted").unlink()
        delta = run("backup", *common, "--level", "incremental")
        run("restore", "--catalog", full, delta, "--destination", self.destination,
            "--work-dir", self.work, "--media-dir", self.media_dir)
        self.assertEqual(tree_contents(self.source), tree_contents(self.destination))
        self.assertIn('"pending": null', run("status", "--state", self.state))


if __name__ == "__main__":
    unittest.main()
