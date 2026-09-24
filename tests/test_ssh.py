"""Real SSH streaming tests, plus malformed/failed source protocol coverage."""
from contextlib import closing, redirect_stderr
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tape_backup as tb
from ssh_fixture import SSHServer
from test_append import TapeMedia as SimulatedTapeMedia, Cartridge
from test_tape_backup import FaultMedia, tree_contents


class SSHTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SSHServer()
        cls.addClassCleanup(cls.server.close)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "source ' $(touch UNWANTED)"
        self.source.mkdir()
        (self.source / "unchanged").write_bytes(os.urandom(700000))
        (self.source / "change").write_text("before")
        (self.source / "delete").write_text("delete")
        (self.source / "old-dir").mkdir()
        (self.source / "old-dir" / "space and\nnewline").write_text("odd filename")
        (self.source / "symlink").symlink_to("unchanged")
        os.link(self.source / "unchanged", self.source / "hardlink")
        self.media = FaultMedia(self.root / "media")
        self.program = self.root / "remote binary ' $(touch UNWANTED)"
        self.program.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(Path(tb.__file__).resolve())]) + ' "$@"\n')
        self.program.chmod(0o700)
        self.ssh = tb.SSHConfig("backup-source", config=self.server.config, program=str(self.program))
        self.output = io.StringIO()
        capture = redirect_stderr(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def create(self, base=None):
        return tb.backup(self.source, self.media, level="incremental" if base else "full",
                         base=base, ssh=self.ssh, buffer_size=tb.BLOCK_SIZE,
                         volume_size=8*tb.BLOCK_SIZE, quiet=True)

    def restore(self, ids):
        destination = self.root / "restored"
        tb.restore(ids, destination, self.media, quiet=True)
        self.assertEqual(tree_contents(self.source), tree_contents(destination))
        self.assertEqual((destination / "unchanged").stat().st_ino,
                         (destination / "hardlink").stat().st_ino)

    def test_remote_accepts_10_gib_buffer_without_allocating_it_for_small_sources(self):
        full = tb.backup(self.source, self.media, ssh=self.ssh,
                         buffer_size=10 * 1024**3, quiet=True)
        self.restore([full])

    def test_remote_preview_does_not_start_stream_and_keeps_incremental_tape_unchanged(self):
        preview = tb.backup(self.source, None, ssh=self.ssh, dry_run=True, excludes=['old-dir'])
        self.assertTrue(preview['dry_run'])
        self.assertEqual(preview['excludes'], ['old-dir'])
        self.assertFalse(self.media.directory.exists())
        full = self.create()
        before = tree_contents(self.media.directory)
        preview = tb.backup(self.source, self.media, level='incremental', base=full,
                            ssh=self.ssh, dry_run=True)
        self.assertEqual(preview['parent'], full)
        self.assertEqual(tree_contents(self.media.directory), before)

    def test_remote_stream_continues_after_rejecting_a_used_continuation_tape(self):
        media = SimulatedTapeMedia(capacity=12)
        used = Cartridge()
        used.records = [b'previous backup'.ljust(tb.BLOCK_SIZE, b'\0'), None]
        before = list(used.records)
        request = media.request
        attempts = []
        def wrong_then_blank(backup_id, number, action):
            if action == 'blank' and number == 2:
                attempts.append((backup_id, number, action))
                if len(attempts) == 1:
                    media.active = used
                    return
            request(backup_id, number, action)
        media.request = wrong_then_blank
        full = tb.backup(self.source, media, ssh=self.ssh, buffer_size=256 * 1024, quiet=True)
        self.assertEqual(attempts, [(full, 2, 'blank')] * 2)
        self.assertEqual(used.records, before)
        self.assertIn('Backup remains active', self.output.getvalue())
        media.loaded = False
        self.assertTrue(tb.scan(media, full)['data_verified'])
        destination = self.root / 'restored'
        tb.restore([full], destination, media, quiet=True)
        self.assertEqual(tree_contents(destination), tree_contents(self.source))

    def test_old_ssh_protocol_is_rejected_before_tape_writing(self):
        self.program.write_text("#!/bin/sh\nprintf 'TAPE-SSH-1\\n'\n")
        with self.assertRaisesRegex(tb.BackupError, 'Unsupported SSH source protocol'):
            self.create()
        self.assertFalse(list(self.media.directory.glob('*.tape')))

    def test_remote_full_and_two_deltas_restore_without_remote_access(self):
        full = self.create()
        self.assertGreater(len(list(self.media.directory.glob(f"{full}.*.tape"))), 1)
        (self.source / "change").write_text("first delta")
        (self.source / "delete").unlink()
        (self.source / "added").write_text("new")
        (self.source / "old-dir").rename(self.source / "renamed")
        first = self.create(full)
        reader = tb.StreamReader(self.media, first)
        with closing(reader):
            data = b"".join(data for kind, data in reader.frames() if kind == "data")
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            names = archive.getnames()
        self.assertIn("./change", names)
        self.assertNotIn("./unchanged", names)
        shutil.rmtree(self.source / "renamed")
        (self.source / "change").write_text("second delta")
        second = self.create(first)
        # The source helper can be removed: restore uses only self-contained tapes.
        self.program.unlink()
        self.restore([full, first, second])
        info = tb.scan(self.media, second)
        self.assertEqual(info["ssh"], self.ssh.metadata)
        self.assertTrue(info["data_verified"])
        self.assertIn("MiB/s", self.output.getvalue())
        self.assertIn("ETA", self.output.getvalue())
        for pattern in ("*.tar", "*.snar", "UNWANTED"):
            self.assertFalse(list(self.root.rglob(pattern)))
        self.assertEqual(list(self.root.rglob('*.json')),
                         [tb.restore_marker_path(self.root / 'restored')])

    def test_remote_stream_survives_tape_flush_failure_and_replay(self):
        self.media.fail_commit = True
        full = self.create()
        self.assertIn("uncommitted chunk(s)", self.output.getvalue())
        self.restore([full])

    def test_remote_incremental_appends_using_cached_snapshot(self):
        self.media = tb.FileMedia(self.root / 'append-media')
        full = tb.backup(self.source, self.media, ssh=self.ssh, quiet=True)
        tape = next(self.media.directory.glob('*.tape'))
        original = tape.read_bytes()
        (self.source / 'change').write_text('appended delta')
        (self.source / 'delete').unlink()
        delta = tb.backup(self.source, self.media, ssh=self.ssh, quiet=True,
                          level='incremental', base=full)
        self.assertIn('no archive scan', self.output.getvalue())
        self.assertEqual(list(self.media.directory.glob('*.tape')), [tape])
        self.assertEqual(tape.read_bytes()[:len(original)], original)
        self.program.unlink()
        self.restore([full, delta])

    def test_remote_wrong_host_and_path_rejected_before_writing_new_tapes(self):
        full = self.create()
        original = set(self.media.directory.glob("*.tape"))
        self.ssh.host = "other-source"
        with self.assertRaisesRegex(tb.BackupError, "SSH source differs"):
            self.create(full)
        self.ssh.host = "backup-source"
        self.source = self.root / "another-source"
        self.source.mkdir()
        with self.assertRaisesRegex(tb.BackupError, "source differs"):
            self.create(full)
        self.assertEqual(original, set(self.media.directory.glob("*.tape")))

    def test_missing_remote_binary_or_source_does_not_start_tape(self):
        self.ssh.program = str(self.root / "missing-binary")
        with self.assertRaisesRegex(tb.BackupError, "SSH source"):
            self.create()
        self.ssh.program = str(self.program)
        self.source = self.root / "missing-source"
        with self.assertRaises((tb.BackupError, BrokenPipeError)):
            self.create()
        self.assertFalse(list(self.media.directory.glob("*.tape")))

    def test_untrusted_host_is_rejected_before_tape_write(self):
        config = self.root / "untrusted-config"
        config.write_text(self.server.config.read_text().replace(str(self.server.root / "known_hosts"), "/dev/null"))
        self.ssh.config = config
        with self.assertRaisesRegex(tb.BackupError, "SSH source"):
            self.create()
        self.assertFalse(list(self.media.directory.glob("*.tape")))

    def test_remote_tar_failure_is_incomplete_and_retry_succeeds(self):
        tools = self.root / "tools"
        tools.mkdir()
        tar = tools / "tar"
        tar.write_text('#!/bin/sh\nif [ "$1" = "--create" ]; then exit 1; fi\nexec /bin/tar "$@"\n')
        tar.chmod(0o700)
        original = self.program.read_text()
        self.program.write_text("#!/bin/sh\nexport PATH=" + shlex.quote(str(tools)) + "\n" + original.split("\n", 1)[1])
        with self.assertRaisesRegex(tb.BackupError, "SSH source"):
            self.create()
        incomplete = next(self.media.directory.glob("*.tape")).name.split(".")[0]
        with self.assertRaises(tb.BackupError):
            tb.scan(self.media, incomplete)
        self.program.write_text(original)
        self.restore([self.create()])

    def test_disconnect_after_data_does_not_publish_completion(self):
        # A real SSH session whose helper exits midway through the protocol.
        helper = self.root / "disconnect.py"
        helper.write_text(f"""import sys
sys.path.insert(0, {str(Path(tb.__file__).parent)!r})
import tape_backup as tb
incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
outgoing.write(tb.SSH_MAGIC)
outgoing.flush()
request = tb.receive_json(incoming)
while tb.receive_packet(incoming, tb.BLOCK_SIZE)[0] != b'e':
    pass
tb.send_packet(outgoing, b'j', tb.json.dumps({{'source': request['source'], 'estimated_bytes': 200000, 'excludes': request['excludes']}}).encode())
tb.receive_packet(incoming, 0)
tb.send_packet(outgoing, b'd', b'x' * tb.BLOCK_SIZE)
raise SystemExit(255)
""")
        self.program.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(helper)]) + "\n")
        with self.assertRaisesRegex(tb.BackupError, "disconnected"):
            self.create()
        incomplete = next(self.media.directory.glob("*.tape")).name.split(".")[0]
        with self.assertRaises(tb.BackupError):
            tb.scan(self.media, incomplete)


class SSHProtocolTests(unittest.TestCase):
    def test_packet_lengths_above_4_gib_are_encoded_and_decoded_without_truncation(self):
        # Exercise the framing boundaries without allocating multi-GiB payloads.
        class SizedPayload:
            def __init__(self, size):
                self.size = size

            def __len__(self):
                return self.size

        for size in (2**32, 2**32 + 1, 10 * 1024**3):
            with self.subTest(size=size):
                payload = SizedPayload(size)
                output = Mock()
                tb.send_packet(output, b'd', payload)
                header = struct.pack('!cQ', b'd', size)
                self.assertEqual(output.write.call_args_list[0].args, (header,))
                self.assertIs(output.write.call_args_list[1].args[0], payload)
                incoming = io.BytesIO(header)
                read_exact = tb.read_exact

                def read_payload(stream, count, progress=None):
                    if stream.tell() == 0:
                        return read_exact(stream, count)
                    self.assertEqual(count, size)
                    return payload

                with patch.object(tb, 'read_exact', side_effect=read_payload):
                    self.assertEqual(tb.receive_packet(incoming, size), (b'd', payload))
                with self.assertRaisesRegex(tb.BackupError, 'exceeds'):
                    tb.receive_packet(io.BytesIO(header), size - 1)

    def test_packet_progress_updates_before_data_packet_is_complete(self):
        progress = tb.Progress('Test')
        observed = []

        class ObservedStream(io.BytesIO):
            def read(self, size):
                observed.append(progress.read_bytes)
                return super().read(size)

        wire = io.BytesIO()
        payload = b'x' * (2 * 1024**2 + 1)
        tb.send_packet(wire, b'd', payload)
        tb.send_packet(wire, b's', b'snapshot')
        stream = ObservedStream(wire.getvalue())
        self.assertEqual(tb.receive_packet(stream, len(payload), progress), (b'd', payload))
        self.assertIn(1024**2, observed)
        self.assertIn(2 * 1024**2, observed)
        self.assertEqual(progress.read_bytes, len(payload))
        self.assertEqual(tb.receive_packet(stream, len(payload), progress), (b's', b'snapshot'))
        self.assertEqual(progress.read_bytes, len(payload))

    def test_source_cannot_finish_with_failed_exit_extra_output_or_reordered_data(self):
        for packets, exit_code, extra in (
                ([(b'd', b'data'), (b's', b'snapshot'), (b'e', b'')], 1, b''),
                ([(b'd', b'data'), (b's', b'snapshot'), (b'e', b'')], 0, b'junk'),
                ([(b's', b'snapshot'), (b'd', b'data')], 0, b'')):
            with self.subTest(packets=packets, exit_code=exit_code, extra=extra):
                wire = io.BytesIO()
                for kind, payload in packets:
                    tb.send_packet(wire, kind, payload)
                wire.write(extra)
                wire.seek(0)
                remote = tb.RemoteSource(tb.SSHConfig('server'), '/data', -1, None, tb.BLOCK_SIZE, True)
                remote.process = SimpleNamespace(stdin=io.BytesIO(), stdout=wire, wait=lambda: exit_code)
                with self.assertRaises(tb.BackupError):
                    list(remote.frames())

    def test_packet_bounds_truncation_and_bad_metadata(self):
        for wire in (tb.PACKET_HEADER.pack(b"d", tb.MAX_BUFFER + 1),
                     tb.PACKET_HEADER.pack(b"d", 2) + b"x"):
            with self.assertRaises(tb.BackupError):
                tb.receive_packet(io.BytesIO(wire), tb.MAX_BUFFER)
        for payload in (b"not json", b"[]", b"null"):
            wire = io.BytesIO()
            tb.send_packet(wire, b"j", payload)
            wire.seek(0)
            with self.assertRaises(tb.BackupError):
                tb.receive_json(wire)

    def test_ssh_options_are_validated_and_remote_program_is_one_word(self):
        for host in ("-oProxyCommand=bad", "host\ncommand", ""):
            with self.assertRaises(tb.BackupError):
                tb.SSHConfig(host)
        for port in (0, 65536):
            with self.assertRaises(tb.BackupError):
                tb.SSHConfig("server", port=port)
        program = "/path with spaces/$(touch BAD)'binary"
        config = tb.SSHConfig("user@server", port=2222, identity="/key with spaces", program=program)
        self.assertEqual(shlex.split(config.command()[-1]), [program, "_ssh-source"])
        self.assertIn("StrictHostKeyChecking=yes", config.command())
        self.assertIn("BatchMode=yes", config.command())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(tb.main(["backup", "--source", "/data", "--ssh-port", "22"]), 1)


@unittest.skipUnless(os.environ.get("TAPE_BACKUP_BINARY"), "Set TAPE_BACKUP_BINARY to test SSH between binaries")
class BinarySSHTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SSHServer()
        cls.addClassCleanup(cls.server.close)

    def test_standalone_binaries_stream_remote_full_and_delta_without_python_in_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "tape-backup"
            shutil.copy2(Path(os.environ["TAPE_BACKUP_BINARY"]).resolve(), binary)
            tools = root / "tools"
            tools.mkdir()
            for name in ("tar", "ssh"):
                (tools / name).symlink_to(Path(shutil.which(name)).resolve())
            remote = root / "remote-helper"
            remote.write_text("#!/bin/sh\nexport PATH=" + shlex.quote(str(tools)) + "\nexec " +
                              shlex.quote(str(binary)) + ' "$@"\n')
            remote.chmod(0o700)
            env = os.environ.copy()
            env.update(PATH=str(tools), PYTHONHOME="/no-python", PYTHONPATH="/no-python",
                       TAR_OPTIONS="--invalid-option")
            source, media = root / "source", root / "media"
            source.mkdir()
            (source / "keep").write_bytes(os.urandom(400000))
            (source / "change").write_text("before")
            (source / "delete").write_text("delete")

            def run(*args):
                result = subprocess.run([str(binary), *map(str, args)], env=env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("MiB/s", result.stderr)
                self.assertIn("ETA", result.stderr)
                return result

            backup = ["backup", "--source", source, "--ssh", "backup-source", "--ssh-config", self.server.config,
                      "--ssh-port", self.server.port, "--ssh-identity", self.server.root / "client",
                      "--remote-program", remote, "--media-dir", media, "--volume-size", "512KiB",
                      "--buffer-size", "64KiB"]
            result = run(*backup)
            full = result.stdout.strip()
            self.assertIn("./change", result.stderr)
            (source / "change").write_text("after")
            (source / "delete").unlink()
            (source / "added").write_text("new")
            result = run(*backup, "--level", "incremental", "--base", full, "--quiet")
            delta = result.stdout.strip()
            self.assertNotIn("./change", result.stderr)
            remote.unlink()
            run("restore", "--backup", full, delta, "--destination", root / "restored", "--media-dir", media)
            self.assertEqual(tree_contents(source), tree_contents(root / "restored"))
            for pattern in ("*.tar", "*.snar"):
                self.assertFalse(list(root.rglob(pattern)))
            self.assertEqual(list(root.rglob('*.json')), [tb.restore_marker_path(root / 'restored')])


if __name__ == "__main__":
    unittest.main()
