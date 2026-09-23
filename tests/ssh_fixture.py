"""An isolated loopback SSH server; never alters user SSH keys/configuration."""
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


class SSHServer:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tape-backup-ssh-")
        self.root = Path(self.tmp.name)
        self.process = None
        self.log = None
        try:
            sshd = shutil.which("sshd") or "/usr/sbin/sshd"
            if not Path(sshd).exists() or not shutil.which("ssh-keygen") or not shutil.which("ssh"):
                raise unittest.SkipTest("OpenSSH client/server tools are required for SSH integration tests")
            for name in ("host", "client"):
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.root / name)],
                               check=True, capture_output=True)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
            server_config = self.root / "sshd_config"
            server_config.write_text(
                f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {self.root}/host\n"
                f"PidFile {self.root}/pid\nAuthorizedKeysFile {self.root}/client.pub\n"
                "StrictModes no\nUsePAM no\nPasswordAuthentication no\n"
                "KbdInteractiveAuthentication no\nPubkeyAuthentication yes\nPermitRootLogin prohibit-password\n"
                "AllowTcpForwarding no\nX11Forwarding no\nLogLevel ERROR\n")
            self.log = open(self.root / "server.log", "w+")
            self.process = subprocess.Popen([sshd, "-D", "-e", "-f", str(server_config)],
                                            stdout=subprocess.DEVNULL, stderr=self.log)
            deadline = time.monotonic() + 5
            while True:
                if self.process.poll() is not None or time.monotonic() > deadline:
                    self.log.seek(0)
                    raise RuntimeError("Test SSH server did not start: " + self.log.read())
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            known_hosts = self.root / "known_hosts"
            known_hosts.write_text("tape-backup-test " + (self.root / "host.pub").read_text())
            self.config = self.root / "ssh_config"
            self.config.write_text(
                f"Host backup-source other-source\n  HostName 127.0.0.1\n  Port {self.port}\n"
                f"  User {pwd.getpwuid(os.getuid()).pw_name}\n  IdentityFile {self.root}/client\n"
                f"  UserKnownHostsFile {known_hosts}\n  HostKeyAlias tape-backup-test\n"
                "  IdentitiesOnly yes\n  IdentityAgent none\n")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        if self.log:
            self.log.close()
        self.tmp.cleanup()
