"""CPU-only backend tests. All generated video is synthetic, not headset evidence."""
import unittest
from unittest.mock import Mock, call, patch
from pathlib import Path
import tempfile
import json
import os
import shutil
import subprocess
import sys
import time

from frame_testbench import recording


class HeadsetValidationTests(unittest.TestCase):
    def test_headset_accepts_one_to_sixty_fps(self):
        for fps in (1, 10, 30, 60):
            recording.validate(2, fps, 'headset')

    def test_invalid_requests_do_not_touch_dependencies_or_runtime(self):
        invalid = [(0, 30, 'headset'), (61, 30, 'headset'), (True, 30, 'headset'),
                   (1.5, 30, 'headset'), (2, 0, 'headset'), (2, 61, 'headset'),
                   (2, True, 'headset'), (2, 1.5, 'headset'), (2, 30, 'bad'),
                   (2, 11, 'preview'), (2, 11, 'stereo')]
        native = Mock()
        with patch.object(recording.shutil, 'which') as which:
            for args in invalid:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    recording.record('/unused', *args, native)
            native.assert_not_called()
            which.assert_not_called()

    def test_legacy_views_keep_low_rate_limit(self):
        for view in ('preview', 'stereo'):
            recording.validate(1, 1, view)
            recording.validate(60, 10, view)

    def test_existing_output_rejected_before_dependency_or_runtime_access(self):
        native = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.object(recording.shutil, 'which') as which:
            for out in (Path(tmp), Path(tmp) / 'link'):
                if out.name == 'link':
                    out.symlink_to(Path(tmp) / 'absent')
                with self.subTest(output=out), self.assertRaises(FileExistsError):
                    recording.record(out, 2, 30, 'headset', native)
            native.assert_not_called()
            which.assert_not_called()


class HeadsetBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='headset-backend-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "space and 'quote"
        self.native = Mock(return_value={'ok': True, 'command': 'status', 'hmd': {}})

    def module(self):
        try:
            from frame_testbench import headset_recording
        except ImportError:
            self.fail('continuous headset recording backend is missing')
        return headset_recording

    def executable(self, name='fake-framecorder', body='pass'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!{sys.executable}\n' + body + '\n')
        path.chmod(0o700)
        return path

    def record(self, duration=2, fps=30):
        return recording.record(self.out, duration, fps, 'headset', self.native)

    def test_missing_framecorder_instructs_explicit_setup_before_runtime(self):
        module = self.module()
        with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(self.root / 'missing')}), \
             patch.object(module.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'python3 scripts/setup-framecorder.py'):
                self.record()
        popen.assert_not_called()
        self.native.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_dependency_resolution_prefers_explicit_then_pinned_then_path(self):
        module = self.module()
        override = self.executable('override')
        pinned = self.executable('tools/framecorder-v0.1.1/framecorder')
        other = self.executable('path-framecorder')
        with patch.object(module, 'ROOT', self.root), \
             patch.object(module.shutil, 'which', return_value=str(other)), \
             patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(override)}):
            self.assertEqual(Path(module.resolve_framecorder()), override)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(Path(module.resolve_framecorder()), pinned)
                pinned.unlink()
                self.assertEqual(Path(module.resolve_framecorder()), other)

    def test_missing_ffprobe_prevents_runtime_and_launcher(self):
        module = self.module()
        binary = self.executable()
        with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}), \
             patch.object(module.shutil, 'which', return_value=None), \
             patch.object(module.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'ffprobe'):
                self.record()
        self.native.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_runtime_failure_prevents_launcher_and_output_writes(self):
        module = self.module()
        binary = self.executable()
        for status in ({'ok': False}, {}, None, {'ok': 1}, {'runtime': {'ok': True}}):
            self.native.return_value = status
            with self.subTest(status=status), \
                 patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}), \
                 patch.object(module.subprocess, 'Popen') as popen:
                with self.assertRaisesRegex(RuntimeError, 'runtime'):
                    self.record()
                popen.assert_not_called()
                self.assertFalse(self.out.exists())
        self.assertEqual(self.native.call_args_list, [call('status')] * 5)

    def test_runtime_exception_propagates_before_launcher(self):
        module = self.module()
        binary = self.executable()
        self.native.side_effect = RuntimeError('runtime offline')
        with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}), \
             patch.object(module.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'runtime offline'):
                self.record()
        popen.assert_not_called()
        self.assertFalse(self.out.exists())

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
    def test_real_synthetic_video_preserves_encoded_pts_and_safe_launcher_arguments(self):
        module = self.module()
        fixture = self.root / 'synthetic-testsrc.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=160x90:rate=30:duration=2', '-c:v', 'libx264',
                        '-threads', '1', '-bf', '0', '-pix_fmt', 'yuv420p', '-an', str(fixture)],
                       stdin=subprocess.DEVNULL, check=True, timeout=15)
        binary = self.executable(body='''import json, os, pathlib, shutil, sys
pathlib.Path('invocation.json').write_text(json.dumps({'argv':sys.argv[1:], 'stdin':sys.stdin.read(),
    'pid':os.getpid(), 'pgid':os.getpgrp(), 'sid':os.getsid(0)}))
print('local recorder stdout')
print('local recorder stderr', file=sys.stderr)
shutil.copyfile(''' + repr(str(fixture)) + ''', sys.argv[-1])''')
        with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}):
            data = self.record()
        self.native.assert_called_once_with('status')
        invocation = json.loads((self.out / 'invocation.json').read_text())
        self.assertEqual(invocation['argv'][:-1], ['--source', 'headset', '--view', 'eye',
                         '--eye', 'left', '--aspect', '16:9', '--codec', 'h264', '--fps', '30',
                         '--duration', '2', '--bitrate', '12', '--no-audio', '--gpu-priority', 'low'])
        self.assertEqual(invocation['stdin'], '')
        self.assertEqual(invocation['pid'], invocation['pgid'])
        self.assertEqual(invocation['pid'], invocation['sid'])
        self.assertTrue(data['ok'])
        self.assertEqual(data['source'], 'openvr_headset_view')
        self.assertEqual(data['backend'], 'framecorder')
        self.assertEqual(data['view'], 'headset')
        self.assertEqual(data['codec'], 'h264')
        self.assertEqual((data['width'], data['height']), (160, 90))
        self.assertEqual(data['encoded_frame_count'], 60)
        self.assertAlmostEqual(data['encoded_duration_seconds'], 2, places=3)
        self.assertEqual(data['requested_fps'], 30)
        self.assertEqual(data['requested_duration_seconds'], 2)
        self.assertEqual(data['timing_basis'], 'encoded_video_pts')
        self.assertIn('exposure', data['timing_note'])
        self.assertNotIn('sample_count', data)
        self.assertNotIn('frames', data)
        self.assertEqual(set(data['files']), {'recording.mp4', 'timeline.json'})
        self.assertEqual(list(self.out.rglob('*.png')), [])
        timeline = json.loads((self.out / 'timeline.json').read_text())
        actual = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_packets',
                            '-show_entries', 'packet=pts_time', '-of', 'json', data['files']['recording.mp4']]))
        self.assertEqual(timeline['video_pts_seconds'], [float(p['pts_time']) for p in actual['packets']])
        self.assertEqual(json.loads((self.out / 'result.json').read_text()), data)
        self.assertIn('local recorder stdout', (self.out / 'framecorder.log').read_text())
        self.assertIn('local recorder stderr', (self.out / 'framecorder.log').read_text())
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)

    def assert_no_success(self):
        for name in ('result.json', 'timeline.json', 'recording.mp4'):
            self.assertFalse((self.out / name).exists(), name)

    def test_launcher_failure_never_falls_back_or_publishes_success(self):
        self.module()
        binary = self.executable(body='import sys; print("capture failed", file=sys.stderr); sys.exit(7)')
        with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}):
            with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                self.record()
        self.native.assert_called_once_with('status')
        self.assert_no_success()
        self.assertIn('capture failed', (self.out / 'framecorder.log').read_text())

    def test_missing_empty_not_mp4_symlink_and_oversized_media_refused(self):
        self.module()
        bodies = ['pass', 'pathlib.Path(sys.argv[-1]).touch()',
                  'pathlib.Path(sys.argv[-1]).write_bytes(b"not a video" * 10)',
                  'pathlib.Path(sys.argv[-1]).symlink_to(__file__)',
                  'f=open(sys.argv[-1], "wb"); f.write(b"\\x00\\x00\\x00\\x18ftypisom"); f.truncate(512*1024*1024+1); f.close()']
        for i, body in enumerate(bodies):
            self.out = self.root / f'bad-media-{i}'
            binary = self.executable(body='import pathlib, sys\n' + body)
            with self.subTest(body=body), patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}):
                with self.assertRaises((ValueError, RuntimeError)):
                    self.record()
                self.assert_no_success()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
    def test_real_invalid_videos_are_not_successful_recordings(self):
        self.module()
        cases = [('short', '0.2', 'libx264', False), ('long', '4', 'libx264', False),
                 ('wrong-codec', '2', 'mpeg4', False), ('audio', '2', 'libx264', True),
                 ('one-frame', '0.033334', 'libx264', False), ('truncated', '2', 'libx264', False)]
        for name, duration, codec, audio in cases:
            with self.subTest(case=name):
                fixture = self.root / (name + '.mp4')
                argv = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        f'testsrc2=size=160x90:rate=30:duration={duration}']
                if audio:
                    argv += ['-f', 'lavfi', '-i', 'sine=duration=2', '-c:a', 'aac']
                argv += ['-c:v', codec, '-threads', '1', '-bf', '0', '-movflags', '+faststart']
                if name == 'one-frame':
                    argv += ['-frames:v', '1']
                subprocess.run(argv + [str(fixture)], stdin=subprocess.DEVNULL, check=True, timeout=15)
                if name == 'truncated':
                    contents = fixture.read_bytes()
                    fixture.write_bytes(contents[:len(contents)//2])
                self.out = self.root / name
                binary = self.executable(body='import shutil, sys\nshutil.copyfile(' + repr(str(fixture)) + ', sys.argv[-1])')
                with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}):
                    with self.assertRaises((ValueError, RuntimeError, subprocess.CalledProcessError)):
                        self.record()
                self.assert_no_success()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
    def test_low_fps_does_not_allow_early_stop_by_a_whole_frame(self):
        self.module()
        fixture = self.root / 'synthetic-short-low-fps.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=160x90:rate=2:duration=1.5', '-c:v', 'libx264',
                        '-threads', '1', '-bf', '0', '-an', str(fixture)],
                       stdin=subprocess.DEVNULL, check=True, timeout=15)
        binary = self.executable(body='import shutil, sys\nshutil.copyfile(' + repr(str(fixture)) + ', sys.argv[-1])')
        with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}):
            with self.assertRaisesRegex(ValueError, 'duration'):
                self.record(duration=2, fps=2)
        self.assert_no_success()

    def test_timeout_escalates_kills_process_group_and_reaps_launcher(self):
        module = self.module()
        binary = self.executable(body='''import json, os, pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path('pids.json').write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(60)''')
        started = time.monotonic()
        with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}), \
             patch.object(module, 'CAPTURE_OVERHEAD_SECONDS', -1.7), \
             patch.object(module, 'STOP_GRACE_SECONDS', .1):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                self.record()
        self.assertLess(time.monotonic() - started, 3)
        pids = json.loads((self.out / 'pids.json').read_text())
        for pid in pids:
            deadline = time.monotonic() + 2
            while self.process_running(pid) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(self.process_running(pid), f'process {pid} left running')
        with self.assertRaises(ChildProcessError):
            os.waitpid(pids[0], os.WNOHANG)
        self.assert_no_success()

    @staticmethod
    def process_running(pid):
        try:
            return Path(f'/proc/{pid}/stat').read_text().split(')')[1].split()[0] != 'Z'
        except (FileNotFoundError, ProcessLookupError):
            return False

    def test_nonzero_launcher_exit_also_cleans_surviving_child(self):
        self.module()
        binary = self.executable(body='''import json, os, pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path('pids.json').write_text(json.dumps([os.getpid(), child.pid]))
sys.exit(9)''')
        with patch.dict(os.environ, {'FRAME_TESTBENCH_FRAMECORDER': str(binary)}):
            with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
                self.record()
        pids = json.loads((self.out / 'pids.json').read_text())
        deadline = time.monotonic() + 2
        while self.process_running(pids[1]) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.process_running(pids[1]))
        with self.assertRaises(ChildProcessError):
            os.waitpid(pids[0], os.WNOHANG)
        self.assert_no_success()
