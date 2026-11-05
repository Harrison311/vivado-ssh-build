"""Windows client: metadata-only incremental transfer over OpenSSH, no hashes."""
import base64
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
from contextlib import nullcontext

TOOL = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL / '.deps'))


def server_config(cfg):
    # Only build settings cross to the server. Never serialize login credentials.
    keys = ('Part', 'TopModule', 'Jobs', 'VivadoSettings', 'SourceFiles',
            'ConstraintFiles', 'IpSetupTcl', 'IncludeDirs')
    return {key: cfg[key] for key in keys if key in cfg}


def relative_path(root, name):
    rel = PurePosixPath(name.replace('\\', '/'))
    if not name or rel.is_absolute() or '..' in rel.parts or ':' in str(rel):
        raise ValueError('Expected project-relative path: ' + name)
    path = root.joinpath(*rel.parts)
    if root.resolve() not in path.resolve().parents:
        raise ValueError('Path escapes project: ' + name)
    return path


def scan(cfg):
    root = Path(cfg['LocalProjectRoot']).resolve()
    result = {}
    inputs = []
    for key in ('SourceFiles', 'ConstraintFiles', 'IpSetupTcl', 'IncludeDirs', 'ExtraUploadPaths'):
        inputs.extend(cfg.get(key, []))
    for name in inputs:
        p = relative_path(root, name)
        if not p.exists():
            raise ValueError('Missing input: ' + str(p))
        candidates = p.rglob('*') if p.is_dir() else [p]
        for item in candidates:
            if cfg.get('ConfigFile') and item.resolve() == Path(cfg['ConfigFile']).resolve():
                continue  # The project launcher may contain a plaintext password.
            if item.is_symlink():
                raise ValueError('Symlink input is not supported: ' + str(item))
            if not item.is_file():
                continue
            rel = item.relative_to(root).as_posix()
            # Never traverse generated trees accidentally included under a directory.
            if any(part in cfg.get('ExcludeNames', []) for part in item.relative_to(root).parts):
                continue
            stat = item.stat()
            result[rel] = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    if not result or not cfg.get('SourceFiles'):
        raise ValueError('SourceFiles must name the RTL compilation inputs')
    for key in ('SourceFiles', 'ConstraintFiles', 'IpSetupTcl'):
        for name in cfg.get(key, []):
            if name.replace('\\', '/') not in result:
                raise ValueError('Required input excluded or not a file: ' + name)
    return dict(sorted(result.items()))


def changes(local, remote, force=False):
    return [name for name, meta in local.items() if force or remote.get(name) != meta]


class Client:
    def __init__(self, cfg):
        self.cfg = cfg
        target = cfg['SshTarget']
        if not target or target.startswith('-') or any(c.isspace() for c in target):
            raise ValueError('SshTarget must be an SSH alias or user@host')
        self.root = cfg['RemoteProjectRoot'].rstrip('/')
        if not self.root.startswith('/') or self.root in ('', '/', '/home', '/tmp', '/opt', '/usr'):
            raise ValueError('Use a dedicated absolute Linux RemoteProjectRoot')
        self.ssh = [cfg.get('SshExecutable', 'ssh'), '-T', '-p', str(int(cfg.get('SshPort', 22))),
                    '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30']
        if cfg.get('IdentityFile'):
            self.ssh += ['-i', str(Path(cfg['IdentityFile']).expanduser())]
        self.ssh += [target]
        self.state_path = Path(cfg['LocalResultRoot']).resolve() / '.vivado_ssh_latest.json'
        self.password_transport = None
        if cfg.get('Password'):
            from password_transport import PasswordTransport
            self.password_transport = PasswordTransport(cfg)

    def run(self, args, **kwargs):
        if self.password_transport:
            return self.password_transport.run(args[-1], **kwargs)
        return subprocess.run(args, **kwargs)

    def popen(self, args, **kwargs):
        if self.password_transport:
            return self.password_transport.popen(args[-1])
        return subprocess.Popen(args, **kwargs)

    def close(self):
        if self.password_transport:
            self.password_transport.close()

    def ssh_python(self, code, **kwargs):
        encoded = base64.b64encode(code.encode('utf-8')).decode('ascii')
        command = "python3 -c " + shlex.quote("exec(__import__('base64').b64decode('" + encoded + "'))")
        return self.run(self.ssh + [command], check=True, **kwargs)

    def check(self):
        manifest = scan(self.cfg)
        settings = self.cfg['VivadoSettings']
        code = ('import sys, pathlib, shutil, json\n'
                'assert sys.version_info >= (3,8), "Python 3.8+ required"\n'
                'assert pathlib.Path(' + repr(settings) + ').is_file(), "Vivado settings missing"\n'
                'assert shutil.which("bash"), "bash missing"\n'
                'print(json.dumps({"python":sys.version,"settings":' + repr(settings) + '}))\n')
        self.ssh_python(code)
        # This checks installation/version, not a synthesis licence checkout.
        command = ['bash', '-c', 'source "$1" && vivado -version', 'check-vivado', settings]
        self.run(self.ssh + [' '.join(shlex.quote(x) for x in command)], check=True)
        print('Local inputs:', len(manifest), '; total bytes:', sum(x['size'] for x in manifest.values()))

    def deploy_agent(self):
        code = '''import os, pathlib, sys
root = pathlib.Path(ROOT)
root.mkdir(parents=True, exist_ok=True)
marker = root / '.vivado-ssh-owner'
if not marker.exists():
    if any(root.iterdir()):
        raise RuntimeError('RemoteProjectRoot is non-empty and not managed; choose a new directory')
    marker.write_text('vivado-ssh-v1', encoding='utf-8')
elif marker.read_text(encoding='utf-8') != 'vivado-ssh-v1':
    raise RuntimeError('Unexpected directory owner marker')
tool = root / '.tool'
tool.mkdir(exist_ok=True)
import tempfile
fd, name = tempfile.mkstemp(dir=str(tool), prefix='agent-')
with os.fdopen(fd, 'wb') as f:
    f.write(sys.stdin.buffer.read())
os.replace(name, tool / 'remote_agent.py')
'''
        self.ssh_python('ROOT=' + repr(self.root) + '\n' + code,
                        input=(TOOL / 'remote_agent.py').read_bytes())

    def request(self, action, **extra):
        request = dict(root=self.root, action=action, **extra)
        return (json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8')

    def command(self):
        # Put manifests on stdin, not the command line: large projects would
        # otherwise exceed Windows' process command-line length limit.
        command = 'python3 ' + shlex.quote(self.root + '/.tool/remote_agent.py') + ' --stdin-json'
        return self.ssh + [command]

    def call(self, action, **extra):
        result = self.run(self.command(), input=self.request(action, **extra),
                                check=True, stdout=subprocess.PIPE)
        return json.loads(result.stdout)

    def upload(self, manifest, force=False):
        current = self.call('inventory')['files']
        delta = changes(manifest, current, force)
        print('Uploading', len(delta), 'of', len(manifest), 'input files; no content digest comparison.', flush=True)
        process = self.popen(self.command(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            process.stdin.write(self.request('sync', manifest=manifest, changes=delta))
            for name in delta:
                p = relative_path(Path(self.cfg['LocalProjectRoot']).resolve(), name)
                size = manifest[name]['size']
                process.stdin.write((json.dumps({'path': name, 'size': size}) + '\n').encode())
                with p.open('rb') as f:
                    remaining = size
                    while remaining:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError('Local file changed during upload: ' + name)
                        process.stdin.write(chunk)
                        remaining -= len(chunk)
            if scan(self.cfg) != manifest:
                raise ValueError('Local inputs changed during upload; retry after saving edits')
            process.stdin.write(b'{"end":true}\n')
            process.stdin.close()
            response = process.stdout.read()
            if process.wait() != 0:
                raise RuntimeError('Remote sync failed; no build submitted')
            print(json.loads(response))
        except BaseException:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.wait()
            raise

    def remember(self, job):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({'target': self.cfg['SshTarget'], 'root': self.root,
                                               'job': job}, indent=2), encoding='utf-8')

    def resolve_job(self, supplied):
        if supplied:
            job = supplied
        else:
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            if state['target'] != self.cfg['SshTarget'] or state['root'] != self.root:
                raise ValueError('Latest job belongs to another project; supply -JobId')
            job = state['job']
        if not job or any(c not in '0123456789_-' for c in job):
            raise ValueError('Invalid job id')
        return job

    def fetch(self, job):
        destination = Path(self.cfg['LocalResultRoot']).resolve() / job
        destination.mkdir(parents=True, exist_ok=True)
        process = self.popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        process.stdin.write(self.request('fetch', job=job))
        process.stdin.close()
        ended = False
        while True:
            line = process.stdout.readline()
            if not line:
                break
            header = json.loads(line)
            if header.get('end'):
                ended = True
                break
            target = relative_path(destination, header['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.partial')
            with temp.open('wb') as f:
                remaining = header['size']
                while remaining:
                    data = process.stdout.read(min(1024 * 1024, remaining))
                    if not data:
                        raise RuntimeError('Download interrupted; .partial retained')
                    f.write(data)
                    remaining -= len(data)
            os.replace(temp, target)
        if process.wait() != 0 or not ended:
            raise RuntimeError('Result download incomplete; retry fetch for the same job')
        print('Results:', destination)


def execute(client, cfg, action):
    if action == 'check':
        client.check()
        return
    if action not in ('submit', 'submit_start', 'status', 'fetch'):
        raise ValueError('Expected check, submit, submit_start, status or fetch')
    if action in ('submit', 'submit_start'):
        if action == 'submit_start':
            # Import before submission so a missing UI dependency cannot leave an
            # unexpected unmonitored job running.
            from progress_ui import watch_and_fetch, Console
        manifest = scan(cfg)
        if int(cfg['Jobs']) < 1:
            raise ValueError('Jobs must be positive')
        display = Console().status('Uploading inputs and creating build...', spinner='dots') if action == 'submit_start' else nullcontext()
        with display:
            client.deploy_agent()
            client.upload(manifest, bool(cfg.get('ForceUpload', False)))
            result = client.call('submit', manifest=manifest, config=server_config(cfg),
                                 build_script=(TOOL / 'build.tcl').read_text(encoding='utf-8'))
        client.remember(result['job'])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if action == 'submit_start':
            watch_and_fetch(client, result['job'])
    else:
        # Status/fetch do not overwrite the server agent or touch source files.
        job = client.resolve_job(cfg.get('JobId', ''))
        if action == 'status':
            print(json.dumps(client.call('status', job=job), ensure_ascii=False, indent=2))
        else:
            client.fetch(job)


def main():
    if sys.version_info < (3, 8):
        raise RuntimeError('Local Python 3.8+ required')
    cfg = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
    client = Client(cfg)
    try:
        execute(client, cfg, sys.argv[1])
    finally:
        client.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nMonitoring interrupted. The remote job is not cancelled; use status/fetch later.', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
