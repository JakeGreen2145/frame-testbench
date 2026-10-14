"""Controlled PC protocol and cross-client tests. No games, SSH or headset required."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from frame_testbench.ugevr import Client, HostStatus, ProtocolError, read_scalars, run_process, write_scalars
from frame_testbench.streamed_session import Observation, Orchestrator, RunResult

RUN = '11223344-5566-7788-99aa-bbccddeeff00'


def values(**changes):
    result = dict(schema_version='1', run_id=RUN, process_id='321', process_start_utc_ticks='123456',
                  executable='C:\\Game path\\game.exe', state='active', process_alive='true',
                  engine_ready='true', runtime_ready='true', rendering_active='true',
                  injector_status='fresh', stream_ready='false', stream_status='unavailable',
                  cleanup_complete='false', deployment_restored='false')
    result.update(changes)
    return result


class ClientTests(unittest.TestCase):
    def test_protocol_subset_and_rejections(self):
        text = write_scalars(values())
        self.assertEqual(read_scalars(text), values())
        self.assertEqual(read_scalars("name: 'It''s here'\n"), {'name': "It's here"})
        for bad in ['x: one\nx: two', ' x: one', 'x: !object', 'x: [one]', 'x: "bad', 'x: ' + 'a' * 65536]:
            with self.subTest(bad=bad[:40]), self.assertRaises(ProtocolError):
                read_scalars(bad)
        for fields in [values(schema_version='2'), values(process_id='0'), values(engine_ready='yes')]:
            with self.assertRaises(ProtocolError):
                HostStatus.parse(fields)

    def test_launch_pending_and_identity_are_preserved_without_shell(self):
        calls, responses = [], [values(state='waiting_for_injector', engine_ready='false'), values()]

        def runner(argv, timeout, cancelled):
            calls.append(argv)
            return 4 if len(calls) == 1 else 0, write_scalars(responses.pop(0)), ''

        client = Client(['dotnet', 'controller path.dll'], 'owned session', runner=runner)
        pending = client.launch('game.exe', prepared=True, game_arguments=['literal $() space'])
        self.assertFalse(pending.ready)
        self.assertTrue(client.status().ready)
        self.assertEqual(calls[0][-2:], ['--game-arg', 'literal $() space'])
        self.assertIn('--session-dir', calls[0])

    def test_failed_deployment_has_typed_session_and_zero_process_identity(self):
        reply = values(state='failed', process_alive='false', process_id='0',
                       process_start_utc_ticks='0', last_error='deployment failed')
        client = Client(['unused'], 'owned', runner=lambda *_: (3, write_scalars(reply), ''))
        status = client.launch('fixture.exe', prepared=True)
        self.assertFalse(status.ready)
        self.assertEqual(status.values['last_error'], 'deployment failed')

    def test_wrong_run_or_reused_pid_is_rejected(self):
        for changed in [dict(run_id='00000000-0000-0000-0000-000000000001'),
                        dict(process_start_utc_ticks='123457'), dict(executable='other.exe')]:
            replies = [values(), values(**changed)]
            client = Client(['unused'], 'owned', runner=lambda *_: (0, write_scalars(replies.pop(0)), ''))
            client.status()
            with self.assertRaises(ProtocolError):
                client.status()

    def test_error_reply_and_bad_exit_do_not_become_ready(self):
        for code, reply in [(2, {'error': 'denied'}), (3, values()), (0, {'schema_version': '2'})]:
            client = Client(['unused'], 'owned', runner=lambda *_: (code, write_scalars(reply), ''))
            with self.assertRaises(ProtocolError):
                client.status()

    def test_real_subprocess_reply_timeout_and_cancellation(self):
        # Exercise the transport with a real controlled executable, not a mocked subprocess.
        code, reply, _ = run_process([sys.executable, '-c', 'print(' + repr(write_scalars(values())) + ')'],
                                     5, threading.Event())
        self.assertEqual(code, 0)
        self.assertTrue(HostStatus.parse(read_scalars(reply)).ready)
        with self.assertRaises(ProtocolError):
            run_process([sys.executable, '-c', 'print("x" * 65537)'], 5, threading.Event())
        with self.assertRaises(TimeoutError):
            run_process([sys.executable, '-c', 'import time; time.sleep(10)'], .15, threading.Event())
        cancelled = threading.Event()
        timer = threading.Timer(.1, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                run_process([sys.executable, '-c', 'import time; time.sleep(10)'], 5, cancelled)
        finally:
            timer.join()

    def test_cli_requires_deployment_options_before_any_transport(self):
        from frame_testbench.stream_cli import main
        with patch('frame_testbench.stream_cli.run_headset_cli') as bench, patch('builtins.print'):
            exit_code = main(['--controller-dll', 'controller.dll', '--session-dir', 'owned',
                              'run', '--exe', 'fixture.exe', '--host', 'frame', '--expected-peer', 'pc'])
        self.assertEqual(exit_code, 2)
        bench.assert_not_called()

    def test_steam_app_id_is_typed_bounded_and_forwarded_without_coercion(self):
        calls = []
        client = Client(['unused'], 'owned', runner=lambda args, *_: (
            calls.append(args) or 0, write_scalars(values()), ''))
        client.launch('fixture.exe', prepared=True, steam_app_id=42)
        self.assertIn('--steam-app-id', calls[-1])
        self.assertEqual(calls[-1][calls[-1].index('--steam-app-id') + 1], '42')
        for invalid in [True, False, 0, -1, 4294967296, '42', 42.0]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                client.launch('fixture.exe', prepared=True, steam_app_id=invalid)
        self.assertEqual(len(calls), 1)
        client.launch('fixture.exe', prepared=True)
        self.assertNotIn('--steam-app-id', calls[-1])

    def test_cli_steam_id_validation_and_forwarding(self):
        from frame_testbench.stream_cli import main, steam_id_argument
        import argparse
        self.assertEqual(steam_id_argument('4294967295'), 4294967295)
        for invalid in ['0', '-1', '+42', '42.0', ' 42', '4294967296', 'true', '٤٢']:
            with self.subTest(invalid=invalid), self.assertRaises(argparse.ArgumentTypeError):
                steam_id_argument(invalid)
        with patch('frame_testbench.stream_cli.Orchestrator') as orchestrator, patch('builtins.print'):
            orchestrator.return_value.run.return_value = RunResult('test')
            code = main(['--controller-dll', 'controller.dll', '--session-dir', 'owned',
                         'run', '--exe', 'fixture.exe', '--prepared', '--host', 'frame',
                         '--expected-peer', 'pc', '--steam-app-id', '42'])
        self.assertEqual(code, 4)
        self.assertEqual(orchestrator.return_value.run.call_args.kwargs['launch_options']['steam_app_id'], 42)

    def test_explicit_replacement_paths_forward_as_literal_repeated_arguments(self):
        selected = ['BepInEx/config/BepInEx.cfg',
                    'BepInEx/plugins/UnityVRInjector/UnityVRInjector.dll',
                    'UnityVRRenderBridge.dll', r'literal\$() file.dll']
        calls = []
        client = Client(['unused'], 'owned', runner=lambda args, *_: (
            calls.append(args) or 0, write_scalars(values()), ''))
        deployment = dict(game_root='game', distribution_root='payload', deployment_script='deploy.ps1')
        client.launch('game/fixture.exe', replacement_paths=selected, **deployment)
        forwarded = [calls[0][i + 1] for i, arg in enumerate(calls[0]) if arg == '--replace-path']
        self.assertEqual(forwarded, selected)
        client.launch('game/fixture.exe', **deployment)
        self.assertNotIn('--replace-path', calls[1])

    def test_dangerous_or_prepared_replacements_reject_before_controller(self):
        calls = []
        client = Client(['unused'], 'owned', runner=lambda *args: calls.append(args))
        deployment = dict(game_root='game', distribution_root='payload', deployment_script='deploy.ps1')
        invalid = ['', ' ', '..', '../outside.dll', r'..\outside.dll', 'sub/../outside.dll',
                   '/outside.dll', r'\outside.dll', r'\\server\share\file.dll',
                   'C:outside.dll', 'C:/outside.dll', 'file.dll:stream', '*', 'sub/*.dll',
                   'sub//file.dll', 'sub/./file.dll', 'sub/file.dll ', 'sub/file.dll.',
                   'sub/line\nfile.dll', 42, True, Path('file.dll')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                client.launch('game/fixture.exe', replacement_paths=[value], **deployment)
        with self.assertRaises(ValueError):
            client.launch('game/fixture.exe', replacement_paths='file.dll', **deployment)
        with self.assertRaises(ValueError):
            client.launch('fixture.exe', prepared=True, replacement_paths=['file.dll'])
        self.assertEqual(calls, [])

    def test_cli_replacement_selection_and_prepared_refusal_before_any_transport(self):
        from frame_testbench.stream_cli import main, replacement_argument
        import argparse
        selected = ['BepInEx/config/BepInEx.cfg', 'UnityVRRenderBridge.dll']
        prefix = ['--controller-dll', 'controller.dll', '--session-dir', 'owned',
                  'run', '--exe', 'game/fixture.exe', '--host', 'frame', '--expected-peer', 'pc']
        with self.assertRaises(argparse.ArgumentTypeError):
            replacement_argument('../outside.dll')
        with patch('frame_testbench.stream_cli.Orchestrator') as orchestrator, patch('builtins.print'):
            orchestrator.return_value.run.return_value = RunResult('test')
            code = main(prefix + ['--game-root', 'game', '--distribution-root', 'payload',
                                  '--deployment-script', 'deploy.ps1', '--replace-path', selected[0],
                                  '--replace-path', selected[1]])
        self.assertEqual(code, 4)
        self.assertEqual(orchestrator.return_value.run.call_args.kwargs['launch_options']['replacement_paths'], selected)
        with patch('frame_testbench.stream_cli.Orchestrator') as orchestrator, \
                patch('frame_testbench.stream_cli.run_headset_cli') as bench, \
                patch('frame_testbench.ugevr.run_process') as controller, patch('builtins.print'):
            code = main(prefix + ['--prepared', '--replace-path', selected[0], '--experimental-steam-cef'])
        self.assertEqual(code, 2)
        orchestrator.assert_not_called()
        bench.assert_not_called()
        controller.assert_not_called()

    def test_standalone_stop_exit_code_requires_restoration_and_terminal_cleanup(self):
        from frame_testbench.stream_cli import main
        cases = [({'deployment_restored': 'true', 'cleanup_complete': 'true'}, 0),
                 ({'deployment_restored': 'not_applicable', 'cleanup_complete': 'true'}, 0),
                 ({'deployment_restored': 'false', 'cleanup_complete': 'true'}, 4),
                 ({'deployment_restored': 'true', 'cleanup_complete': 'false'}, 4),
                 ({'state': 'failed', 'last_error': 'restore failed'}, 3)]
        for fields, expected in cases:
            with self.subTest(fields=fields), patch('frame_testbench.stream_cli.Client') as client, patch('builtins.print'):
                status_fields = dict(state='stopped', process_alive='false')
                status_fields.update(fields)
                client.return_value.stop.return_value = HostStatus.parse(values(**status_fields))
                self.assertEqual(main(['--controller-dll', 'controller.dll', '--session-dir', 'owned', 'stop']), expected)


class Host:
    def __init__(self):
        self.calls = []
        self.status_value = HostStatus.parse(values())
        self.stop_error = False
        self.stop_fields = dict(state='stopped', process_alive='false', cleanup_complete='true',
                                deployment_restored='true')

    def launch(self, executable, **options):
        self.calls.append('launch')
        return self.status_value

    def status(self, cancelled=None):
        self.calls.append('status')
        return self.status_value

    def stop(self):
        self.calls.append('stop')
        if self.stop_error:
            raise RuntimeError('controller unreachable')
        return HostStatus.parse(values(**self.stop_fields))


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 100.0
        self.host = Host()
        self.calls = []
        self.active = False
        self.fail_record = False
        self.fail_cleanup = False
        self.cancelled = threading.Event()
        self.cancel_record = False

    def bench(self, args):
        self.calls.append(args)
        if args[:2] == ['session', 'status']:
            return {'ok': True, 'active': self.active}
        if args[:2] == ['session', 'begin']:
            self.active = True
            return {'ok': True, 'active': True}
        if args[:2] == ['session', 'renew']:
            return {'ok': True, 'active': self.active}
        if args[:2] == ['session', 'end']:
            if self.fail_cleanup:
                raise RuntimeError('headset unavailable')
            self.active = False
        if args[0] == 'record':
            if self.fail_record:
                raise RuntimeError('capture failed')
            self.now += int(args[args.index('--duration') + 1])
            if self.cancel_record:
                self.cancelled.set()
        return {'ok': True}

    def run_test(self, **options):
        orchestrator = Orchestrator(self.host, self.bench, self.tmp.name, 'expected-pc',
                                    clock=lambda: self.now, **options)
        return orchestrator.run('fixture.exe', cancelled=self.cancelled, launch_options={'prepared': True})

    def test_three_minutes_composes_bounded_video_and_stereo_without_fake_pass(self):
        result = self.run_test()
        recordings = [args for args in self.calls if args[0] == 'record']
        self.assertEqual(len(recordings), 6)
        self.assertEqual(sum(int(args[2]) for args in recordings), 180)
        self.assertEqual(result.recorded_seconds, 180)
        self.assertEqual(len([args for args in self.calls if args[0] == 'capture']), 6)
        self.assertEqual(result.outcome, 'incomplete')
        self.assertFalse(result.association_verified)
        self.assertEqual(result.peer_evidence, 'unknown')
        self.assertEqual(result.stream_request, 'unavailable')
        self.assertEqual(result.stream_cleanup, 'unavailable')
        self.assertEqual((result.host_cleanup, result.headset_cleanup), ('observed', 'observed'))
        metadata = Path(self.tmp.name, result.test_id, 'session.yaml')
        self.assertEqual(read_scalars(metadata.read_text())['association_verified'], 'false')

    def test_request_alone_proves_no_stream_and_unattended_mode(self):
        result = self.run_test(stream_request=lambda *_: 'requested')
        self.assertEqual(result.stream_request, 'requested')
        self.assertEqual(result.mode, 'assisted')
        self.assertFalse(result.association_verified)

    def test_independent_current_peer_both_eyes_and_receipt_can_associate(self):
        result = self.run_test(observe=lambda status, test_id: Observation(
            status.run_id, 'expected-pc', self.now, status.run_id, status.run_id,
            test_id, 'controlled-observer://fixture'))
        self.assertEqual(result.outcome, 'association_observed')
        self.assertTrue(result.association_verified)
        # Association is narrower than physical gameplay completion.
        self.assertNotIn('functional_pass', result.metadata())

    def test_wrong_peer_wrong_run_stale_eye_or_receipt_reject_and_cleanup(self):
        cases = [dict(peer='different-pc'), dict(run_id='different-run'),
                 dict(observed_at=0), dict(marker_right='different-run'),
                 dict(input_receipt='different-test')]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.now = 100
                def observe(status, test_id):
                    data = dict(run_id=status.run_id, peer='expected-pc', observed_at=self.now,
                                marker_left=status.run_id, marker_right=status.run_id,
                                input_receipt=test_id, evidence_reference='controlled://fixture')
                    data.update(overrides)
                    return Observation(**data)
                result = self.run_test(observe=observe)
                self.assertEqual(result.outcome, 'failed')
                self.assertEqual(result.host_cleanup, 'observed')
                self.assertEqual(result.headset_cleanup, 'observed')

    def test_record_failure_and_cancellation_attempt_both_cleanups(self):
        self.fail_record = True
        result = self.run_test()
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(self.host.calls[-1], 'stop')
        self.assertFalse(self.active)
        self.fail_record, self.cancel_record = False, True
        result = self.run_test()
        self.assertEqual(result.outcome, 'cancelled')
        self.assertEqual(self.host.calls[-1], 'stop')
        self.assertFalse(self.active)

    def test_cleanup_failures_remain_visible_and_are_independent(self):
        self.fail_record = self.fail_cleanup = self.host.stop_error = True
        result = self.run_test()
        self.assertEqual((result.headset_cleanup, result.host_cleanup), ('failed', 'failed'))
        self.assertIn('headset cleanup', result.error)
        self.assertIn('host cleanup', result.error)
        self.assertEqual(self.host.calls[-1], 'stop')

    def test_stopped_process_does_not_imply_deployment_restoration(self):
        for restored, complete, expected in [('true', 'true', 'observed'),
                                              ('not_applicable', 'true', 'observed'),
                                              ('false', 'true', 'pending'),
                                              ('pending', 'true', 'pending'),
                                              ('true', 'false', 'pending')]:
            with self.subTest(restored=restored, complete=complete):
                self.now = 100
                self.host.stop_fields.update(deployment_restored=restored, cleanup_complete=complete)
                result = self.run_test()
                self.assertEqual(result.host_cleanup, expected)
                self.assertEqual(result.host_cleanup_complete, complete == 'true')
                self.assertEqual(result.host_deployment_restored, restored)
                if expected == 'pending':
                    self.assertIn('cleanup is pending', result.host_cleanup_reason)

    def test_failed_host_cleanup_reports_reason_even_when_process_has_exited(self):
        self.host.stop_fields.update(state='failed', deployment_restored='false',
                                     last_error='restore failed: file was changed')
        result = self.run_test()
        self.assertEqual(result.host_cleanup, 'failed')
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(result.host_cleanup_reason, 'restore failed: file was changed')
        self.assertIn(result.host_cleanup_reason, result.error)
        self.assertEqual(result.headset_cleanup, 'observed')

    def test_existing_shared_lease_is_not_ended_or_used(self):
        self.active = True
        result = self.run_test()
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(self.calls, [['session', 'status']])
        self.assertEqual(self.host.calls, [])

    def test_cancelled_before_start_contacts_neither_host_nor_headset(self):
        self.cancelled.set()
        result = self.run_test()
        self.assertEqual(result.outcome, 'cancelled')
        self.assertEqual((self.calls, self.host.calls), ([], []))

    def test_bad_duration_and_chunk_fail_before_transport(self):
        orchestrator = Orchestrator(self.host, self.bench, self.tmp.name, 'pc')
        for options in [dict(duration_seconds=179), dict(duration_seconds=301),
                        dict(chunk_seconds=61), dict(chunk_seconds=True)]:
            with self.assertRaises(ValueError):
                orchestrator.run('fixture.exe', **options)
        self.assertEqual((self.calls, self.host.calls), ([], []))

    def test_renewal_failure_cancels_work_and_attempts_cleanup(self):
        original = self.bench
        def bench(args):
            if args[:2] == ['session', 'renew']:
                return {'ok': True, 'active': False}
            if args[0] == 'record':
                time.sleep(.06)
            return original(args)
        orchestrator = Orchestrator(self.host, bench, self.tmp.name, 'pc', clock=lambda: self.now)
        orchestrator.heartbeat_seconds = .01
        result = orchestrator.run('fixture.exe', launch_options={'prepared': True})
        self.assertEqual(result.outcome, 'failed')
        self.assertIn('lease renewal failed', result.error)
        self.assertEqual((result.host_cleanup, result.headset_cleanup), ('observed', 'observed'))

    def test_experimental_provider_connects_before_game_and_preserves_independent_evidence(self):
        from frame_testbench.steam_cef import StreamState
        parent = self
        class Provider:
            mode = 'experimental_steam_cef'
            last_request = 'requested'
            owns_stream = True
            calls = []
            def connect(self, cancelled):
                parent.assertTrue(parent.active)
                parent.assertEqual(parent.host.calls, [])
                self.calls.append('connect')
                return StreamState('42', True, True, False, True, False, None)
            def hide_dashboard(self, cancelled):
                parent.assertIn('launch', parent.host.calls)
                self.calls.append('hide')
                return StreamState('42', True, True, False, True, False, False)
            def observe(self, cancelled):
                self.calls.append('observe')
                return StreamState('42', True, True, False, False, False, None)
            def disconnect(self):
                self.calls.append('disconnect_unavailable')
                return 'unavailable'
        provider = Provider()
        result = self.run_test(stream_provider=provider, hide_dashboard=True)
        self.assertEqual(provider.calls[:2], ['connect', 'hide'])
        self.assertEqual(result.mode, 'experimental_steam_cef')
        self.assertEqual(result.stream_association, 'observed')
        self.assertEqual(result.stream_client_id, '42')
        self.assertEqual(result.dashboard_visibility, 'hidden')
        self.assertEqual(result.stream_cleanup, 'unavailable')
        self.assertEqual(result.outcome, 'incomplete')
        self.assertFalse(result.association_verified)
        self.assertEqual((result.both_eye_evidence, result.input_forwarding_evidence), ('unknown', 'unknown'))
        metadata = read_scalars(result.metadata())
        self.assertEqual(metadata['stream_association'], 'observed')
        self.assertEqual(metadata['peer_evidence'], 'unknown')
        self.assertEqual(metadata['both_eye_evidence'], 'unknown')
        self.assertEqual(metadata['input_forwarding_evidence'], 'unknown')
        self.assertEqual(metadata['association_verified'], 'false')

    def test_stream_request_without_association_does_not_launch_game(self):
        from frame_testbench.steam_cef import StreamState
        class Provider:
            mode = 'experimental_steam_cef'
            last_request = 'requested'
            owns_stream = False
            def connect(self, cancelled):
                return StreamState('42', True, False, False, True, True, None)
        result = self.run_test(stream_provider=Provider())
        self.assertEqual(self.host.calls, [])
        self.assertEqual(result.stream_request, 'requested')
        self.assertEqual(result.stream_association, 'unknown')
        self.assertEqual(result.host_cleanup, 'not_started')
        self.assertEqual(result.headset_cleanup, 'observed')

    def test_reused_stream_is_not_disconnected_and_loss_is_reported(self):
        from frame_testbench.steam_cef import StreamState
        class Provider:
            mode = 'experimental_steam_cef'
            last_request = 'reused'
            owns_stream = False
            def connect(self, cancelled):
                return StreamState('42', True, True, False, False, False, None)
            def observe(self, cancelled):
                return StreamState('42', True, False, False, False, False, None)
            def disconnect(self):
                raise AssertionError('reused stream must not be disconnected')
        result = self.run_test(stream_provider=Provider())
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(result.stream_association, 'unknown')
        self.assertIn('association was lost', result.error)
        self.assertEqual(result.host_cleanup, 'observed')

    def test_dashboard_dismissal_requires_explicit_provider(self):
        with self.assertRaises(ValueError):
            self.run_test(hide_dashboard=True)

    def test_heartbeat_renews_during_long_running_work_and_stops_before_cleanup(self):
        renewals = []
        original = self.bench
        def bench(args):
            if args[:2] == ['session', 'renew']:
                renewals.append(time.monotonic())
            if args[0] == 'record':
                time.sleep(.06)
            return original(args)
        orchestrator = Orchestrator(self.host, bench, self.tmp.name, 'expected-pc',
                                    clock=lambda: self.now)
        # Compress the renewal interval only inside this controlled test.
        orchestrator.heartbeat_seconds = .01
        result = orchestrator.run('fixture.exe', launch_options={'prepared': True})
        self.assertEqual(result.headset_cleanup, 'observed')
        self.assertGreater(len(renewals), 3)
        count = len(renewals)
        time.sleep(.04)
        self.assertEqual(len(renewals), count)


if __name__ == '__main__':
    unittest.main()
