"""In-memory password authentication. No password argument or temporary file."""
import getpass
from pathlib import Path
import subprocess
import sys
import threading
import time


class InputStream:
    def __init__(self, channel):
        self.channel = channel
        self.closed = False

    def write(self, value):
        self.channel.sendall(value)

    def close(self):
        if not self.closed:
            self.channel.shutdown_write()
            self.closed = True


class Process:
    def __init__(self, channel):
        self.channel = channel
        self.stdin = InputStream(channel)
        self.stdout = channel.makefile('rb')
        self.stderr_thread = threading.Thread(target=self._stderr, daemon=True)
        self.stderr_thread.start()

    def _stderr(self):
        with self.channel.makefile_stderr('rb') as stream:
            while True:
                data = stream.read(4096)
                if not data:
                    return
                sys.stderr.write(data.decode('utf-8', errors='replace'))
                sys.stderr.flush()

    def wait(self):
        status = self.channel.recv_exit_status()
        self.stderr_thread.join(timeout=2)
        self.channel.close()
        return status


class PasswordTransport:
    def __init__(self, cfg):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError('Password mode requires Paramiko; install requirements.txt with the configured Python') from exc
        self.paramiko = paramiko
        self.cfg = cfg
        self.client = None

    def connect(self):
        if self.client and self.client.get_transport() and self.client.get_transport().is_active():
            return
        if self.client:
            self.client.close()
        target = self.cfg['SshTarget']
        explicit_user, sep, host = target.rpartition('@')
        if not sep:
            host = target
        settings = {}
        config_path = Path.home() / '.ssh/config'
        if config_path.exists():
            config = self.paramiko.SSHConfig()
            with config_path.open(encoding='utf-8') as f:
                config.parse(f)
            settings = config.lookup(host)
        if settings.get('proxyjump'):
            raise RuntimeError('Password mode does not support ProxyJump; use a direct host or ProxyCommand')
        hostname = settings.get('hostname', host)
        username = explicit_user if sep else settings.get('user', getpass.getuser())
        port = int(self.cfg.get('SshPort') or settings.get('port', 22))
        client = self.paramiko.SSHClient()
        known_hosts = Path.home() / '.ssh/known_hosts'
        if known_hosts.exists():
            client.load_host_keys(str(known_hosts))
        # Never silently trust an unknown or changed host key.
        client.set_missing_host_key_policy(self.paramiko.RejectPolicy())
        proxy = self.paramiko.ProxyCommand(settings['proxycommand']) if settings.get('proxycommand') else None
        client.connect(hostname, port=port, username=username, password=self.cfg['Password'],
                       allow_agent=False, look_for_keys=False, sock=proxy,
                       timeout=15, auth_timeout=30, banner_timeout=30)
        client.get_transport().set_keepalive(30)
        self.client = client

    def popen(self, command):
        self.connect()
        channel = self.client.get_transport().open_session(timeout=30)
        channel.settimeout(60)
        channel.exec_command(command)
        return Process(channel)

    def run(self, command, input=None, stdout=None, check=False, **unused):
        process = self.popen(command)
        if input:
            process.stdin.write(input)
        process.stdin.close()
        output = process.stdout.read()
        status = process.wait()
        if stdout != subprocess.PIPE:
            sys.stdout.write(output.decode('utf-8', errors='replace'))
            sys.stdout.flush()
        if check and status:
            # No config or password is included in the diagnostic.
            raise RuntimeError('Remote SSH command failed (exit %d)' % status)
        return subprocess.CompletedProcess(command, status, output)

    def close(self):
        if self.client:
            self.client.close()
