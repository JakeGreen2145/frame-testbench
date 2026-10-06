"""MCP contract tests. Install .[mcp] to run; no SteamVR or SSH needed."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SDK_AVAILABLE = importlib.util.find_spec('mcp') is not None


@unittest.skipUnless(SDK_AVAILABLE, 'install .[mcp] for MCP tests')
class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from frame_testbench import mcp_server
        except ImportError:
            self.fail('MCP server module missing')
        self.module = mcp_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

        def run(argv):
            self.calls.append(argv)
            return {'ok': True, 'argv': argv}

        self.server = mcp_server.create_server(
            mcp_server.Config(artifacts_dir=Path(self.tmp.name)), runner=run)

    async def test_tool_inventory_and_no_transport_or_arbitrary_command_arguments(self):
        tools = await self.server.list_tools()
        self.assertEqual({t.name for t in tools}, {
            'status', 'worn', 'hmd_pose_set', 'hmd_pose_move', 'hmd_pose_reset',
            'controller_pose_set', 'controller_pose_move', 'controller_pose_reset',
            'compositor', 'capture', 'release_all'})
        for tool in tools:
            self.assertFalse(tool.inputSchema.get('additionalProperties', True))
            self.assertFalse({'host', 'socket', 'command', 'output', 'remote_root'} &
                             tool.inputSchema.get('properties', {}).keys())

    async def test_invalid_arguments_never_reach_backend(self):
        cases = [
            ('worn', {'state': 'yes'}), ('compositor', {'mode': 'off'}),
            ('controller_pose_reset', {'side': 'both'}),
            ('hmd_pose_set', {}), ('hmd_pose_set', {'position': [1, 2]}),
            ('hmd_pose_set', {'position': [1, 2, 3, 4]}),
            ('hmd_pose_set', {'position': '[1, 2, 3]'}),
            ('hmd_pose_set', {'position': [1, True, 3]}),
            ('hmd_pose_set', {'position': [1, '2', 3]}),
            ('hmd_pose_move', {'space': 'seated'}),
            ('status', {'host': 'elsewhere'}), ('release_all', {'socket': '/arbitrary'}),
            ('install', {}), ('uninstall', {}), ('arbitrary', {'command': 'id'}),
        ]
        for name in ('hmd_pose_set', 'controller_pose_set', 'hmd_pose_move', 'controller_pose_move'):
            for value in (float('nan'), float('inf'), -float('inf')):
                arguments = {'position': [0, 0, 0]} if name.endswith('_set') else {}
                if name.startswith('controller'):
                    arguments['side'] = 'left'
                cases.append((name, dict(arguments, rotation=[value, 0, 0])))
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                reply = await self.server.call_tool(name, arguments)
                self.assertTrue(reply.isError, reply)
                self.assertEqual(self.calls, [])

    async def test_backend_failure_is_mcp_error(self):
        for failure in ({'ok': False, 'error': 'proxy refused'}, ['not an object'],
                        {'error': 'missing success flag'}, {'ok': 'false'},
                        RuntimeError('backend died'), OSError('not installed')):
            def run(argv, failure=failure):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            server = self.module.create_server(self.module.Config(), runner=run)
            reply = await server.call_tool('worn', {'state': 'off'})
            self.assertTrue(reply.isError, reply)
            self.assertTrue(reply.content[0].text)

    async def test_remote_configuration_is_fixed_and_forwarded_as_literal_argv(self):
        server = self.module.create_server(self.module.Config(
            host='frame', remote_root='dev/bench space; literal'), runner=lambda argv: {'ok': True, 'argv': argv})
        reply = await server.call_tool('worn', {'state': 'on'})
        self.assertEqual(reply.structuredContent['argv'],
                         ['--host', 'frame', '--remote-root', 'dev/bench space; literal', 'worn', 'on'])
        with self.assertRaises(ValueError):
            self.module.create_server(self.module.Config(host='-oProxyCommand=bad'), runner=lambda _: {})

    async def test_operations_are_serialized_without_blocking_event_loop(self):
        import asyncio
        import threading
        import time
        entered = threading.Event()
        finish = threading.Event()
        events = []
        def run(argv):
            events.append(('start', argv[0]))
            if argv[0] == 'worn':
                entered.set()
                finish.wait(1)
            events.append(('end', argv[0]))
            return {'ok': True}
        server = self.module.create_server(self.module.Config(), runner=run)
        first = asyncio.create_task(server.call_tool('worn', {'state': 'on'}))
        start = time.monotonic()
        while not entered.is_set() and time.monotonic() - start < 2:
            await asyncio.sleep(0.01)
        try:
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - start, 0.7, 'backend blocked the event loop')
            second = asyncio.create_task(server.call_tool('hmd_pose_move', {}))
            await asyncio.sleep(0.05)
            self.assertEqual(events, [('start', 'worn')])
        finally:
            finish.set()
            await first
        await second
        self.assertEqual(events, [('start', 'worn'), ('end', 'worn'), ('start', 'pose'), ('end', 'pose')])

    async def test_mcp_cancellation_does_not_release_lock_while_backend_runs(self):
        import threading

        import anyio
        entered, finish = threading.Event(), threading.Event()
        events = []
        first_scope = anyio.CancelScope()
        def run(argv):
            events.append(('start', argv[0]))
            if argv[0] == 'worn':
                entered.set()
                finish.wait(3)
            events.append(('end', argv[0]))
            return {'ok': True}
        server = self.module.create_server(self.module.Config(), runner=run)
        async def first():
            with first_scope:
                await server.call_tool('worn', {'state': 'on'})
        async def second():
            await server.call_tool('release_all', {})
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(first)
            with anyio.fail_after(2):
                while not entered.is_set():
                    await anyio.sleep(0.01)
            first_scope.cancel()
            tasks.start_soon(second)
            try:
                await anyio.sleep(0.05)
                self.assertEqual(events, [('start', 'worn')])
            finally:
                finish.set()
        self.assertEqual(events, [('start', 'worn'), ('end', 'worn'), ('start', 'release'), ('end', 'release')])

    async def test_capture_returns_png_blocks_and_metadata_in_unique_owned_directories(self):
        import base64

        from test_mcp_stdio import PNG
        directories = []
        for host in (None, 'frame'):
            def run(argv, host=host):
                flag = '--fetch' if host else '--output'
                self.assertIn(flag, argv)
                self.assertNotIn('--output' if host else '--fetch', argv)
                directory = Path(argv[argv.index(flag) + 1])
                self.assertFalse(directory.exists())
                self.assertTrue(directory.is_relative_to(Path(self.tmp.name)))
                directory.mkdir()
                directories.append(directory)
                files = {}
                for name in ('preview.png', 'stereo.png'):
                    (directory / name).write_bytes(PNG)
                    files[name] = str(directory / name)
                return {'ok': True, 'frame_index': 42, 'local_files' if host else 'files': files}
            server = self.module.create_server(self.module.Config(
                host=host, artifacts_dir=Path(self.tmp.name)), runner=run)
            for _ in range(2):
                reply = await server.call_tool('capture', {})
                self.assertFalse(reply.isError, reply)
                self.assertEqual(reply.structuredContent['frame_index'], 42)
                self.assertEqual(json.loads(reply.content[0].text)['frame_index'], 42)
                images = [c for c in reply.content if c.type == 'image']
                self.assertEqual(len(images), 2)
                for image in images:
                    self.assertEqual(image.mimeType, 'image/png')
                    self.assertEqual(base64.b64decode(image.data), PNG)
        self.assertEqual(len(set(directories)), 4)

    async def test_capture_rejects_missing_corrupt_or_outside_artifacts(self):
        from test_mcp_stdio import PNG
        for failure in ('missing', 'corrupt', 'symlink', 'outside'):
            def run(argv, failure=failure):
                directory = Path(argv[-1])
                directory.mkdir()
                files = {}
                for name in ('preview.png', 'stereo.png'):
                    path = directory / name
                    files[name] = str(path)
                    if failure == 'missing':
                        continue
                    if failure == 'symlink':
                        target = Path(self.tmp.name) / ('outside-' + name)
                        target.write_bytes(PNG)
                        path.symlink_to(target)
                    else:
                        path.write_bytes(b'not a PNG' if failure == 'corrupt' else PNG)
                    if failure == 'outside':
                        files[name] = '/etc/passwd'
                return {'ok': True, 'files': files}
            server = self.module.create_server(self.module.Config(
                artifacts_dir=Path(self.tmp.name)), runner=run)
            reply = await server.call_tool('capture', {})
            self.assertTrue(reply.isError, (failure, reply))
            self.assertFalse(any(c.type == 'image' for c in reply.content))

    async def test_small_negative_numbers_survive_argparse(self):
        from frame_testbench import cli
        def parse(argv):
            try:
                args = cli.parser().parse_args(argv)
            except SystemExit:
                self.fail('finite numbers must not be mistaken for CLI options')
            return {'ok': True, 'position': args.position, 'rotation': args.rotation}
        server = self.module.create_server(self.module.Config(), runner=parse)
        reply = await server.call_tool('hmd_pose_set', {
            'position': [-1e-8, -1e20, -5e-324], 'rotation': [-1e-9, 0, 0]})
        self.assertFalse(reply.isError, reply)
        self.assertEqual(reply.structuredContent['position'], [-1e-8, -1e20, -5e-324])
        self.assertEqual(reply.structuredContent['rotation'], [-1e-9, 0, 0])

    async def test_tools_forward_validated_cli_arguments(self):
        cases = [
            ('status', {}, ['status']),
            *[('worn', {'state': s}, ['worn', s]) for s in ('on', 'off', 'physical')],
            *[('compositor', {'mode': s}, ['compositor', s]) for s in ('awake', 'auto')],
            ('hmd_pose_set', {'position': [1, 2, -3]},
             ['pose', 'set', '--position', '1', '2', '-3', '--rotation', '0', '0', '0', '--space', 'standing']),
            ('hmd_pose_move', {'translation': [0.5, 0, 0], 'rotation': [90, 0, -45], 'space': 'raw'},
             ['pose', 'move', '--translation', '0.5', '0', '0', '--rotation', '90', '0', '-45', '--space', 'raw']),
            ('hmd_pose_reset', {}, ['pose', 'reset']),
            ('release_all', {}, ['release']),
        ]
        for side in ('left', 'right'):
            cases.extend([
                ('controller_pose_set', {'side': side, 'position': [1, 2, 3], 'rotation': [4, 5, 6], 'space': 'raw'},
                 ['controller', side, 'set', '--position', '1', '2', '3', '--rotation', '4', '5', '6', '--space', 'raw']),
                ('controller_pose_move', {'side': side, 'translation': [-1, 0, 1]},
                 ['controller', side, 'move', '--translation', '-1', '0', '1', '--rotation', '0', '0', '0', '--space', 'standing']),
                ('controller_pose_reset', {'side': side}, ['controller', side, 'reset']),
            ])
        for name, arguments, expected in cases:
            with self.subTest(name=name, arguments=arguments):
                result = await self.server.call_tool(name, arguments)
                self.assertFalse(result.isError, result)
                self.assertEqual(self.calls[-1], expected)
                self.assertEqual(json.loads(result.content[0].text)['argv'], expected)
                self.assertEqual(result.structuredContent['argv'], expected)


if __name__ == '__main__':
    unittest.main()
