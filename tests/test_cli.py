import json
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def module(self):
        try:
            from frame_testbench import cli
        except ImportError:
            self.fail('CLI missing')
        return cli

    def bench(self):
        cli = self.module()
        from frame_testbench import pose
        class Fake(cli.Bench):
            def __init__(self):
                self.commands = []
                self.current = pose.from_euler([0, 0, 0], [0, 0, 0])
                self.on = False
            def native(self, *args):
                self.commands.append(('native', args))
                return {'transforms': {'raw_to_standing': [[1, 0, 0, 0], [0, 1, 0, 1.6], [0, 0, 1, 0]]}}
            def control(self, text):
                self.commands.append(('control', text))
                bits = text.split()
                if bits[0] == 'pose':
                    values = list(map(float, bits[1:]))
                    self.current = {'position': values[:3], 'quaternion': values[3:]}
                    self.on = True
                return {'ok': True, 'pose_override': self.on, 'pose': self.current}
        return Fake()

    def test_compositor_mode(self):
        cli, bench = self.module(), self.bench()
        cli.dispatch(cli.parser().parse_args(['compositor', 'awake']), bench)
        self.assertEqual(bench.commands[-1], ('native', ('setting', 'false')))
        cli.dispatch(cli.parser().parse_args(['compositor', 'auto']), bench)
        self.assertEqual(bench.commands[-1], ('native', ('setting', 'true')))

    def test_worn_and_release(self):
        cli, bench = self.module(), self.bench()
        for command, expected in [(['worn', 'on'], 'worn 1'), (['worn', 'off'], 'worn 0'),
                                  (['worn', 'physical'], 'worn-release'), (['release'], 'release')]:
            cli.dispatch(cli.parser().parse_args(command), bench)
            self.assertEqual(bench.commands[-1], ('control', expected))

    def test_absolute_standing_pose_is_converted_to_raw(self):
        cli, bench = self.module(), self.bench()
        result = cli.dispatch(cli.parser().parse_args(['pose', 'set', '--position', '1', '1.8', '-2',
                                                      '--rotation', '90', '0', '0']), bench)
        for a, b in zip(result['pose']['position'], [1, .2, -2]):
            self.assertAlmostEqual(a, b)
        self.assertTrue(result['pose_override'])

    def test_move_world_space_and_reset(self):
        cli, bench = self.module(), self.bench()
        args = cli.parser().parse_args(['pose', 'move', '--translation', '.25', '0', '0'])
        with self.assertRaisesRegex(ValueError, 'set'):
            cli.dispatch(args, bench)
        cli.dispatch(cli.parser().parse_args(['pose', 'set', '--position', '0', '1.6', '0']), bench)
        result = cli.dispatch(args, bench)
        self.assertEqual(result['pose']['position'], [.25, 0, 0])
        cli.dispatch(cli.parser().parse_args(['pose', 'reset']), bench)
        self.assertEqual(bench.commands[-1], ('control', 'pose-release'))

    def test_invalid_pose_never_calls_backend(self):
        cli, bench = self.module(), self.bench()
        args = cli.parser().parse_args(['pose', 'set', '--position', 'nan', '0', '0'])
        with self.assertRaises(ValueError):
            cli.dispatch(args, bench)
        self.assertEqual(bench.commands, [])

    def test_capture_native_owns_directory_creation(self):
        cli = self.module()
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'fresh'
            bench = cli.Bench()
            def native(*args):
                if args[0] == 'status':
                    return {'compositor': {'frame_index': 1}, 'hmd': {'poses': {}}}
                self.assertEqual(args[0], 'capture')
                directory = Path(args[1])
                self.assertFalse(directory.exists())
                directory.mkdir()
                for name in ('preview.png', 'stereo.png'):
                    (directory / name).write_bytes(b'fixture')
                return {'ok': True}
            with patch.object(bench, 'native', side_effect=native):
                result = bench.capture(str(out))
            self.assertEqual(result['files']['stereo.png'], str(out / 'stereo.png'))
            self.assertIn('runtime_before', result)
            self.assertIn('runtime_after', result)

    def test_remote_capture_fetch_writes_local_artifacts(self):
        cli = self.module()
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'capture'
            reply = {'ok': True, 'output': '/remote/artifacts/capture-123',
                     'files': {'stereo.png': '/remote/artifacts/capture-123/stereo.png',
                               'preview.png': '/remote/artifacts/capture-123/preview.png'}}
            calls = []
            def copy(argv, **kwargs):
                calls.append(argv)
                Path(argv[-1]).write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            with patch.object(cli.subprocess, 'run', side_effect=copy):
                result = cli.fetch_capture('frame', reply, out)
            self.assertEqual(len(calls), 2)
            self.assertEqual(Path(result['local_files']['stereo.png']).parent, out)
            self.assertTrue((out / 'result.json').is_file())
            with self.assertRaises(FileExistsError):
                cli.fetch_capture('frame', reply, out)

    def test_status_without_proxy_still_observes(self):
        cli, bench = self.module(), self.bench()
        def refused(text):
            raise FileNotFoundError('not installed')
        bench.control = refused
        result = cli.dispatch(cli.parser().parse_args(['status']), bench)
        self.assertIn('raw_to_standing', result['runtime']['transforms'])
        self.assertFalse(result['input']['available'])


if __name__ == '__main__':
    unittest.main()
