"""Separate observer executable against fake public SDK; no live OpenVR library."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from test_observe import fake_source

ROOT = Path(__file__).resolve().parents[1]


class ControllerPropertyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(['sh', str(ROOT / 'scripts/build-controller-properties.sh')], cwd=ROOT, check=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='controller-properties-', dir=ROOT / 'build')
        cls.work = Path(cls.tmp.name)
        source = cls.work / 'fake.cpp'
        source.write_text(fake_source())
        cls.library = cls.work / 'libfakeopenvr.so'
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-shared', '-fPIC', '-pthread',
                        '-I', str(ROOT / 'vendor'), str(source), '-o', str(cls.library)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.work)
        self.log = Path(self.case.name) / 'calls.log'
        self.env = dict(os.environ, OPENVR_API_LIBRARY=str(self.library), FAKE_LOG=str(self.log),
                        FAKE_MODE='', FAKE_CONTROLLER_PROPERTIES='1')

    def tearDown(self):
        self.case.cleanup()

    def run_observer(self, mode='', args=(), expected_code=0):
        self.env['FAKE_MODE'] = mode
        completed = subprocess.run([str(ROOT / 'build/frame-controller-properties'), *args],
                                   env=self.env, capture_output=True, text=True, timeout=7)
        self.assertEqual(completed.returncode, expected_code, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertIs(value['ok'], expected_code == 0)
        return value

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_properties_and_role_hint_are_separate_from_actual_role(self):
        value = self.run_observer('role_invalid')
        self.assertEqual(value['device_limit'], 64)
        self.assertEqual(value['string_capacity_bytes'], 256)
        self.assertEqual(value['controller_forwarding'], 'unknown')
        rows = {row['device_index']: row for row in value['controllers']}
        self.assertEqual(set(rows), {2, 4, 7, 63})
        right = rows[7]
        self.assertEqual(right['device_class'], 2)
        self.assertEqual(right['actual_role'], 0)
        self.assertEqual(right['role_hint'], {'value': 2, 'error': 0})
        self.assertEqual(right['hand_selection_priority'], {'value': -5, 'error': 0})
        self.assertEqual(right['controller_type']['value'], 'frame_controller')
        self.assertEqual(right['registered_device_type']['value'], 'testbench/right')
        self.assertEqual(right['input_profile_path']['value'], '{frame_controller}/input/frame_controller_profile.json')
        self.assertEqual(right['serial']['value'], 'frame_testbench_right')
        self.assertEqual(len(right['standing_pose']['matrix']), 3)
        self.assertEqual(right['standing_pose']['matrix'][0][3], 701)
        self.assertFalse(right['standing_pose']['connected'])
        calls = self.calls()
        self.assertEqual(sum(line.startswith('string_property ') for line in calls), 4 * 6)
        self.assertEqual(sum(line.startswith('int_property ') for line in calls), 4 * 2)
        self.assertEqual([line for line in calls if line.startswith('poses ')], ['poses 1 64'])
        self.assertEqual(calls[-1], 'shutdown')
        self.assertNotIn('manifest', calls)
        self.assertNotIn('update_actions', calls)

    def test_missing_metadata_stays_unknown_with_property_error(self):
        right = self.run_observer('metadata_error')['controllers'][2]
        for key in ('role_hint', 'hand_selection_priority', 'registered_device_type'):
            self.assertIsNone(right[key]['value'])
            self.assertNotEqual(right[key]['error'], 0)
        self.assertEqual(right['actual_role'], 2)
        self.assertEqual(right['controller_type']['error'], 0)

    def test_oversized_unterminated_or_zero_count_strings_are_not_retried(self):
        for mode, count in [('oversized_metadata', 257), ('unterminated_metadata', 256), ('empty_count_metadata', 0)]:
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                value = self.run_observer(mode)
                for row in value['controllers']:
                    prop = row['registered_device_type']
                    self.assertIsNone(prop['value'])
                    self.assertEqual(prop['error'], 3)  # TrackedProp_BufferTooSmall in the public header
                    self.assertEqual(prop['required_bytes'], count)
                self.assertEqual(sum(line.startswith('string_property ') for line in self.calls()), 24)

    def test_no_controllers_is_empty_observation(self):
        value = self.run_observer('no_controllers')
        self.assertEqual(value['controllers'], [])
        self.assertFalse(any('property ' in line for line in self.calls()))

    def test_invalid_arguments_fail_before_sdk_initialization(self):
        value = self.run_observer(args=['set-role', 'right'], expected_code=1)
        self.assertIn('usage:', value['error'])
        self.assertEqual(self.calls(), [])

    def test_hanging_getter_is_bounded_without_sdk_shutdown(self):
        started = time.monotonic()
        value = self.run_observer('property_hang', expected_code=124)
        self.assertIn('timed out', value['error'])
        self.assertLess(time.monotonic() - started, 6.5)
        self.assertNotIn('shutdown', self.calls())


if __name__ == '__main__':
    unittest.main()
