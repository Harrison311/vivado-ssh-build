"""Tool-only tests: no SSH, Vivado, DUT/testbench or file digests."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import vivado_ssh as client

# Linux locking itself is NOT tested on Windows; substitute only its API here.
if sys.platform == 'win32':
    sys.modules['fcntl'] = types.SimpleNamespace(LOCK_EX=2, LOCK_UN=8, flock=lambda *args: None)
import remote_agent as agent


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = ROOT / 'tests' / '.runs' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        cls.base.mkdir(parents=True)

    def setUp(self):
        self.root = self.base / self._testMethodName
        self.root.mkdir()
        (self.root / agent.MARKER).write_text('vivado-ssh-v1')

    def upload(self, files, previous=None):
        manifest = {name: {'size': len(data), 'mtime_ns': 1700000000000000000}
                    for name, data in files.items()}
        delta = client.changes(manifest, previous or {})
        stream = io.BytesIO()
        for name in delta:
            stream.write((json.dumps({'path': name, 'size': len(files[name])}) + '\n').encode())
            stream.write(files[name])
        stream.write(b'{"end":true}\n')
        stream.seek(0)
        result = agent.sync(self.root, {'manifest': manifest, 'changes': delta}, stream)
        return manifest, result

    def test_delta_and_force(self):
        old = {'a.v': {'size': 3, 'mtime_ns': 1}}
        new = {'a.v': {'size': 3, 'mtime_ns': 2}, 'b.v': {'size': 1, 'mtime_ns': 1}}
        self.assertEqual(client.changes(new, old), ['a.v', 'b.v'])
        self.assertEqual(client.changes(old, old), [])
        self.assertEqual(client.changes(old, old, True), ['a.v'])

    def test_path_escape_rejected(self):
        for name in ('../outside', '/absolute', 'dir/../../outside'):
            with self.assertRaises(ValueError):
                client.relative_path(self.root, name)
            with self.assertRaises(ValueError):
                agent.safe_path(self.root, name)

    def test_sync_incremental_and_owned_deletion(self):
        initial, _ = self.upload({'rtl/a.v': b'a', 'rtl/b.v': b'b'})
        unmanaged = self.root / 'source' / 'keep.txt'
        unmanaged.write_text('not owned')
        latest, result = self.upload({'rtl/a.v': b'changed'}, initial)
        self.assertEqual(result, {'uploaded': 1, 'removed': 1})
        self.assertEqual(agent.inventory(self.root)['files'], latest)
        self.assertFalse((self.root / 'source/rtl/b.v').exists())
        self.assertTrue(unmanaged.exists())
        self.assertEqual((self.root / 'source/rtl/a.v').read_bytes(), b'changed')

    def test_interrupted_upload_does_not_commit(self):
        initial, _ = self.upload({'a.v': b'old'})
        request = {'manifest': {'a.v': {'size': 5, 'mtime_ns': 3}}, 'changes': ['a.v']}
        stream = io.BytesIO(b'{"path":"a.v","size":5}\nxx')
        with self.assertRaises(ValueError):
            agent.sync(self.root, request, stream)
        self.assertEqual((self.root / 'source/a.v').read_bytes(), b'old')
        self.assertEqual(agent.read(self.root / 'manifest.json'), initial)

    def test_concurrent_change_rejected(self):
        initial, _ = self.upload({'a.v': b'old'})
        (self.root / 'source/a.v').write_bytes(b'other version')
        with self.assertRaises(ValueError):
            agent.sync(self.root, {'manifest': initial, 'changes': []}, io.BytesIO(b'{"end":true}\n'))

    def test_snapshot_is_independent_and_manifest_only(self):
        manifest, _ = self.upload({'rtl/a.v': b'original'})
        (self.root / 'source/stale.v').write_bytes(b'untracked')
        cfg = {'Part': 'test-part', 'TopModule': 'top', 'Jobs': 1,
               'SourceFiles': ['rtl/a.v'], 'ConstraintFiles': [], 'IpSetupTcl': [], 'IncludeDirs': []}
        with patch.object(agent.subprocess, 'Popen', return_value=types.SimpleNamespace(pid=123)) as start:
            result = agent.submit(self.root, {'manifest': manifest, 'config': cfg, 'build_script': '# test only'})
            self.assertTrue(start.call_args.kwargs['start_new_session'])
        (self.root / 'source/rtl/a.v').write_bytes(b'later')
        snap = self.root / 'jobs' / result['job'] / 'src'
        self.assertEqual((snap / 'rtl/a.v').read_bytes(), b'original')
        self.assertFalse((snap / 'stale.v').exists())

    def test_tcl_string_cannot_inject_commands(self):
        encoded = agent.tcl_string('path with spaces/$x[exec bad];\n中文')
        self.assertNotIn('exec bad', encoded)
        self.assertTrue(encoded.startswith('[encoding convertfrom utf-8 [binary format H* {'))

    def test_settings_roundtrip_without_tcl86_binary_decode(self):
        import tkinter
        tcl = tkinter.Tcl()
        # Restrict binary to the operations available in the reported Vivado.
        tcl.eval('rename binary original_binary')
        tcl.eval('''proc binary {operation args} {
            if {$operation ne "format" && $operation ne "scan"} {
                error "unsupported binary operation"
            }
            return [uplevel 1 [linsert $args 0 original_binary $operation]]
        }''')
        values = ['', 'xcku040-ffva1156-2-i', '/home/user/project with spaces',
                  '中文路径/$x[error INJECTED];\\test\n{}"', 'line\r\nnext\tvalue']
        for value in values:
            tcl.eval('set roundtrip ' + agent.tcl_string(value))
            self.assertEqual(tcl.getvar('roundtrip'), value)

    def test_large_manifest_uses_stdin_not_command_line(self):
        cfg = {'SshTarget': 'test-only', 'RemoteProjectRoot': '/tmp/test-only',
               'LocalResultRoot': str(self.root)}
        connection = client.Client(cfg)
        manifest = {'rtl/file_%d.v' % i: {'size': 1, 'mtime_ns': 1} for i in range(2000)}
        packet = connection.request('sync', manifest=manifest, changes=[])
        self.assertGreater(len(packet), 32767)
        self.assertLess(len(' '.join(connection.command())), 1000)
        self.assertEqual(json.loads(packet)['manifest'], manifest)

    def test_worker_requires_exit_code_and_pass_marker(self):
        job = self.root / 'job'
        (job / 'results').mkdir(parents=True)
        (job / 'work').mkdir()
        agent.save(job / 'config.json', {'VivadoSettings': '/not/executed'})
        with patch.object(agent.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)):
            agent.worker(job)
        self.assertEqual(agent.read(job / 'status.json')['state'], 'FAILED')
        (job / 'results/build_status.txt').write_text('PASS\n')
        with patch.object(agent.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)):
            agent.worker(job)
        self.assertEqual(agent.read(job / 'status.json')['state'], 'PASSED')
        with patch.object(agent.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1)):
            agent.worker(job)
        self.assertEqual(agent.read(job / 'status.json')['state'], 'FAILED')

    def test_server_config_excludes_credentials(self):
        cfg = {'Password': 'test-only-secret', 'IdentityFile': 'private-key-path',
               'SshTarget': 'not-for-server', 'Part': 'part', 'Jobs': 2, 'SourceFiles': ['a.v']}
        safe = client.server_config(cfg)
        self.assertEqual(safe, {'Part': 'part', 'Jobs': 2, 'SourceFiles': ['a.v']})
        self.assertNotIn('test-only-secret', json.dumps(safe))

    def test_password_transport_supports_rich_text_stdout(self):
        from password_transport import PasswordTransport
        transport = PasswordTransport.__new__(PasswordTransport)
        process = types.SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(b'OK\n'), wait=lambda: 0)
        transport.popen = lambda command: process
        text_output = io.StringIO()  # Like Rich's stream proxy, no .buffer attribute.
        with patch('sys.stdout', text_output):
            result = transport.run('test-only', input=b'input', check=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(text_output.getvalue(), 'OK\n')

    def test_launcher_is_excluded_from_upload_directory(self):
        folder = self.root / 'scripts'
        folder.mkdir()
        launcher = folder / 'command.ps1'
        launcher.write_text("Password = 'test-only-secret'")
        rtl = self.root / 'top.v'
        rtl.write_text('module top; endmodule')
        cfg = {'LocalProjectRoot': str(self.root), 'SourceFiles': ['top.v'],
               'ExtraUploadPaths': ['scripts'], 'ConfigFile': str(launcher)}
        self.assertEqual(list(client.scan(cfg)), ['top.v'])

    def test_progress_uses_real_run_markers(self):
        results = self.root / 'results'
        results.mkdir()
        (results / 'phase_events.tsv').write_text('100000\tPreparing\n110000\tSynthesis\n200000\tImplementation\n')
        impl = self.root / 'work/project/build.runs/impl_1'
        impl.mkdir(parents=True)
        for name, stamp in [('.opt_design.begin.rst', 201), ('.opt_design.end.rst', 220),
                             ('.place_design.begin.rst', 221)]:
            path = impl / name
            path.write_text('')
            os.utime(path, (stamp, stamp))
        with patch.object(agent.time, 'time', return_value=240):
            result = agent.progress(self.root, 'RUNNING')
        self.assertEqual(result['current'], 'Implementation: place_design')
        rows = {r['name']: r for r in result['stages']}
        self.assertEqual(rows['Synthesis']['seconds'], 90)
        self.assertEqual(rows['Synthesis']['state'], 'DONE')
        self.assertEqual(rows['Implementation: opt_design']['seconds'], 19)
        self.assertEqual(rows['Implementation: place_design']['state'], 'RUNNING')
        self.assertEqual(rows['Implementation: route_design']['state'], 'PENDING')

    def test_live_watcher_fetches_success_and_failure(self):
        from progress_ui import watch_and_fetch
        for state in ('PASSED', 'FAILED'):
            fake = types.SimpleNamespace(cfg={'Part': 'test-part'},
                call=lambda *args, **kwargs: {'state': state, 'progress': {'stages': []}})
            fake.fetch = unittest.mock.Mock()
            with patch('progress_ui.Console') as console, patch('progress_ui.Live') as live:
                if state == 'FAILED':
                    with self.assertRaises(RuntimeError):
                        watch_and_fetch(fake, 'test-job')
                else:
                    watch_and_fetch(fake, 'test-job')
            fake.fetch.assert_called_once_with('test-job')


if __name__ == '__main__':
    unittest.main(verbosity=2)
