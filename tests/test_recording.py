"""CPU recording fixtures use generated solid-color PNGs, never headset pixels."""
import contextlib
import io
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch


def png(path, rgb):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 32, 24, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress((b'\0' + bytes(rgb) * 32) * 24)) + chunk(b'IEND', b''))


class RecordingTests(unittest.TestCase):
    def module(self):
        try:
            from frame_testbench import recording
        except ImportError:
            self.fail('recording implementation missing')
        return recording

    def native(self, command, output, duration, fps):
        self.assertEqual(command, 'record')
        root = Path(output)
        self.assertFalse(root.exists())
        root.mkdir()
        frames = []
        for i, t in enumerate([.1, .5, 1.6]):
            directory = root / f'frame-{i:06d}'
            directory.mkdir()
            for name in ('preview', 'stereo'):
                png(directory / (name + '.png'), [(255, 0, 0), (0, 255, 0), (0, 0, 255)][i])
            frames.append(dict(index=i, request_seconds=t-.05, completed_seconds=t,
                               preview=str(directory / 'preview.png'), stereo=str(directory / 'stereo.png')))
        return dict(ok=True, command='record', source='openvr_screenshots', frames=frames,
                    requested_duration_seconds=duration, requested_fps=fps, elapsed_seconds=2.0,
                    capture_scope='synthetic fixture')

    def test_reject_invalid_parameters_before_io(self):
        module = self.module()
        for duration, fps, view in [(0,5,'stereo'), (61,5,'stereo'), (True,5,'stereo'),
                                   (1.5,5,'stereo'), (10,0,'stereo'), (10,11,'stereo'),
                                   (10,True,'stereo'), (10,5,'bad'), (10,11,'preview'),
                                   (10,61,'headset'), (10,0,'headset'), (10,True,'headset'),
                                   (10,30.0,'headset'), (10,'30','headset')]:
            with self.subTest(args=(duration, fps, view)):
                with self.assertRaises(ValueError):
                    module.validate(duration, fps, view)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
    def test_real_encoding_preserves_measured_intervals_and_color_changes(self):
        module = self.module()
        with tempfile.TemporaryDirectory(prefix='video-') as tmp:
            out = Path(tmp) / "space and 'quote"
            data = module.record(out, 2, 5, 'stereo', self.native)
            self.assertTrue(data['ok'])
            self.assertEqual(data['sample_count'], 3)
            self.assertEqual(data['timing_basis'], 'screenshot_completion_monotonic')
            self.assertAlmostEqual(data['video_duration_seconds'], 1.7)
            timeline = json.loads((out / 'timeline.json').read_text())
            self.assertEqual(timeline['sample_count'], 3)
            self.assertAlmostEqual(timeline['frames'][1]['video_pts_seconds'], .4)
            self.assertAlmostEqual(timeline['frames'][2]['video_pts_seconds'], 1.5)
            probe = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format',
                                                        '-of','json', data['files']['recording.mp4']]))
            self.assertEqual(probe['streams'][0]['codec_name'], 'h264')
            self.assertAlmostEqual(float(probe['format']['duration']), 1.7, delta=.08)
            decoded = subprocess.check_output(['ffmpeg','-v','error','-i',data['files']['recording.mp4'],
                                               '-vsync','0','-f','rawvideo','-pix_fmt','rgb24','-'])
            frame_size = 32 * 24 * 3
            self.assertGreaterEqual(len(decoded), frame_size*3)
            for i, channel in enumerate([0,1,2]):
                pixel = decoded[i*frame_size:i*frame_size+3]
                self.assertGreater(pixel[channel], 200)
                self.assertLess(sum(pixel)-pixel[channel], 30)
            self.assertTrue((out/'result.json').is_file())
            with self.assertRaises(FileExistsError):
                module.record(out, 2, 5, 'stereo', self.native)

    def test_missing_encoder_fails_before_native(self):
        module = self.module()
        with patch.object(module.shutil,'which',return_value=None), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'ffmpeg'):
                module.record(Path('/unused'), 2, 5, 'stereo', lambda *a: self.fail('must not capture'))

    def test_untrusted_native_timestamps_and_paths_refused_before_encoder(self):
        module = self.module()
        mutations = [lambda d: d['frames'][1].update(completed_seconds=float('nan')),
                     lambda d: d['frames'][1].update(completed_seconds=.01),
                     lambda d: d['frames'][0].update(stereo='/etc/passwd'),
                     lambda d: d.update(frames=d['frames'][:1]),
                     lambda d: d.update(ok=False)]
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as tmp:
                def native(*args):
                    data=self.native(*args); mutate(data); return data
                with patch.object(module.shutil,'which',side_effect=lambda name:'/fake/'+name), \
                     patch.object(module.subprocess,'run') as run:
                    with self.assertRaises((ValueError,RuntimeError)):
                        module.record(Path(tmp)/'out',2,5,'stereo',native)
                    run.assert_not_called()

    def test_encoder_failure_keeps_samples_without_success(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'out'
            with patch.object(module.shutil,'which',side_effect=lambda name:'/fake/'+name), \
                 patch.object(module.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['ffmpeg'])):
                with self.assertRaises(subprocess.CalledProcessError):
                    module.record(out,2,5,'preview',self.native)
            self.assertTrue((out/'frame-000000/preview.png').exists())
            self.assertFalse((out/'result.json').exists())

    def test_headset_validation_accepts_full_rate_range(self):
        for fps in (1, 30, 60):
            with self.subTest(fps=fps):
                self.module().validate(10, fps, 'headset')

    def test_cli_record_defaults_and_explicit_views(self):
        from frame_testbench import cli
        args = cli.parser().parse_args(['record'])
        self.assertEqual((args.duration, args.fps, args.view), (10, 30, 'headset'))
        for view, fps in [('headset', 60), ('preview', 10), ('stereo', 10)]:
            args = cli.parser().parse_args(['record', '--view', view, '--fps', str(fps)])
            self.assertEqual((args.view, args.fps), (view, fps))
        # Selecting a sampled view must not silently lower the default rate.
        args = cli.parser().parse_args(['record', '--view', 'preview'])
        self.assertEqual(args.fps, 30)

    def test_cli_record_help_distinguishes_continuous_and_sampled_views(self):
        from frame_testbench import cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            cli.parser().parse_args(['record', '--help'])
        text = ' '.join(output.getvalue().split()).lower()
        for phrase in ['headset', '30', '1..60', '1..10', 'rootless', '1920x1080',
                       'left-eye', 'framecorder', 'vulkan', 'iris', 'no audio', 'no png',
                       'active compositor', 'pose', 'power', 'sampled', 'screenshots']:
            self.assertIn(phrase, text)

    def test_cli_invalid_recording_never_creates_directory_or_starts_io(self):
        from frame_testbench import cli
        cases = [('headset', '61'), ('headset', '0'), ('preview', '11'), ('stereo', '30')]
        for host in ([], ['--host', 'frame']):
            for view, fps in cases:
                with self.subTest(host=host, view=view, fps=fps), tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / 'new-parent' / 'recording'
                    with patch.object(cli.transport, 'run_json') as remote, \
                         patch.object(cli.recording, 'record') as record, \
                         patch.object(cli.recording, 'fetch') as fetch, \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main([*host, 'record', '--view', view, '--fps', fps,
                                                  '--fetch' if host else '--output', str(target)]), 1)
                    remote.assert_not_called()
                    record.assert_not_called()
                    fetch.assert_not_called()
                    self.assertFalse(target.parent.exists())

    def test_cli_defaults_dispatch_and_validation_before_ssh(self):
        from frame_testbench import cli
        self.assertIn('record', cli.parser()._subparsers._group_actions[0].choices)
        args = cli.parser().parse_args(['record'])
        self.assertEqual((args.duration,args.fps,args.view), (10,30,'headset'))
        bench=cli.Bench()
        with patch.object(bench,'record',return_value={'ok':True}) as record:
            cli.dispatch(args,bench)
        record.assert_called_once_with(None,10,30,'headset')
        with patch.object(cli.transport,'run_json') as remote, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--host','frame','record','--duration','61']),1)
        remote.assert_not_called()

    def test_cli_remote_record_forwards_and_fetches_with_long_timeout(self):
        from frame_testbench import cli
        reply={'ok':True}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(cli.transport,'run_json',return_value=reply) as run, \
             patch.object(cli.recording,'fetch',return_value=reply) as fetch, \
             contextlib.redirect_stdout(io.StringIO()):
            out=str(Path(tmp)/'new')
            self.assertEqual(cli.main(['--host','frame','--remote-root','dev/my bench','record',
                                      '--duration','60','--fps','2','--view','preview','--fetch',out]),0)
            argv=run.call_args.args[0]
            self.assertNotIn('--fetch',argv[-1])
            self.assertIn("'dev/my bench/frame-testbench'",argv[-1])
            self.assertEqual(run.call_args.kwargs['timeout'],300)
            fetch.assert_called_once_with('frame',reply,out)

    def test_existing_fetch_target_refused_before_capture(self):
        from frame_testbench import cli
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli.transport,'run_json') as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--host','frame','record','--view','stereo','--fps','5','--fetch',tmp]),1)
            run.assert_not_called()

    def test_fetch_validates_media_and_uses_strict_ssh(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out'
            data={'ok':True,'files':{'recording.mp4':'/remote/recording.mp4','timeline.json':'/remote/timeline.json'}}
            def copy(argv,**kw):
                self.assertIn('StrictHostKeyChecking=yes',argv)
                self.assertIn('BatchMode=yes',argv)
                path=Path(argv[-1])
                path.write_bytes(b'\x00\x00\x00\x18ftypisom'+b'\0'*16 if path.suffix=='.mp4' else b'{"ok":true}')
            with patch.object(module.subprocess,'run',side_effect=copy):
                fetched=module.fetch('frame',data,out)
            self.assertEqual(fetched['local_files']['recording.mp4'],str(out/'recording.mp4'))
            self.assertTrue((out/'result.json').is_file())
            with self.assertRaises(FileExistsError):
                module.fetch('frame',data,out)

    def test_fetch_rejects_remote_paths_before_copy(self):
        module=self.module()
        for remote in ['relative/recording.mp4','/remote/wrong.mp4','/remote/\nrecording.mp4']:
            with self.subTest(path=remote),tempfile.TemporaryDirectory() as tmp,patch.object(module.subprocess,'run') as run:
                with self.assertRaises(ValueError):
                    module.fetch('frame',{'ok':True,'files':{'recording.mp4':remote,'timeline.json':'/remote/timeline.json'}},Path(tmp)/'out')
                run.assert_not_called()
