"""Linux-side transport and isolated Vivado jobs. Python 3.8+, stdlib only."""
import base64
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

MARKER = '.vivado-ssh-owner'


def safe_path(root, relative):
    p = PurePosixPath(relative)
    if not relative or p.is_absolute() or '..' in p.parts or '\\' in relative:
        raise ValueError('Unsafe relative path: ' + relative)
    target = root.joinpath(*p.parts)
    if root.resolve() not in target.resolve().parents:
        raise ValueError('Path escapes managed directory')
    return target


def save(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


@contextmanager
def locked(root):
    if not (root / MARKER).is_file():
        raise ValueError('Missing managed directory marker')
    with (root / '.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def inventory(root):
    known = read(root / 'manifest.json', {})
    result = {}
    for name in known:
        p = safe_path(root / 'source', name)
        if p.is_file():
            s = p.stat()
            result[name] = {'size': s.st_size, 'mtime_ns': s.st_mtime_ns}
    return {'files': result, 'tracked': list(known)}


def sync(root, request, stream):
    desired = request['manifest']
    changes = set(request['changes'])
    if not changes.issubset(desired):
        raise ValueError('Unexpected change list')
    source = root / 'source'
    source.mkdir(exist_ok=True)
    for name in desired:
        safe_path(source, name)
    with locked(root):
        # Validate unchanged files again under lock; another submit may have synced.
        current = inventory(root)['files']
        for name, meta in desired.items():
            if name not in changes and current.get(name) != meta:
                raise ValueError('Source changed concurrently; submit again: ' + name)
        with tempfile.TemporaryDirectory(prefix='incoming-', dir=str(root)) as temp:
            staging = Path(temp)
            received = set()
            while True:
                line = stream.readline()
                if not line:
                    raise ValueError('Upload interrupted; source not committed')
                header = json.loads(line)
                if header.get('end'):
                    break
                name = header['path']
                if name not in changes or name in received:
                    raise ValueError('Unexpected upload file')
                size = desired[name]['size']
                if header['size'] != size:
                    raise ValueError('Upload size mismatch')
                p = safe_path(staging, name)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open('wb') as f:
                    remaining = size
                    while remaining:
                        data = stream.read(min(1024 * 1024, remaining))
                        if not data:
                            raise ValueError('Truncated upload')
                        f.write(data)
                        remaining -= len(data)
                received.add(name)
            if received != changes:
                raise ValueError('Missing upload files')
            old = read(root / 'manifest.json', {})
            # Invalidate metadata first. A process failure cannot yield a false
            # successful snapshot; the next submit repairs the incomplete sync.
            save(root / 'manifest.json', {})
            for name in changes:
                target = safe_path(source, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(safe_path(staging, name), target)
                ns = desired[name]['mtime_ns']
                os.utime(target, ns=(ns, ns))
            for name in set(old) - set(desired):
                p = safe_path(source, name)
                if p.is_file():
                    p.unlink()
            save(root / 'manifest.json', desired)
    return {'uploaded': len(changes), 'removed': len(set(old) - set(desired))}


def tcl_string(value):
    # Vivado 2023.2 can embed Tcl without "binary decode" (Tcl 8.6).
    # Hex is a reversible byte encoding, not a content digest. H* is available
    # in older Tcl and prevents paths from becoming executable Tcl syntax.
    encoded = str(value).encode('utf-8').hex()
    return '[encoding convertfrom utf-8 [binary format H* {' + encoded + '}]]'


def submit(root, request):
    cfg = request['config']
    manifest = request['manifest']
    with locked(root):
        if inventory(root)['files'] != manifest:
            raise ValueError('Remote source changed after synchronization; submit again')
        job_id = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d_%H-%M-%S_%f')
        job = root / 'jobs' / job_id
        job.mkdir(parents=True)
        (job / 'src').mkdir()
        for name in manifest:
            dest = safe_path(job / 'src', name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_path(root / 'source', name), dest)
        (job / 'work').mkdir()
        (job / 'results').mkdir()
        save(job / 'manifest.json', manifest)
        save(job / 'config.json', cfg)
        variables = {'part': cfg['Part'], 'top': cfg['TopModule'],
                     'jobs': cfg['Jobs'], 'source_root': str(job / 'src'),
                     'result_root': str(job / 'results')}
        lines = ['set ' + key + ' ' + tcl_string(value) for key, value in variables.items()]
        for key, config_key in [('rtl_files', 'SourceFiles'), ('xdc_files', 'ConstraintFiles'),
                                ('ip_scripts', 'IpSetupTcl'), ('include_dirs', 'IncludeDirs')]:
            values = cfg.get(config_key, [])
            lines.append('set ' + key + ' [list ' + ' '.join(
                tcl_string(str(safe_path(job / 'src', p))) for p in values) + ']')
        (job / 'settings.tcl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        (job / 'build.tcl').write_text(request['build_script'], encoding='utf-8')
        shutil.copy2(Path(__file__), job / 'worker.py')
        save(job / 'status.json', {'state': 'STARTING', 'job': job_id})
        with (job / 'worker.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, str(job / 'worker.py'), '--worker', str(job)],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True, close_fds=True)
        save(job / 'worker_pid.json', proc.pid)
    return {'job': job_id, 'remote_directory': str(job)}


def worker(job):
    try:
        cfg = read(job / 'config.json')
        save(job / 'status.json', {'state': 'RUNNING', 'job': job.name})
        command = ['bash', '-c', 'source "$1" && exec vivado -mode batch -source "$2" -log "$3" -journal "$4"',
                   'vivado-build', cfg['VivadoSettings'], str(job / 'build.tcl'),
                   str(job / 'results' / 'vivado.log'), str(job / 'results' / 'vivado.jou')]
        with (job / 'results' / 'console.log').open('ab') as log:
            completed = subprocess.run(command, cwd=str(job / 'work'), stdout=log, stderr=log)
        for path in (job / 'work').rglob('runme.log'):
            dest = job / 'results' / 'run_logs' / path.relative_to(job / 'work')
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        marker = job / 'results' / 'build_status.txt'
        passed = completed.returncode == 0 and marker.exists() and marker.read_text().strip() == 'PASS'
        save(job / 'status.json', {'state': 'PASSED' if passed else 'FAILED', 'job': job.name,
                                 'vivado_exit_code': completed.returncode})
    except Exception as exc:
        save(job / 'status.json', {'state': 'FAILED', 'job': job.name, 'error': str(exc)})


def job_status(job):
    result = read(job / 'status.json')
    if result is None:
        raise ValueError('Unknown job')
    if result['state'] in ('RUNNING', 'STARTING'):
        pid = read(job / 'worker_pid.json')
        if pid:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                result['state'] = 'INTERRUPTED'
    log = job / 'results' / 'console.log'
    if log.exists():
        with log.open('rb') as f:
            f.seek(max(0, log.stat().st_size - 6000))
            result['log_tail'] = f.read().decode('utf-8', errors='replace')
    result['progress'] = progress(job, result['state'])
    return result


def progress(job, state):
    events = []
    path = job / 'results' / 'phase_events.tsv'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                stamp, name = line.split('\t', 1)
                events.append((float(stamp) / 1000, name))
            except ValueError:
                pass  # A writer may still be finishing the last line.
    now = time.time()
    rows = []
    current = events[-1][1] if events else 'Starting Vivado'
    phase_names = ['Preparing', 'Synthesis', 'Checks', 'Generate Bitstream', 'Debug Probes']
    for name in phase_names:
        hits = [i for i, item in enumerate(events) if item[1] == name]
        start = events[hits[-1]][0] if hits else None
        end = events[hits[-1] + 1][0] if hits and hits[-1] + 1 < len(events) else None
        status = 'DONE' if end is not None else ('RUNNING' if start is not None else 'PENDING')
        rows.append({'name': name, 'state': status, 'start': start, 'end': end})
    impl = job / 'work' / 'project' / 'build.runs' / 'impl_1'
    implementation = []
    for step in ('init_design', 'opt_design', 'power_opt_design', 'place_design',
                 'post_place_power_opt_design', 'phys_opt_design', 'route_design',
                 'post_route_phys_opt_design'):
        begin, end, error = [impl / ('.' + step + '.' + suffix + '.rst')
                             for suffix in ('begin', 'end', 'error')]
        start_time = begin.stat().st_mtime if begin.exists() else None
        end_time = end.stat().st_mtime if end.exists() else None
        status = 'FAILED' if error.exists() else ('DONE' if end_time is not None else
                 ('RUNNING' if start_time is not None else 'PENDING'))
        # Optional steps are shown only if Vivado actually started them.
        if start_time is not None or step in ('init_design', 'opt_design', 'place_design', 'route_design'):
            implementation.append({'name': 'Implementation: ' + step, 'state': status,
                                   'start': start_time, 'end': end_time})
    rows[2:2] = implementation
    active = [r for r in implementation if r['state'] == 'RUNNING']
    if current == 'Implementation' and active:
        current = max(active, key=lambda r: r['start'])['name']
    terminal = state not in ('STARTING', 'RUNNING')
    finished_at = (job / 'status.json').stat().st_mtime if terminal and (job / 'status.json').exists() else now
    for row in rows:
        if terminal and row['state'] == 'RUNNING':
            row['state'] = 'FAILED' if state != 'PASSED' else 'UNKNOWN'
        if terminal and row['state'] == 'PENDING':
            row['state'] = 'NOT RUN'
        stop = row['end'] if row['end'] is not None else (finished_at if terminal else now)
        row['seconds'] = max(0, stop - row['start']) if row['start'] is not None else None
    return {'current': current, 'stages': rows, 'sampled_at': now}


def fetch(job):
    state = job_status(job)['state']
    if state in ('RUNNING', 'STARTING'):
        raise ValueError('Job still running; use status first')
    files = [(p, p.relative_to(job / 'results').as_posix())
             for p in sorted((job / 'results').rglob('*')) if p.is_file()]
    files += [(job / 'status.json', 'build_info.json'), (job / 'worker.log', 'worker.log')]
    out = sys.stdout.buffer
    for p, name in files:
        if not p.exists():
            continue
        out.write((json.dumps({'path': name, 'size': p.stat().st_size}) + '\n').encode())
        with p.open('rb') as f:
            shutil.copyfileobj(f, out)
    out.write(b'{"end":true}\n')
    out.flush()


def main(request):
    root = Path(request['root'])
    action = request['action']
    if action == 'inventory':
        with locked(root):
            result = inventory(root)
    elif action == 'sync':
        result = sync(root, request, sys.stdin.buffer)
    elif action == 'submit':
        result = submit(root, request)
    elif action in ('status', 'fetch'):
        name = request['job']
        if not name or any(c not in '0123456789_-' for c in name):
            raise ValueError('Invalid job id')
        job = safe_path(root / 'jobs', name)
        if action == 'fetch':
            fetch(job)
            return
        result = job_status(job)
    else:
        raise ValueError('Unknown action')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        if sys.argv[1] == '--worker':
            worker(Path(sys.argv[2]))
        elif sys.argv[1] == '--stdin-json':
            main(json.loads(sys.stdin.buffer.readline()))
        else:
            main(json.loads(base64.b64decode(sys.argv[1])))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
