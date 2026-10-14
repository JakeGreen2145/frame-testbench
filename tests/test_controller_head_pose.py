"""Opt-in raw HMD records from the existing controlled SDK, never live SteamVR."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import test_controller_actions as existing
from test_controller_right_b import b_fixture

ROOT = Path(__file__).resolve().parents[1]


def head_fixture():
    s = b_fixture()
    s = s.replace('if(unDeviceIndex==0) return TrackedDeviceClass_HMD;', '''if(unDeviceIndex==0) {
        static unsigned prior=0, reads=0;if(prior!=updates){prior=updates;reads=0;}++reads;
        log("head_class");
        if(mode("head_timeline") && updates==11 && reads==1)throw std::runtime_error("private-class-before");
        if(mode("head_timeline") && updates==12 && reads==2)throw std::runtime_error("private-class-after");
        if(mode("head_timeline") && updates==5 && reads==1)return TrackedDeviceClass_Invalid;
        if(mode("head_timeline") && updates==6 && reads==2)return TrackedDeviceClass_Controller;
        return TrackedDeviceClass_HMD;
    }''')
    marker = 'if(eOrigin!=TrackingUniverseStanding || fPredictedSecondsToPhotonsFromNow!=0 || unTrackedDevicePoseArrayCount!=64)abort();'
    s = s.replace(marker, marker + '''log("existing_poses_64");
        if(mode("head_timeline") && updates==13)throw std::runtime_error("private-pose-query");''')
    s = s.replace('p.eTrackingResult=TrackingResult_Running_OK;p.mDeviceToAbsoluteTracking=matrix(0);p.mDeviceToAbsoluteTracking.m[0][3]=.27f;}','''p.eTrackingResult=TrackingResult_Running_OK;p.mDeviceToAbsoluteTracking=matrix(0);p.mDeviceToAbsoluteTracking.m[0][3]=.27f;}
        auto &h=pTrackedDevicePoseArray[k_unTrackedDeviceIndex_Hmd];h.bDeviceIsConnected=true;h.bPoseIsValid=true;
        h.mDeviceToAbsoluteTracking=matrix(updates>=4 && updates<=7?.05f:0);h.mDeviceToAbsoluteTracking.m[1][3]=1.6f;
        if(mode("head_timeline")){
            if(updates==2)h.bDeviceIsConnected=false;
            if(updates==3){h.bPoseIsValid=false;h.eTrackingResult=TrackingResult_Running_OutOfRange;}
            if(updates==4)h.mDeviceToAbsoluteTracking.m[0][0]=std::numeric_limits<float>::quiet_NaN();
            if(updates==7)h.mDeviceToAbsoluteTracking.m[1][1]=std::numeric_limits<float>::infinity();
        }''')
    # Retain the existing dual-hand error/origin timeline while adding head cases.
    s = s.replace('mode("dual_timeline")', '(mode("dual_timeline") || mode("head_timeline"))')
    return '#include <stdexcept>\n' + s


class HeadPoseTests(unittest.TestCase):
    setUp = existing.ControllerActionTests.setUp
    tearDown = existing.ControllerActionTests.tearDown
    calls = existing.ControllerActionTests.calls
    run_observer = existing.ControllerActionTests.run_observer

    @classmethod
    def setUpClass(cls):
        subprocess.run(['sh', str(ROOT/'scripts/build-controller-actions.sh')], cwd=ROOT, check=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='head-pose-', dir=ROOT/'build')
        cls.work = Path(cls.tmp.name)
        source = cls.work/'fake.cpp';source.write_text(head_fixture())
        cls.library = cls.work/'libfakeopenvr.so'
        subprocess.run([os.environ.get('CXX','g++'), '-std=c++17', '-shared','-fPIC','-pthread',
                        '-I',str(ROOT/'vendor'),str(source),'-o',str(cls.library)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_default_has_no_head_records_clocks_or_additional_sdk_queries(self):
        r=self.run_observer();samples=[x for x in r if x['event']=='sample'];calls=self.calls()
        self.assertNotIn('head_pose_diagnostic',r[0])
        self.assertTrue(all('head_pose' not in x for x in r))
        self.assertEqual(calls.count('head_class'),len(samples)) # Existing producer enumeration only.
        self.assertEqual(calls.count('existing_poses_64'),len(samples))
        self.assertEqual(calls.count('init_background'),1)
        self.assertEqual(calls.count('shutdown'),1)

    def test_all_flags_one_sdk_raw_pose_context_clock_unknown_and_bounded_errors(self):
        opts=[str(ROOT/'resources/input-actions.json'),'--head-pose','--dual-stick','--right-b','--trigger','--bindings']
        started=time.monotonic();r=self.run_observer(opts,mode='head_timeline')
        self.assertLess(time.monotonic()-started,19)
        self.assertTrue(r[0]['head_pose_diagnostic']);samples=[x for x in r if x['event']=='sample']
        self.assertGreater(len(samples),100);self.assertLessEqual(len(samples),300)
        self.assertEqual(r[-1]['sample_count'],len(samples))
        self.assertTrue(all(x.get('input_receipt','unknown')=='unknown' for x in r))
        self.assertEqual(len({x['observer_pid'] for x in r if 'observer_pid' in x}),1)
        for row in samples:
            h=row['head_pose'];self.assertEqual(h['device_index'],0);self.assertEqual(h['tracking_space'],'standing')
            self.assertEqual(h['prediction_seconds'],0);self.assertIsNone(h['pose_api_error'])
            self.assertIsInstance(h['query_started_monotonic_ns'],str);self.assertIsInstance(h['query_completed_monotonic_ns'],str)
            self.assertLessEqual(int(h['query_started_monotonic_ns']),int(h['query_completed_monotonic_ns']))
            self.assertLessEqual(h['query_started_utc_unix_ms'],h['query_completed_utc_unix_ms'])
            self.assertEqual(h['context_unchanged'],row['scene_pid_before']==row['scene_pid_after'] and row['dashboard_before']==row['dashboard_after'] and row['input_available_before']==row['input_available'])
        head=[x['head_pose'] for x in samples]
        self.assertTrue(head[0]['sdk_pose_available']);self.assertEqual(head[0]['class_before'],1);self.assertEqual(head[0]['class_after'],1)
        self.assertEqual(head[0]['matrix'],[[1,0,0,0],[0,1,0,1.600000024],[0,0,1,0]])
        self.assertEqual(head[1]['unavailable_reason'],'head_disconnected');self.assertFalse(head[1]['connected'])
        self.assertEqual(head[2]['unavailable_reason'],'head_pose_invalid');self.assertEqual(head[2]['tracking_result'],201)
        self.assertEqual(head[3]['unavailable_reason'],'head_matrix_nonfinite');self.assertIsNone(head[3]['matrix'][0][0])
        self.assertEqual(head[4]['unavailable_reason'],'head_class_unavailable_or_changed');self.assertEqual(head[4]['class_before'],0)
        self.assertEqual(head[5]['class_after'],2);self.assertFalse(head[5]['sdk_pose_available'])
        self.assertEqual(head[6]['unavailable_reason'],'head_matrix_nonfinite');self.assertIsNone(head[6]['matrix'][1][1])
        self.assertEqual(head[10]['query_error'],'head_class_before_query_exception');self.assertIsNone(head[10]['class_before'])
        self.assertEqual(head[11]['query_error'],'head_class_after_query_exception');self.assertIsNone(head[11]['class_after'])
        self.assertEqual(head[12]['query_error'],'head_pose_query_exception');self.assertFalse(head[12]['pose_query_returned'])
        for key in ('connected','valid','tracking_result','matrix','matrix_finite'):self.assertIsNone(head[12][key])
        self.assertFalse(head[12]['sdk_pose_available'])
        self.assertTrue(any(not x['head_pose']['context_unchanged'] and x['scene_pid_before']!=x['scene_pid_after'] for x in samples))
        self.assertTrue(any(x['dashboard_after'] and not x['input_available'] for x in samples))
        self.assertTrue(any(x['scene_app_key']['error']!=0 for x in samples))
        self.assertTrue(all('right_b' in x and 'left_axis0' in x and 'right_trigger' in x for x in samples))
        calls=self.calls()
        for call in ('init_background','manifest','shutdown','resolve_b','resolve_left'):self.assertEqual(calls.count(call),1)
        self.assertEqual(calls.count('existing_poses_64'),len(samples)) # Reused, never a second pose query.
        self.assertEqual(calls.count('head_class'),4*len(samples)) # Two new bracket reads, two existing producers.
        self.assertEqual(sum(x['event']=='binding_query' for x in r),15)
        self.assertNotIn('private-',str(r));self.assertFalse(any(x.startswith('set_') for x in calls))

    def test_duplicate_unknown_head_options_fail_before_sdk(self):
        for flags in (['--head-pose','--head-pose'],['--head-pose','--invalid']):
            r=self.run_observer([str(ROOT/'resources/input-actions.json'),*flags],code=1)
            self.assertEqual(r[-1]['stage'],'validation')
        self.assertNotIn('init_background',self.calls())

    def test_head_mode_shutdown_deadline_keeps_records_unknown(self):
        started=time.monotonic();r=self.run_observer([str(ROOT/'resources/input-actions.json'),'--head-pose'],mode='shutdown_hang',code=124)
        self.assertLess(time.monotonic()-started,22)
        samples=[x for x in r if x['event']=='sample'];self.assertGreater(len(samples),100);self.assertLessEqual(len(samples),300)
        self.assertTrue(all(x['head_pose']['sdk_pose_available'] for x in samples))
        self.assertEqual(r[-1]['error'],'whole_process_deadline');self.assertEqual(r[-1]['outcome'],'unknown')
        self.assertFalse(any(x['event']=='end' for x in r));self.assertNotIn('shutdown',self.calls())


if __name__=='__main__':unittest.main()
