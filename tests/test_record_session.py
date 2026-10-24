"""CPU processes and synthetic media only; never loads a native VR dependency."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from frame_testbench import cli, record_session as rs


FAKE = r'''import json, os, pathlib, select, signal, sys, time
mode = os.environ.get('RECORDER_TEST_MODE', '')
path = pathlib.Path(sys.argv[-1])
pathlib.Path('invocation.json').write_text(json.dumps({'argv':sys.argv[1:], 'pid':os.getpid(), 'pgid':os.getpgrp()}))
print('fake native diagnostic', file=sys.stderr, flush=True)
def announce():
    if mode != 'missing_ack': print('recording ' + str(path), flush=True)
announce()
last = time.monotonic()
commands = []
captured = 0
while True:
    if mode != 'stall' and time.monotonic() - last >= .1:
        captured += 3
        count = 0 if mode == 'zero' else 3 if mode == 'repeat' else 6 if mode == 'regress' and captured==3 else 3 if mode == 'regress' else captured
        print('[INFO] 30.0 fps, ' + str(count) + ' frames, 0 dropped, 1.00 ms gpu per frame', file=sys.stderr, flush=True)
        last = time.monotonic()
    if not select.select([sys.stdin], [], [], .02)[0]: continue
    line = sys.stdin.readline()
    if not line:
        if mode == 'ignore_quit':
            time.sleep(.02)
            continue
        break
    line = line.rstrip('\n')
    commands.append(line)
    pathlib.Path('commands.json').write_text(json.dumps(commands))
    if line == 'stop-record':
        if mode == 'missing_saved': continue
        path.write_bytes(b'\0\0\0\x18ftypisom' + b'xxxx')
        if mode == 'bad_path': print('saved recording 16 1.0 /tmp/foreign.mp4', flush=True)
        else: print('saved recording 16 1.0 ' + str(path), flush=True)
    elif line.startswith('record '):
        path = pathlib.Path(line[7:])
        if mode == 'collision': path.write_bytes(b'collision')
        announce()
    elif line == 'quit':
        if mode == 'ignore_quit': continue
        break
    else: raise RuntimeError('unexpected command')
if mode != 'no_footer': print('[INFO] done: 30 frames, 0.1 MB', file=sys.stderr, flush=True)
'''


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.paths = [Path('/private') / f'segment-{i}.mp4' for i in range(7)]
        self.events = Mock()
        self.p = rs.Protocol(self.paths, self.events)
        self.s = self.p.requested(self.paths[0], 10)

    def test_control_ack_round_trip_and_progress(self):
        self.p.line('stdout', 'recording ' + str(self.paths[0]), 10.1)
        self.s['stopped_monotonic'] = 12
        self.p.line('stdout', 'saved recording 1000 2.0 ' + str(self.paths[0]), 12.3)
        self.p.line('stderr', '[INFO] 30.0 fps, 60 frames, 0 dropped, 1.00 ms gpu per frame', 12.4)
        self.p.line('stderr', '[INFO] done: 60 frames, 0.1 MB', 12.5)
        self.assertTrue(self.p.done)
        self.assertEqual(self.s['announced_bytes'], 1000)

    def test_unexpected_failed_duplicate_and_out_of_order_acks(self):
        cases = ['recording /foreign', 'saved recording 20 1.0 ' + str(self.paths[0]),
                 'failed recording writer failed', 'failed command no', 'arbitrary output']
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.p.line('stdout', text, 10.2)
        self.p.line('stdout', 'recording ' + str(self.paths[0]), 10.2)
        with self.assertRaises(ValueError):
            self.p.line('stdout', 'recording ' + str(self.paths[0]), 10.3)

    def test_late_ack_and_overlong_or_duplicate_save(self):
        with self.assertRaisesRegex(ValueError, 'late'):
            self.p.line('stdout', 'recording ' + str(self.paths[0]), 31)
        self.p.line('stdout', 'recording ' + str(self.paths[0]), 10.2)
        self.s['stopped_monotonic'] = 11
        for text, at in [('saved recording 100 1.0 ', 20), ('saved recording 100 60.1 ', 11.2)]:
            with self.assertRaises(ValueError):
                self.p.line('stdout', text + str(self.paths[0]), at)

    def test_progress_requires_positive_advancement_and_finite_fps(self):
        def line(count, fps='30.0'):
            return '[INFO] ' + fps + ' fps, ' + str(count) + ' frames, 0 dropped, 1.00 ms gpu per frame'
        self.p.line('stderr', line(0), 11)
        self.assertIsNone(self.p.first_progress)
        self.assertIsNone(self.p.progress_at)
        self.p.line('stderr', line(3), 12)
        self.p.line('stderr', line(3), 13)
        self.assertEqual(self.p.first_progress, 12)
        self.assertEqual(self.p.progress_at, 12)
        with self.assertRaisesRegex(ValueError, 'regressed'): self.p.line('stderr', line(2), 14)
        for fps in ('nan', 'inf', '-1', 'not-a-number'):
            with self.assertRaises(ValueError): self.p.line('stderr', line(4, fps), 14)
        with self.assertRaises(ValueError): self.p.line('stderr', '30.0 fps, broken frames,', 14)

    def test_invalid_values_and_legacy_limit_are_independent(self):
        for values in [(331, 50, 30), (True, 50, 30), (10, 51, 30), (10, 50, False), (0, 1, 1)]:
            with self.assertRaises(ValueError): rs.validate(*values)
        rs.validate(330, 50, 60)
        from frame_testbench import recording
        with self.assertRaises(ValueError): recording.validate(61, 30, 'headset')

    def test_segment_count_and_real_file_collision_refuse(self):
        for path in self.paths[1:]: self.p.requested(path, 10)
        with self.assertRaises(ValueError): self.p.requested(Path('/foreign/eighth.mp4'), 10)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'owned.mp4'
            path.write_bytes(b'foreign collision')
            with self.assertRaises(FileExistsError): rs.Protocol([path], Mock()).requested(path, 10)
            self.assertEqual(path.read_bytes(), b'foreign collision')

    def test_cli_new_validation_precedes_transport(self):
        with patch.object(cli.transport, 'run_json') as remote, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--host', 'frame', 'record-session', '--duration', '331', '--output', '/unused']), 1)
            remote.assert_not_called()

    def test_atomic_json_create_collision_preserves_foreign_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'session.json'
            target.write_bytes(b'old receipt')
            foreign = target.with_name('.' + 'a' * 32 + '.tmp')
            foreign.write_bytes(b'foreign collision')
            with patch.object(rs.uuid, 'uuid4', return_value=Mock(hex='a' * 32)):
                with self.assertRaises(FileExistsError): rs.atomic_json(target, {'ok':True})
            self.assertEqual(foreign.read_bytes(), b'foreign collision')
            self.assertEqual(target.read_bytes(), b'old receipt')
            with self.assertRaises(FileExistsError): rs.atomic_json(target, {'ok':True}, replace=False)
            self.assertEqual(target.read_bytes(), b'old receipt')

    def test_cli_new_remote_bound_and_no_lease_dispatch(self):
        with patch.object(cli.transport, 'run_json', return_value={'ok': True}) as remote, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--host', 'frame', 'record-session', '--output', '/fresh']), 0)
            self.assertEqual(remote.call_args.kwargs['timeout'], 350)
        with patch.object(cli.record_session, 'capture', return_value={'ok': False}) as capture, \
             patch.object(cli.session, 'dispatch') as lease, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['record-session', '--output', '/fresh']), 1)
            capture.assert_called_once()
            lease.assert_not_called()


@unittest.skipUnless(sys.platform == 'linux', 'real /proc and POSIX process-group fixtures')
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='record-session-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'fresh space output'
        self.observer = self.executable('observer', "import json\nprint(json.dumps({'ok':True}))")
        self.binary = self.executable('framecorder', FAKE)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER':str(self.binary),
                                                        'FRAME_TESTBENCH_OBSERVER':str(self.observer)}))
        for name, value in [('NORMAL_QUIT', .15), ('ESCALATION', .1), ('ACK_TIMEOUT', .4),
                            ('START_TIMEOUT', .7), ('PROGRESS_TIMEOUT', .4)]:
            self.stack.enter_context(patch.object(rs, name, value))

    def executable(self, name, body):
        path = self.root / name
        path.write_text('#!' + sys.executable + '\n' + body + '\n')
        path.chmod(0o700)
        return path

    def capture(self, mode='', **kw):
        with patch.dict(os.environ, {'RECORDER_TEST_MODE':mode}):
            return rs.capture(self.out, duration=2, segment_seconds=1, **kw)

    def test_one_precheck_one_owned_process_rotation_and_durable_closure(self):
        result = self.capture()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['runtime_closed'])
        self.assertFalse(result['media_verified'])
        self.assertEqual(len(result['clients']), 2)
        self.assertEqual([c['actual_exit'] for c in result['clients']], [0, 0])
        self.assertGreaterEqual(len(result['segments']), 2)
        invocation = json.loads((self.out / 'invocation.json').read_text())
        self.assertEqual(invocation['pid'], invocation['pgid'])
        commands = json.loads((self.out / 'commands.json').read_text())
        self.assertEqual(commands[-1], 'quit')
        self.assertEqual(commands[0], 'stop-record')
        self.assertTrue(commands[1].startswith('record ' + str(self.out)))
        events = [json.loads(s) for s in (self.out / 'events.jsonl').read_text().splitlines()]
        self.assertIn('ready', [e['event'] for e in events])
        self.assertIn('progress', [e['event'] for e in events])
        self.assertEqual(json.loads((self.out / 'session.json').read_text())['ok'], True)
        self.assertIn('fake native diagnostic', (self.out / 'framecorder.stderr').read_text())

    def test_missing_ack_progress_stall_unknown_paths_and_footer_refuse(self):
        for mode in ['missing_ack', 'missing_saved', 'stall', 'bad_path', 'no_footer', 'zero', 'repeat', 'regress']:
            self.out = self.root / mode
            result = self.capture(mode)
            self.assertFalse(result['ok'], mode)
            self.assertTrue(result['runtime_closed'], result)
            self.assertIsNotNone(result['first_failure'])

    def test_cancellation_preserves_zero_media_credit_and_actual_shutdown(self):
        began = time.monotonic()
        result = self.capture(cancel=lambda: time.monotonic() - began > .3)
        self.assertFalse(result['ok'])
        self.assertIn('cancelled', result['first_failure'])
        self.assertTrue(result['runtime_closed'])
        self.assertFalse(result['media_verified'])

    def test_quit_timeout_escalates_without_claiming_normal_success(self):
        result = self.capture('ignore_quit')
        self.assertFalse(result['ok'])
        self.assertTrue(result['runtime_closed'])
        events = [json.loads(s) for s in (self.out / 'events.jsonl').read_text().splitlines()]
        self.assertTrue(any(e['event'] == 'signal' and e['signal'] == int(signal.SIGINT) for e in events))

    def test_precheck_hang_has_three_second_total_bound_no_recorder(self):
        self.observer.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(100)\n')
        began = time.monotonic()
        result = self.capture()
        self.assertLess(time.monotonic() - began, 3.5)
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['clients']), 1)
        self.assertTrue(result['runtime_closed'])
        self.assertFalse((self.out / 'invocation.json').exists())

    def test_existing_and_symlink_output_refused_without_processes(self):
        self.out.mkdir()
        with patch.object(rs.subprocess, 'Popen') as popen:
            with self.assertRaises(FileExistsError): self.capture()
            popen.assert_not_called()
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target, target_is_directory=True)
        self.out = link / 'fresh'
        with self.assertRaises(ValueError): self.capture()

    def test_pending_dispatch_and_failed_attach_never_become_empty_success(self):
        with patch.object(rs.subprocess, 'Popen', side_effect=OSError('synthetic dispatch error')):
            result = self.capture()
        self.assertFalse(result['ok'])
        self.assertFalse(result['runtime_closed'])
        self.assertEqual(result['clients'][0]['dispatch'], 'pending')

    def test_identity_persist_failure_reaps_owned_child_but_keeps_closure_unknown(self):
        original_save = rs.atomic_json
        original_popen = subprocess.Popen
        children = []
        failed = False
        def save(path, value):
            nonlocal failed
            if not failed and value['clients'] and value['clients'][0]['dispatch'] == 'started':
                failed = True
                raise OSError('synthetic attach persistence failure')
            original_save(path, value)
        def spawn(*a, **kw):
            child = original_popen(*a, **kw)
            children.append(child)
            return child
        with patch.object(rs, 'atomic_json', side_effect=save), patch.object(rs.subprocess, 'Popen', side_effect=spawn):
            result = self.capture()
        self.assertFalse(result['ok'])
        self.assertFalse(result['runtime_closed'])
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_actual_cli_reports_closed_session_without_any_lease_end(self):
        result = subprocess.run([sys.executable, str(rs.ROOT / 'frame-testbench'), 'record-session',
            '--output', str(self.out), '--duration', '12', '--segment-seconds', '1'],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value['runtime_closed'])
        self.assertFalse(value['media_verified'])
        self.assertLessEqual(len(value['segments']), 7)

    def test_group_census_failure_is_unknown_not_absence(self):
        with patch.object(rs, 'group_members', side_effect=PermissionError('synthetic proc read denied')):
            result = self.capture()
        self.assertFalse(result['ok'])
        self.assertFalse(result['runtime_closed'])

    def test_real_live_group_member_survives_launcher_exit_and_blocks_absence(self):
        code = ('import os,sys,time\nchild=os.fork()\n'
                'if child==0:\n time.sleep(10);os._exit(0)\n'
                'print(child,flush=True)\nsys.stdin.readline()\nos._exit(0)\n')
        process = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True, start_new_session=True)
        child = None
        try:
            child = int(process.stdout.readline())
            identity = rs.proc_identity(process.pid)
            process.stdin.write('exit\n')
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=1), 0)
            self.assertIn(child, [p['pid'] for p in rs.group_members(identity)])
        finally:
            if child:
                try: os.kill(child, signal.SIGKILL)
                except ProcessLookupError: pass
            if process.poll() is None: process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()

    def test_proc_identity_and_group_absence_are_real_not_ssh_exit(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(10)'], start_new_session=True)
        try:
            identity = rs.proc_identity(process.pid)
            self.assertEqual(identity['pid'], process.pid)
            self.assertGreater(identity['start_ticks'], 0)
            self.assertEqual([p['pid'] for p in rs.group_members(identity)], [process.pid])
        finally:
            process.kill()
            process.wait()
        self.assertEqual(rs.group_members(identity), [])


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='session-verify-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        nonce = '1' * 32
        self.path = self.root / f'segment-01-{nonce}.mp4'
        self.path.write_bytes(b'\0\0\0\x18ftypisomxxxx')
        self.manifest = dict(schema_version=1, kind='framecorder_control_session', nonce=nonce,
                             duration_budget_seconds=330, segment_seconds=50,
                             output=str(self.root), runtime_closed=True, ok=True, requested_fps=30,
                             capture_scope='synthetic fixture only', clients=[dict(name='precheck', actual_exit=0,
                               group_absent=True, pipes_drained=True), dict(name='framecorder', actual_exit=0,
                               group_absent=True, pipes_drained=True, normal_footer=True)],
                             segments=[dict(index=1, file=self.path.name, announced_bytes=16,
                                            sha256=rs.digest(self.path), file_identity=rs.file_identity(self.path))])
        for client in self.manifest['clients']:
            client.update(dispatch='started', identity=dict(pid=111, pgid=111, sid=111, start_ticks=222))
        self.save()

    def save(self):
        (self.root / 'session.json').write_text(json.dumps(self.manifest))

    def test_refuses_tampered_paths_hashes_partial_or_unknown_closure(self):
        original = copy.deepcopy(self.manifest)
        for mutation in [lambda m:m.update(runtime_closed=False), lambda m:m['segments'][0].update(file='../foreign'),
                         lambda m:m['segments'][0].update(file_identity={}),
                         lambda m:m['clients'][1].update(normal_footer=False),
                         lambda m:m['clients'][0].update(actual_exit=True)]:
            self.manifest = copy.deepcopy(original)
            mutation(self.manifest)
            self.save()
            with patch.object(rs.shutil, 'which', return_value='ffprobe'), self.assertRaises(ValueError):
                rs.verify(self.root)

    def test_same_size_file_rewrite_is_detected_before_decode(self):
        self.path.write_bytes(b'\0\0\0\x18ftypisomyyyy')
        with patch.object(rs.shutil, 'which', return_value='ffprobe'), self.assertRaisesRegex(ValueError, 'changed'):
            rs.verify(self.root)

    def test_bad_metadata_and_foreign_extra_files_are_typed_refusals(self):
        original = copy.deepcopy(self.manifest)
        for change in [lambda m:m.update(schema_version=True), lambda m:m.update(nonce=None),
                       lambda m:m.update(requested_fps=True), lambda m:m.update(segments=[None]),
                       lambda m:m['clients'][0].update(identity={'pid':True})]:
            self.manifest = copy.deepcopy(original)
            change(self.manifest)
            self.save()
            with self.assertRaises(ValueError): rs.verify(self.root)
        self.manifest = original
        self.save()
        (self.root / 'foreign.mp4.part').write_bytes(b'foreign')
        with self.assertRaisesRegex(ValueError, 'inventory'): rs.verify(self.root)

    def test_overlong_media_rejected_before_decoded_success(self):
        probe = subprocess.CompletedProcess([], 0, b'{"format":{"duration":"60.001"}}', b'')
        with patch.object(rs.shutil, 'which', return_value='ffprobe'), \
             patch.object(rs.subprocess, 'run', return_value=probe), patch.object(rs.legacy, '_verify') as decode:
            with self.assertRaisesRegex(ValueError, 'sixty'): rs.verify(self.root)
            decode.assert_not_called()

    def test_corruption_or_decoder_errors_never_mark_verified(self):
        probe = subprocess.CompletedProcess([], 0, b'{"format":{"duration":"1.0"}}', b'')
        with patch.object(rs.shutil, 'which', return_value='ffprobe'), \
             patch.object(rs.subprocess, 'run', return_value=probe), \
             patch.object(rs.legacy, '_verify', side_effect=ValueError('decoding errors')):
            with self.assertRaisesRegex(ValueError, 'decoding'): rs.verify(self.root)
        self.assertFalse(list(self.root.glob('verification-*.json')))

    def test_success_is_explicit_cpu_receipt_and_original_manifest_unchanged(self):
        before = (self.root / 'session.json').read_bytes()
        probe = subprocess.CompletedProcess([], 0, b'{"format":{"duration":"1.0"}}', b'')
        with patch.object(rs.shutil, 'which', return_value='ffprobe'), \
             patch.object(rs.subprocess, 'run', return_value=probe), \
             patch.object(rs.legacy, '_verify', return_value={'encoded_duration_seconds':1.0, 'encoded_frame_count':30}):
            result = rs.verify(self.root)
        self.assertTrue(result['media_verified'])
        self.assertEqual(result['total_encoded_seconds'], 1.0)
        self.assertEqual((self.root / 'session.json').read_bytes(), before)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires CPU FFmpeg tools')
    def test_real_synthetic_h264_decode_and_corrupt_media(self):
        self.path.unlink()
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=160x90:rate=30:duration=1', '-c:v', 'libx264',
                        '-threads', '1', '-bf', '0', '-pix_fmt', 'yuv420p', '-an', str(self.path)],
                       check=True, timeout=15)
        self.manifest['segments'][0].update(announced_bytes=self.path.stat().st_size,
            sha256=rs.digest(self.path), file_identity=rs.file_identity(self.path))
        self.save()
        result = rs.verify(self.root)
        self.assertTrue(result['media_verified'])
        self.assertEqual(result['segments'][0]['encoded_frame_count'], 30)
        self.assertAlmostEqual(result['total_encoded_seconds'], 1)
        self.path.write_bytes(b'\0\0\0\x18ftypisom' + b'not an mp4 container')
        self.manifest['segments'][0].update(announced_bytes=self.path.stat().st_size,
            sha256=rs.digest(self.path), file_identity=rs.file_identity(self.path))
        self.save()
        with self.assertRaises(ValueError): rs.verify(self.root)
        self.path.unlink()
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=32x32:rate=1:duration=61', '-c:v', 'libx264',
                        '-threads', '1', '-bf', '0', '-pix_fmt', 'yuv420p', '-an', str(self.path)],
                       check=True, timeout=15)
        self.manifest['segments'][0].update(announced_bytes=self.path.stat().st_size,
            sha256=rs.digest(self.path), file_identity=rs.file_identity(self.path))
        self.save()
        with self.assertRaisesRegex(ValueError, 'sixty'): rs.verify(self.root)


if __name__ == '__main__':
    unittest.main()
