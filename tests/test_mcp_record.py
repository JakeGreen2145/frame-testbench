"""Recording MCP contracts using synthetic MP4 headers, not headset/video evidence."""
import asyncio
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

SDK_AVAILABLE = importlib.util.find_spec('mcp') is not None
# Deliberately not a playable video. Only the container signature is under test.
MP4_HEADER = b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42'


def recording_fixture(argv):
    """Synthetic CLI boundary; no observer, encoder, SSH or hardware involved."""
    directory = Path(argv[-1])
    directory.mkdir()
    files = {}
    for name, content in [('recording.mp4', MP4_HEADER), ('timeline.json', b'{"frames": []}')]:
        path = directory / name
        path.write_bytes(content)
        files[name] = str(path)
    data = {'ok': True, 'command': 'record', 'files': files, 'recording': {'synthetic_fixture': True}}
    if '--fetch' in argv:
        data['local_files'] = files
        data['files'] = {name: '/remote/' + name for name in files}
    return data


@unittest.skipUnless(SDK_AVAILABLE, 'install .[mcp] for MCP tests')
class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from frame_testbench import mcp_server
        self.module = mcp_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def server(self, runner=recording_fixture, host=None):
        return self.module.create_server(self.module.Config(
            host=host, remote_root='dev/bench space; literal', artifacts_dir=self.root), runner=runner)

    async def test_schema_has_strict_bounded_integers_and_view_enum(self):
        tools = {t.name: t for t in await self.server().list_tools()}
        self.assertIn('record', tools)
        schema = tools['record'].inputSchema
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['properties']), {'duration_seconds', 'fps', 'view'})
        for name, upper, default in [('duration_seconds', 60, 10), ('fps', 10, 5)]:
            field = schema['properties'][name]
            self.assertEqual((field['type'], field['minimum'], field['maximum'], field['default']),
                             ('integer', 1, upper, default))
        self.assertEqual(schema['properties']['view']['enum'], ['preview', 'stereo'])
        self.assertEqual(schema['properties']['view']['default'], 'stereo')

    async def test_invalid_arguments_never_start_runner(self):
        calls = []
        server = self.server(lambda argv: calls.append(argv))
        cases = [{'view': value} for value in ['both', None, 1, True]]
        cases += [{'host': 'other'}, {'output': '/etc'}, {'extra': 1}]
        for name, upper in [('duration_seconds', 60), ('fps', 10)]:
            cases += [{name: value} for value in [0, -1, upper + 1, True, False, 1.0, 1.5, '1', None,
                                                 float('nan'), float('inf')]]
        for args in cases:
            with self.subTest(args=args):
                reply = await server.call_tool('record', args)
                self.assertTrue(reply.isError, reply)
                self.assertEqual(calls, [])
                self.assertNotIn('unknown tool', reply.content[0].text)

    async def test_local_remote_argv_owned_fresh_paths_metadata_and_links(self):
        directories = []
        for host in [None, 'frame']:
            for args, values in [({}, ['10', '5', 'stereo']),
                                 ({'duration_seconds': 1, 'fps': 1, 'view': 'preview'}, ['1', '1', 'preview']),
                                 ({'duration_seconds': 60, 'fps': 10}, ['60', '10', 'stereo'])]:
                def run(argv, host=host, values=values):
                    prefix = ['--host', host, '--remote-root', 'dev/bench space; literal'] if host else []
                    self.assertEqual(argv[:-1], prefix + ['record', '--duration', values[0], '--fps', values[1],
                                                         '--view', values[2], '--fetch' if host else '--output'])
                    directory = Path(argv[-1])
                    self.assertTrue(directory.is_relative_to(self.root))
                    self.assertFalse(directory.exists())
                    self.assertEqual(directory.parent.stat().st_mode & 0o777, 0o700)
                    directories.append(directory)
                    return recording_fixture(argv)
                reply = await self.server(run, host).call_tool('record', args)
                self.assertFalse(reply.isError, reply)
                self.assertEqual(json.loads(reply.content[0].text), reply.structuredContent)
                self.assertTrue(reply.structuredContent['recording']['synthetic_fixture'])
                links = {c.name: c for c in reply.content if c.type == 'resource_link'}
                self.assertEqual(set(links), {'recording.mp4', 'timeline.json'})
                for name, mime in [('recording.mp4', 'video/mp4'), ('timeline.json', 'application/json')]:
                    self.assertEqual(str(links[name].uri), (directories[-1] / name).as_uri())
                    self.assertEqual(links[name].mimeType, mime)
                self.assertFalse(any(c.type in ('image', 'resource') for c in reply.content))
        self.assertEqual(len(set(directories)), len(directories))

    async def test_backend_failures_do_not_expose_links(self):
        failures = [{'ok': False, 'error': 'encoder refused'}, {}, [], {'ok': 'true'},
                    RuntimeError('encoder died'), OSError('missing encoder')]
        for failure in failures:
            with self.subTest(failure=failure):
                def run(argv, failure=failure):
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                reply = await self.server(run).call_tool('record', {})
                self.assertTrue(reply.isError, reply)
                self.assertNotIn('unknown tool', reply.content[0].text)
                self.assertFalse(any(c.type == 'resource_link' for c in reply.content))

    async def test_artifact_refusals(self):
        for host in [None, 'frame']:
            for name in ['recording.mp4', 'timeline.json']:
                for failure in ['missing', 'outside', 'relative', 'symlink', 'directory', 'fifo',
                                'corrupt', 'oversize', 'directory-symlink', 'parent-symlink', 'bad-files']:
                    with self.subTest(host=host, name=name, failure=failure):
                        def run(argv, host=host, name=name, failure=failure):
                            data = recording_fixture(argv)
                            directory = Path(argv[-1])
                            path = directory / name
                            files = data['local_files' if host else 'files']
                            if failure == 'missing':
                                path.unlink()
                            elif failure == 'outside':
                                files[name] = '/etc/passwd'
                            elif failure == 'relative':
                                files[name] = name
                            elif failure == 'symlink':
                                target = path.with_suffix('.target')
                                path.rename(target)
                                path.symlink_to(target)
                            elif failure in ('directory', 'fifo'):
                                path.unlink()
                                path.mkdir() if failure == 'directory' else os.mkfifo(path)
                            elif failure == 'corrupt':
                                path.write_bytes(b'not a valid artifact')
                            elif failure == 'oversize':
                                with path.open('r+b') as stream:
                                    stream.truncate((512 if name.endswith('.mp4') else 8) * 1024 * 1024 + 1)
                            elif failure in ('directory-symlink', 'parent-symlink'):
                                original = directory if failure == 'directory-symlink' else directory.parent
                                target = original.with_name(original.name + '-moved')
                                original.rename(target)
                                original.symlink_to(target, target_is_directory=True)
                            elif failure == 'bad-files':
                                data['local_files' if host else 'files'] = []
                            return data
                        reply = await self.server(run, host).call_tool('record', {})
                        self.assertTrue(reply.isError, reply)
                        self.assertNotIn('unknown tool', reply.content[0].text)
                        self.assertFalse(any(c.type == 'resource_link' for c in reply.content))

    async def test_video_validation_reads_only_a_bounded_header(self):
        paths = []
        def run(argv):
            data = recording_fixture(argv)
            path = Path(argv[-1]) / 'recording.mp4'
            with path.open('r+b') as stream:
                stream.truncate(512 * 1024 * 1024)
            paths.append(path)
            return data
        # Count kernel read characters in this process, including worker threads.
        # A full 512 MiB video read cannot hide behind a different Python I/O API.
        def read_chars():
            return int(Path('/proc/self/io').read_text().split('rchar: ')[1].splitlines()[0])
        before = read_chars()
        reply = await self.server(run).call_tool('record', {})
        self.assertFalse(reply.isError, reply)
        self.assertLess(read_chars() - before, 16 * 1024 * 1024)
        self.assertEqual(paths[0].stat().st_size, 512 * 1024 * 1024)

    async def test_pose_and_input_tools_run_during_recording(self):
        entered, finish = threading.Event(), threading.Event()
        def run(argv):
            if argv[0] == 'record':
                entered.set()
                if not finish.wait(5):
                    raise RuntimeError('test timed out waiting for concurrent controls')
                return recording_fixture(argv)
            return {'ok': True, 'argv': argv}
        server = self.server(run)
        self.assertIn('record', {tool.name for tool in await server.list_tools()})
        first = asyncio.create_task(server.call_tool('record', {}))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
                for name, args in [('hmd_pose_move', {'translation': [0.1, 0, 0]}),
                                   ('controller_trigger', {'side': 'left', 'value': 0.5})]:
                    reply = await server.call_tool(name, args)
                    self.assertFalse(reply.isError, reply)
            self.assertFalse(first.done())
        finally:
            finish.set()
            reply = await first
        self.assertFalse(reply.isError, reply)

    async def test_cancellation_retains_worker_and_recording_lock(self):
        import anyio
        entered, finish = threading.Event(), threading.Event()
        events, replies = [], []
        scope = anyio.CancelScope()
        def run(argv):
            events.append('start')
            entered.set()
            if not finish.wait(5):
                raise RuntimeError('test recording did not finish')
            data = recording_fixture(argv)
            events.append('end')
            return data
        server = self.server(run)
        self.assertIn('record', {tool.name for tool in await server.list_tools()})
        async def first():
            with scope:
                await server.call_tool('record', {})
        async def second():
            replies.append(await server.call_tool('record', {}))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(first)
            try:
                with anyio.fail_after(2):
                    while not entered.is_set():
                        await anyio.sleep(0.01)
                scope.cancel()
                tasks.start_soon(second)
                await anyio.sleep(0.05)
                self.assertEqual(events, ['start'])
            finally:
                finish.set()
        self.assertEqual(events, ['start', 'end', 'start', 'end'])
        self.assertFalse(replies[0].isError, replies[0])


if __name__ == '__main__':
    unittest.main()
