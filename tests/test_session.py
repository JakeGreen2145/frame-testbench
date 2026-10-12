import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from frame_testbench.session import Session


class FakeBench:
    socket = '/fixture/socket'

    def __init__(self):
        self.overridden = False
        self.auto = True
        self.fail_release = False
        self.calls = []

    def control(self, command):
        self.calls.append(command)
        if command == 'release':
            if self.fail_release:
                raise RuntimeError('proxy unavailable')
            self.overridden = False
        return {'ok': True, 'worn_override': True if self.overridden else None,
                'pose_override': self.overridden, 'controllers': {}}

    def native(self, *args):
        self.calls.append(args)
        if args == ('status',):
            return {'ok': True, 'settings': {'pauseCompositorOnStandby': self.auto}}
        self.auto = args == ('setting', 'true')
        return {'ok': True}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bench = FakeBench()
        self.now = 100
        self.session = Session(self.bench, self.tmp.name, clock=lambda: self.now)
        self.guard = patch.object(self.session, 'require_guard')
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_expiry_releases_proximity_pose_and_power(self):
        self.session.renew(create=True)
        self.bench.overridden, self.bench.auto = True, False
        self.now = 219
        self.assertFalse(self.session.sweep(check_power=True))
        self.assertTrue(self.bench.overridden)
        self.assertFalse(self.bench.auto)
        self.now = 220
        self.assertTrue(self.session.sweep())
        self.assertFalse(self.bench.overridden)
        self.assertTrue(self.bench.auto)
        self.assertFalse(self.session.status()['active'])

    def test_renew_extends_only_active_sessions(self):
        self.session.renew()
        self.assertFalse(self.session.path.exists())
        self.session.renew(10, create=True)
        self.now += 8
        self.session.renew()
        self.now += 8
        self.assertTrue(self.session.status()['active'])
        self.now += 2
        self.assertFalse(self.session.status()['active'])
        self.session.renew()
        self.assertFalse(self.session.status()['active'])

    def test_unleased_native_override_is_recovered(self):
        self.bench.overridden, self.bench.auto = True, False
        self.session.sweep()
        self.assertFalse(self.bench.overridden)
        self.assertTrue(self.bench.auto)

    def test_idle_watchdog_reads_without_rewriting_normal_state(self):
        self.assertFalse(self.session.sweep(check_power=True))
        self.assertEqual(self.bench.calls, ['status', ('status',)])

    def test_unleased_power_override_is_recovered(self):
        self.bench.auto = False
        self.session.sweep(check_power=True)
        self.assertTrue(self.bench.auto)

    def test_failed_proxy_cleanup_still_restores_power_and_retries(self):
        self.session.renew(1, create=True)
        self.bench.overridden, self.bench.auto = True, False
        self.bench.fail_release = True
        self.now += 1
        with self.assertRaisesRegex(RuntimeError, 'proxy unavailable'):
            self.session.sweep()
        self.assertTrue(self.bench.auto)
        self.assertFalse(self.session.path.exists())
        self.bench.fail_release = False
        self.session.sweep()
        self.assertFalse(self.bench.overridden)

    def test_guard_failure_prevents_new_lease(self):
        self.session.require_guard.side_effect = RuntimeError('guard unavailable')
        with self.assertRaisesRegex(RuntimeError, 'guard unavailable'):
            self.session.renew(create=True)
        self.assertFalse(self.session.path.exists())
        self.assertEqual(self.bench.calls, [])

    def test_guard_must_monitor_the_selected_socket(self):
        import json
        import os
        from types import SimpleNamespace
        with patch('frame_testbench.session.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            with self.assertRaisesRegex(RuntimeError, 'no live guard'):
                Session.require_guard(self.session)
            self.session.guard_path.write_text(json.dumps({'pid': os.getpid(), 'socket': '/other/socket'}))
            with self.assertRaisesRegex(RuntimeError, 'no live guard'):
                Session.require_guard(self.session)
            self.session.guard_path.write_text(json.dumps({'pid': os.getpid(), 'socket': self.bench.socket}))
            Session.require_guard(self.session)

    def test_end_is_immediate_and_idempotent(self):
        self.session.renew(create=True)
        self.bench.overridden, self.bench.auto = True, False
        for _ in range(2):
            result = self.session.end()
            self.assertTrue(result['compositor_auto'])
            self.assertFalse(result['session']['active'])
            self.assertFalse(self.bench.overridden)

    def test_corrupt_lease_cannot_keep_overrides_alive(self):
        self.session.path.write_text('{"expires_at": "nan"}')
        self.bench.overridden = True
        self.session.sweep()
        self.assertFalse(self.bench.overridden)

    def test_record_grace_is_temporary_and_preserves_timeout(self):
        self.session.renew(2, create=True)
        self.session.renew(grace=120)
        self.now += 60
        self.assertTrue(self.session.status()['active'])
        self.session.renew()
        self.assertEqual(self.session.status()['timeout_seconds'], 2)
        self.now += 2
        self.session.sweep()
        self.assertFalse(self.session.status()['active'])

    def test_dispatch_arms_recovery_before_override_and_cleans_failure(self):
        from frame_testbench import session
        def fail(args, bench):
            self.assertTrue(self.session.status()['active'])
            bench.overridden, bench.auto = True, False
            raise RuntimeError('operation failed')
        with patch.object(session, 'Session', return_value=self.session):
            with self.assertRaisesRegex(RuntimeError, 'operation failed'):
                session.dispatch(SimpleNamespace(command='worn'), self.bench, fail)
        self.assertFalse(self.bench.overridden)
        self.assertTrue(self.bench.auto)
        self.assertFalse(self.session.status()['active'])

    def test_dispatch_release_restores_power_without_guard(self):
        from frame_testbench import session
        self.session.require_guard.side_effect = RuntimeError('guard stopped')
        self.bench.overridden, self.bench.auto = True, False
        with patch.object(session, 'Session', return_value=self.session):
            reply = session.dispatch(SimpleNamespace(command='release'), self.bench, None)
        self.assertTrue(reply['compositor_auto'])
        self.assertFalse(self.bench.overridden)


if __name__ == '__main__':
    unittest.main()
