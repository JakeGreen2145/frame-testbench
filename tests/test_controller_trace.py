"""Opt-in trace through the production proxy and fake provider, never live VR."""
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import test_input_proxy as proxy_fixture
from frame_testbench import cli, session


class ControllerTraceTests(unittest.TestCase):
    setUpClass = classmethod(lambda cls: proxy_fixture.InputProxyTests.setUpClass())
    setUp = proxy_fixture.InputProxyTests.setUp
    tearDown = proxy_fixture.InputProxyTests.tearDown
    runtime = proxy_fixture.InputProxyTests.runtime
    rpc = proxy_fixture.InputProxyTests.rpc
    restart_runtime = proxy_fixture.InputProxyTests.restart_runtime

    def enabled(self, **environment):
        self.restart_runtime(FRAME_TESTBENCH_TRACE_CONTROLLERS='1', **environment)
        return self.rpc('controller-trace')

    def test_default_off_preserves_original_pointer_and_no_property_reads(self):
        before = self.runtime('property-counts')
        trace = self.rpc('controller-trace')
        self.assertFalse(trace['enabled'])
        self.assertEqual(trace['devices'], [])
        self.assertEqual(before, self.runtime('property-counts'))
        self.assertTrue(self.runtime('forward')['controller_identity'])

    def test_same_context_synchronous_callbacks_and_exact_sdk_forwarding(self):
        trace = self.enabled()
        self.assertEqual(len(trace['devices']), 1)
        device = trace['devices'][0]
        self.assertTrue(device['registered'] and device['active'])
        self.assertEqual((device['generation'], device['index'], device['container']), (1, 9, 109))
        self.assertEqual(device['profile'], '{native_frame}/input/native_controller.json')
        self.assertEqual(device['controller_type'], 'native_frame_controller')
        self.assertEqual(device['role_hint'], 2)
        self.assertEqual((device['profile_error'], device['type_error'], device['role_error']), (0, 0, 0))
        components = {c['path']: c for c in device['components']}
        self.assertEqual(components['/proximity']['creates'], 2)
        self.assertEqual(components['/proximity']['creates_during_activation'], 1)
        self.assertGreaterEqual(components['/proximity']['updates'], 2)
        self.assertTrue(components['/proximity']['boolean'])
        self.assertFalse(components['scalar']['boolean'])
        self.assertEqual(components['scalar']['updates'], 1)
        self.assertEqual(components['scalar']['creates_during_activation'], 1)
        self.assertNotEqual(components['scalar']['last_update_error'], 0)
        self.assertTrue(self.runtime('physical-controller-methods')['ok'])
        before = self.runtime('metrics')
        self.runtime('physical')
        after = self.runtime('metrics')
        self.assertEqual(after['other_x'], 99)
        self.assertFalse(after['other_worn'])
        self.assertGreater(after['pose_count'], before['pose_count'])
        self.assertGreater(self.rpc('controller-trace')['devices'][0]['pose_publications'], device['pose_publications'])
        self.assertTrue(self.runtime('forward')['all_forwarded'])
        self.assertFalse(self.runtime('forward')['controller_identity'])

    def test_input_alias_bypass_is_visible_without_interception_or_inference(self):
        trace = self.enabled(FAKE_TRACE_BYPASS='1')
        device = trace['devices'][0]
        self.assertTrue(device['active'])
        self.assertEqual(trace['alias_passthrough'], 1)
        components = {c['path']: c for c in device['components']}
        self.assertNotIn('scalar', components)
        self.assertEqual(components['/proximity']['creates'], 1)
        self.assertTrue(self.runtime('physical-controller-methods')['ok'])
        self.assertTrue(self.runtime('forward')['all_forwarded'])

    def test_deactivation_generation_and_late_role_readback(self):
        first = self.enabled()['devices'][0]
        self.runtime('trace-role-zero')
        self.assertEqual(self.rpc('controller-trace')['devices'][0]['role_hint'], 0)
        self.runtime('trace-role-right')
        self.assertEqual(self.rpc('controller-trace')['devices'][0]['role_hint'], 2)
        self.runtime('physical-controller-deactivate')
        inactive = self.rpc('controller-trace')['devices'][0]
        self.assertFalse(inactive['active'])
        self.assertEqual(inactive['deactivations'], 1)
        self.runtime('physical-controller-activate')
        again = self.rpc('controller-trace')['devices'][0]
        self.assertGreater(again['generation'], first['generation'])
        self.assertEqual(again['activations'], 2)
        self.assertTrue(again['active'])
        self.assertTrue(self.runtime('physical-controller-methods')['ok'])

    def test_failed_activation_is_returned_and_not_promoted(self):
        device = self.enabled(FAKE_TRACE_ACTIVATE_ERROR='1')['devices'][0]
        self.assertFalse(device['active'])
        self.assertNotEqual(device['activation_error'], 0)
        self.assertGreater(len(device['components']), 0)

    def test_pre_activation_components_are_bounded_pending_then_associated(self):
        trace = self.enabled(FAKE_TRACE_PENDING='1')
        components = {c['path']: c for c in trace['devices'][0]['components']}
        self.assertEqual(components['/proximity']['creates'], 3)
        self.assertEqual(components['/proximity']['creates_during_activation'], 1)
        self.assertEqual(trace['pending_containers'], 1)  # HMD container only.

    def test_registration_and_pending_overflow_never_refuse_real_sdk_calls(self):
        self.enabled()
        result = self.runtime('trace-flood')
        self.assertEqual(result['registrations'], 100)
        trace = self.rpc('controller-trace')
        self.assertEqual(len(trace['devices']), trace['max_devices'])
        self.assertGreater(trace['dropped_devices'], 0)
        self.assertEqual(trace['pending_containers'], trace['max_pending_containers'])
        self.assertGreater(trace['dropped_pending_containers'], 0)
        self.assertEqual(len([c for c in self.runtime('inputs')['components'] if c['container'] >= 1000]), 100)
        self.assertTrue(self.runtime('physical-controller-methods')['ok'])

    def test_component_overflow_is_bounded_and_does_not_change_sdk_components(self):
        trace = self.enabled(FAKE_TRACE_OVERFLOW='1')
        self.assertEqual(len(trace['devices'][0]['components']), trace['max_components_per_container'])
        self.assertGreater(trace['dropped_components'], 0)
        # The fake real provider created all 80 extra components successfully.
        physical = [c for c in self.runtime('inputs')['components'] if c['container'] == 109]
        self.assertEqual(len(physical), 80)
        self.assertLess(len(json.dumps(trace)), 65536)

    def test_trace_readback_does_not_write_inputs_properties_poses_or_state(self):
        self.enabled()
        before_inputs = self.runtime('inputs')
        before_pose = self.runtime('metrics')
        before_properties = self.runtime('property-counts')
        before_status = self.rpc('status')
        for _ in range(3):
            self.assertTrue(self.rpc('controller-trace')['ok'])
        self.assertEqual(self.runtime('inputs'), before_inputs)
        self.assertEqual(self.runtime('metrics'), before_pose)
        self.assertEqual(self.runtime('property-counts')['writes'], before_properties['writes'])
        self.assertGreater(self.runtime('property-counts')['reads'], before_properties['reads'])
        self.assertEqual(self.rpc('status'), before_status)

    def test_trace_cli_does_not_arm_renew_or_clean_up_a_lease(self):
        args = cli.parser().parse_args(['controller-trace'])
        bench = SimpleNamespace(control=lambda command: {'ok': True, 'command': command})
        with patch('frame_testbench.session.Session', side_effect=AssertionError('lease access')):
            self.assertEqual(session.dispatch(args, bench, cli.dispatch), {'ok': True, 'command': 'controller-trace'})


if __name__ == '__main__':
    unittest.main()
