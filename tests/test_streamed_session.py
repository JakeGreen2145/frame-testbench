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
from frame_testbench.streamed_session import Observation, Orchestrator

RUN = '11223344-5566-7788-99aa-bbccddeeff00'


def values(**changes):
    result = dict(schema_version='1', run_id=RUN, process_id='321', process_start_utc_ticks='123456',
                  executable='C:\\Game path\\game.exe', state='active', process_alive='true',
                  engine_ready='true', runtime_ready='true', rendering_active='true',
                  injector_status='fresh', stream_ready='false', stream_status='unavailable')
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


class Host:
    def __init__(self):
        self.calls = []
        self.status_value = HostStatus.parse(values())
        self.stop_error = False

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
        return HostStatus.parse(values(state='stopped', process_alive='false'))


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
