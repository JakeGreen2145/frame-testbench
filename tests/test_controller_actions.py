"""Fixed-window passive observer against controlled public SDK, never SteamVR."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_observe import fake_source

ROOT = Path(__file__).resolve().parents[1]


def action_fixture():
    overrides = {
        'GetActionHandle': '''if(!manifest_set)abort();
            std::string name(pchActionName);if(name.find("/actions/observe/in/")!=0)abort();
            if(name=="/actions/observe/in/left_thumbstick_position"){
                log("resolve_left");
                if(mode("left_action_error"))return VRInputError_NameNotFound;
                if(mode("left_action_zero")){*pHandle=0;return VRInputError_None;}
                if(mode("left_action_alias")){*pHandle=2;return VRInputError_None;}
            }
            if(mode("right_action_zero") && name=="/actions/observe/in/right_thumbstick_position"){
                *pHandle=0;return VRInputError_None;
            }
            for(size_t i=0;i<action_names.size();++i)if(action_names[i]==name){*pHandle=i+1;return VRInputError_None;}
            action_names.push_back(name);*pHandle=action_names.size();return VRInputError_None;''',
        'GetInputSourceHandle': '''std::string name(pchInputSourcePath);
            if(name!="/user/hand/left" && name!="/user/hand/right")abort();
            *pHandle=name=="/user/hand/left"?104:107;
            if(mode("right_source_zero") && name=="/user/hand/right")*pHandle=0;
            if(name=="/user/hand/left"){
                log("source_left");
                if(mode("left_source_zero"))*pHandle=0;
                if(mode("left_source_alias"))*pHandle=107;
            }
            return mode("source_error") && name=="/user/hand/right"?VRInputError_NameNotFound:VRInputError_None;''',
        'GetControllerRoleForTrackedDeviceIndex': '''if(mode("role_invalid"))return TrackedControllerRole_Invalid;
            if(mode("dual_timeline") && updates==10 && unDeviceIndex==4)return TrackedControllerRole_Invalid;
            switch(unDeviceIndex){
                case 2:case 4:return TrackedControllerRole_LeftHand;
                case 7:return TrackedControllerRole_RightHand;
                case 8:return TrackedControllerRole_Invalid;
                case 63:return TrackedControllerRole_OptOut;
                default:abort();}''',
        'GetActionBindingInfo': '''if(unBindingInfoSize!=sizeof(*pOriginInfo) || unBindingInfoCount!=16 || !punReturnedBindingInfoCount)abort();
            if(action<1 || action>action_names.size())abort();
            log("binding_info_16");
            if(updates>=201){*punReturnedBindingInfoCount=20;return VRInputError_BufferTooSmall;}
            if(updates>=101){*punReturnedBindingInfoCount=17;return VRInputError_None;}
            *punReturnedBindingInfoCount=2;
            for(unsigned i=0;i<2;i++){
                strcpy(pOriginInfo[i].rchDevicePathName,"/user/hand/right");
                strcpy(pOriginInfo[i].rchInputPathName,"/input/a/click");
                if(i==0)strcpy(pOriginInfo[i].rchModeName,"button");
                strcpy(pOriginInfo[i].rchSlotName,"click");
                strcpy(pOriginInfo[i].rchInputSourceType,"button");
            }return VRInputError_None;''',
        'GetActionOrigins': '''if(actionSetHandle!=90 || digitalActionHandle<1 || digitalActionHandle>action_names.size() || originOutCount!=64)abort();
            log("action_origins_64");
            if(updates>=201)return VRInputError_InvalidHandle;
            originsOut[0]=1107;originsOut[1]=1104;originsOut[2]=18446744073709551614ull;
            return VRInputError_None;''',
        'GetOriginTrackedDeviceInfo': '''if(unOriginInfoSize!=sizeof(*pOriginInfo))abort();
            if(origin==18446744073709551614ull)return VRInputError_InvalidHandle;
            if(mode("dual_timeline") && origin==0){
                if(updates==7)return VRInputError_InvalidHandle;
                *pOriginInfo={};pOriginInfo->trackedDeviceIndex=4;
                strcpy(pOriginInfo->rchRenderModelComponentName,"input");return VRInputError_None;
            }
            if(origin==1108){*pOriginInfo={};pOriginInfo->trackedDeviceIndex=updates==6?64:8;
                pOriginInfo->devicePath=18446744073709551613ull;
                strcpy(pOriginInfo->rchRenderModelComponentName,"input");return VRInputError_None;}
            if(origin!=1104 && origin!=1107)abort();
            *pOriginInfo={};pOriginInfo->devicePath=origin-1000;pOriginInfo->trackedDeviceIndex=origin==1104?4:7;
            strcpy(pOriginInfo->rchRenderModelComponentName,"input");return VRInputError_None;''',
        'GetCurrentSceneFocusProcess': 'return updates<24?777:888;',
        'IsDashboardVisible': 'return updates>=35;',
        'GetApplicationKeyByProcessId': '''if(unAppKeyBufferLen!=256) abort();
            log("app_key");
            if(updates==6){memset(pchAppKeyBuffer,'x',256);return VRApplicationError_None;}
            if(updates==7)return VRApplicationError_BufferTooSmall;
            strcpy(pchAppKeyBuffer,unProcessId==777?"vrlink.client":"other.scene");return VRApplicationError_None;''',
        'GetDigitalActionData': '''if(unActionDataSize!=sizeof(*pActionData))abort();
            const auto &name=check_action(action,ulRestrictToDevice);
            if(name!="/actions/observe/in/right_a_click")abort();
            *pActionData={};pActionData->bActive=updates<30;
            pActionData->bState=updates>=4 && updates<=10;pActionData->bChanged=updates==4 || updates==11;
            pActionData->fUpdateTime=-.01f;pActionData->activeOrigin=updates<20?1107:1104;
            return VRInputError_None;''',
        'GetAnalogActionData': '''if(unActionDataSize!=sizeof(*pActionData))abort();
            const auto &name=check_action(action,ulRestrictToDevice);
            if(name=="/actions/observe/in/left_thumbstick_position"){
                log("left_axis_value");*pActionData={};pActionData->bActive=updates!=2 && updates<30;
                pActionData->x=updates==4?std::numeric_limits<float>::quiet_NaN():-.25f;
                pActionData->y=updates==4?std::numeric_limits<float>::infinity():.45f;
                pActionData->activeOrigin=updates==5?18446744073709551614ull:
                    updates==6 || updates==9?1108:updates==7 || updates==15?0:updates==8?1107:1104;
                if(updates==3)return VRInputError_WrongType;
                return VRInputError_None;
            }
            if(name=="/actions/observe/in/right_trigger_value") {
                log("trigger_value");*pActionData={};pActionData->bActive=updates<30;pActionData->activeOrigin=1107;
                pActionData->x=updates==12?std::numeric_limits<float>::quiet_NaN():updates>=4 && updates<=10?.60f:0;
                if(updates==13)return VRInputError_WrongType;
                return VRInputError_None;
            }
            if(name!="/actions/observe/in/right_thumbstick_position")abort();
            *pActionData={};pActionData->bActive=updates<30;pActionData->activeOrigin=mode("dual_timeline") && updates==6?1108:1107;
            pActionData->x=updates==12?std::numeric_limits<float>::quiet_NaN():.35f;pActionData->y=-.65f;
            return VRInputError_None;''',
        'IsInputAvailable': 'return updates<30;',
        'GetStringTrackedDeviceProperty': '''if(prop!=Prop_SerialNumber_String || unBufferSize!=256)abort();
            if(unDeviceIndex!=2 && unDeviceIndex!=4 && unDeviceIndex!=7 && unDeviceIndex!=8 && unDeviceIndex!=63)abort();
            if(pError)*pError=TrackedProp_Success;
            log("serial_256");
            if((updates==16 && unDeviceIndex==7) || (mode("dual_timeline") && updates==11 && unDeviceIndex==4))return 257;
            const char *s=unDeviceIndex==7 || (updates==17 && unDeviceIndex==63)?"frame_testbench_right":"physical-controller";
            if(mode("dual_timeline") && (unDeviceIndex==4 || (updates==18 && unDeviceIndex==63)))s="frame_testbench_left";
            auto size=uint32_t(strlen(s)+1);memcpy(pchValue,s,size);return size;''',
        'GetDeviceToAbsoluteTrackingPose': '''if(eOrigin!=TrackingUniverseStanding || fPredictedSecondsToPhotonsFromNow!=0 || unTrackedDevicePoseArrayCount!=64)abort();
            for(unsigned i=0;i<64;i++){auto &p=pTrackedDevicePoseArray[i];p={};p.bDeviceIsConnected=p.bPoseIsValid=i==7 || (mode("dual_timeline") && i==4);
            p.eTrackingResult=TrackingResult_Running_OK;p.mDeviceToAbsoluteTracking=matrix(0);p.mDeviceToAbsoluteTracking.m[0][3]=.27f;}''',
    }
    source = fake_source(overrides)
    return source.replace('if(mode("update_error"))',
                          'if(mode("dual_timeline") && updates==14)return VRInputError_NoData; if(mode("update_error"))')


class ControllerActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(['sh', str(ROOT / 'scripts/build-controller-actions.sh')], cwd=ROOT, check=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='controller-actions-', dir=ROOT / 'build')
        cls.work = Path(cls.tmp.name)
        source = cls.work / 'fake.cpp'
        source.write_text(action_fixture())
        cls.library = cls.work / 'libfakeopenvr.so'
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-shared', '-fPIC', '-pthread',
                        '-I', str(ROOT / 'vendor'), str(source), '-o', str(cls.library)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.work)
        self.path = Path(self.case.name)
        self.log = self.path / 'calls.log'
        self.env = dict(os.environ, OPENVR_API_LIBRARY=str(self.library), FAKE_LOG=str(self.log), FAKE_MODE='', FAKE_CONTROLLER_PROPERTIES='1')

    def tearDown(self):
        self.case.cleanup()

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run_observer(self, args=None, mode='', code=0):
        self.env['FAKE_MODE'] = mode
        result = subprocess.run([str(ROOT / 'build/frame-controller-actions'), *(args if args is not None else [str(ROOT / 'resources/input-actions.json')])],
                                env=self.env, capture_output=True, text=True, timeout=23)
        self.assertEqual(result.returncode, code, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_one_client_retains_timeline_origins_context_and_unknown_receipt(self):
        start = time.monotonic()
        records = self.run_observer()
        self.assertLess(time.monotonic() - start, 19)
        self.assertEqual(records[0]['event'], 'start')
        self.assertEqual(records[-1]['event'], 'end')
        samples = [row for row in records if row['event'] == 'sample']
        self.assertGreater(len(samples), 100)
        self.assertLessEqual(len(samples), 300)
        self.assertEqual([row['sequence'] for row in samples], list(range(len(samples))))
        self.assertEqual(len({row['observer_pid'] for row in records if 'observer_pid' in row}), 1)
        self.assertTrue(all(row['input_receipt'] == 'unknown' for row in records))
        self.assertEqual(records[-1]['sample_count'], len(samples))
        self.assertTrue(any(row['right_a']['value'] is True and row['right_a']['origin']['device_index'] == 7 for row in samples))
        self.assertTrue(any(row['right_a']['value'] is False and row['right_a']['origin']['device_index'] == 4 for row in samples if row['right_a']['origin']))
        self.assertTrue(any(row['right_a']['active'] is False and row['right_a']['value'] is None and row['right_a']['origin'] is None for row in samples))
        self.assertTrue(any(row['scene_pid_before'] != row['scene_pid_after'] for row in samples))
        self.assertTrue(any(row['dashboard_after'] and not row['input_available'] for row in samples))
        self.assertTrue(any(row['input_available_before'] and not row['input_available'] for row in samples))
        self.assertTrue(any(row['scene_app_key']['value'] is None and row['scene_app_key']['validation_error'] for row in samples))
        self.assertTrue(any(row['scene_app_key']['error'] == 200 for row in samples))
        self.assertTrue(any(row['axis0']['value'] == [None, -0.6499999762] for row in samples))
        self.assertTrue(any(row['producer']['match_count'] == 2 and row['producer']['device'] is None for row in samples))
        self.assertTrue(any(row['right_a']['origin'] and row['right_a']['origin']['serial']['error'] == 3
                            and row['right_a']['origin']['serial']['value'] is None for row in samples))
        self.assertTrue(all(isinstance(row['right_a']['origin']['handle'], str) for row in samples if row['right_a']['origin']))
        self.assertTrue(samples[0]['producer']['device']['standing_pose']['valid'])
        self.assertTrue(samples[0]['producer']['device']['standing_pose']['connected'])
        calls = self.calls()
        self.assertEqual(calls.count('init_background'), 1)
        self.assertEqual(calls.count('manifest'), 1)
        self.assertEqual(calls.count('shutdown'), 1)
        self.assertEqual(calls.count('update_actions'), len(samples))
        self.assertFalse(any(line.startswith('set_') for line in calls))
        self.assertNotIn('binding_info_16', calls)
        self.assertNotIn('action_origins_64', calls)
        self.assertNotIn('trigger_value', calls)
        self.assertTrue(all('right_trigger' not in row for row in samples))
        self.assertNotIn('trigger_diagnostic', records[0])
        self.assertNotIn('dual_stick_diagnostic', records[0])
        self.assertNotIn('resolve_left', calls)
        self.assertNotIn('source_left', calls)
        self.assertNotIn('left_axis_value', calls)
        self.assertTrue(all('left_axis0' not in row and 'left_producer' not in row for row in samples))
        self.assertTrue(all('actual_role' not in row['right_a']['origin'] for row in samples if row['right_a']['origin']))

    def test_trigger_opt_in_same_context_and_optional_binding_queries(self):
        records = self.run_observer(args=[str(ROOT / 'resources/input-actions.json'), '--trigger', '--bindings'])
        self.assertTrue(records[0]['trigger_diagnostic'])
        self.assertNotIn('dual_stick_diagnostic',records[0])
        self.assertNotIn('resolve_left',self.calls())
        self.assertNotIn('left_axis_value',self.calls())
        samples = [r for r in records if r['event'] == 'sample']
        self.assertEqual(self.calls().count('trigger_value'), len(samples))
        self.assertTrue(any(r['right_trigger']['active'] and abs(r['right_trigger']['value']-.60)<1e-6
                            and r['right_trigger']['origin']['device_index']==7 for r in samples if r['right_trigger']['value'] is not None))
        self.assertTrue(any(r['right_trigger']['active'] and r['right_trigger']['value'] is None for r in samples))
        self.assertTrue(any(r['right_trigger']['error'] != 0 and r['right_trigger']['active'] is None and r['right_trigger']['value'] is None and r['right_trigger']['origin'] is None for r in samples))
        self.assertTrue(any(r['right_trigger']['active'] is False and r['right_trigger']['value'] is None and r['right_trigger']['origin'] is None for r in samples))
        self.assertTrue(all(r['input_receipt']=='unknown' for r in samples))
        queries = [r for r in records if r['event']=='binding_query']
        self.assertEqual(len(queries),9)
        self.assertEqual({r['action'] for r in queries},{'right_a','axis0','right_trigger'})
        self.assertEqual(self.calls().count('init_background'),1)
        self.assertEqual(self.calls().count('manifest'),1)
        self.assertEqual(self.calls().count('shutdown'),1)
        self.assertEqual(self.calls().count('binding_info_16'),9)
        self.assertEqual(self.calls().count('action_origins_64'),9)
        self.assertFalse(any(line.startswith('set_') for line in self.calls()))

    def test_dual_stick_context_roles_indices_nulls_and_max_four_binding_actions(self):
        records=self.run_observer(args=[str(ROOT/'resources/input-actions.json'),'--dual-stick','--trigger','--bindings'],mode='dual_timeline')
        self.assertTrue(records[0]['dual_stick_diagnostic'])
        self.assertTrue(records[0]['trigger_diagnostic'])
        samples=[r for r in records if r['event']=='sample']
        self.assertLessEqual(len(samples),300)
        self.assertEqual([r['sequence'] for r in samples],list(range(len(samples))))
        self.assertEqual(len({r['observer_pid'] for r in records if 'observer_pid' in r}),1)
        self.assertEqual(records[-1]['sample_count'],len(samples))
        first=samples[0]
        self.assertEqual(first['left_axis0']['value'],[-.25,.4499999881])
        self.assertEqual(first['left_axis0']['origin']['device_index'],4)
        self.assertEqual(first['left_axis0']['origin']['device_class'],2)
        self.assertEqual(first['left_axis0']['origin']['actual_role'],1)
        self.assertEqual(first['left_axis0']['origin']['serial']['value'],'frame_testbench_left')
        self.assertEqual(first['left_producer']['device']['device_index'],4)
        self.assertTrue(first['left_producer']['device']['standing_pose']['connected'])
        self.assertTrue(first['left_producer']['device']['standing_pose']['valid'])
        self.assertEqual(first['axis0']['origin']['actual_role'],2)
        self.assertEqual(first['right_a']['origin']['device_index'],7)
        inactive=samples[1]['left_axis0']
        self.assertIs(inactive['active'],False)
        self.assertIsNone(inactive['value'])
        self.assertIsNone(inactive['origin'])
        failed=samples[2]['left_axis0']
        self.assertNotEqual(failed['error'],0)
        self.assertIsNone(failed['active'])
        self.assertIsNone(failed['value'])
        self.assertIsNone(failed['origin'])
        self.assertEqual(samples[3]['left_axis0']['value'],[None,None])
        failed_origin=samples[4]['left_axis0']['origin']
        self.assertEqual(failed_origin['handle'],'18446744073709551614')
        self.assertNotEqual(failed_origin['error'],0)
        self.assertIsNone(failed_origin['device_index'])
        self.assertIsNone(failed_origin['actual_role'])
        outside=samples[5]['left_axis0']['origin']
        self.assertEqual(outside['error'],0)
        self.assertFalse(outside['device_index_valid'])
        self.assertEqual(outside['validation_error'],'device_index_out_of_range')
        self.assertIsNone(outside['serial'])
        self.assertIsNone(outside['device_class'])
        self.assertIsNone(outside['actual_role'])
        self.assertIsNone(samples[5]['axis0']['origin']['device_index'])
        self.assertEqual(samples[5]['axis0']['origin']['validation_error'],'device_index_out_of_range')
        self.assertEqual(samples[6]['left_axis0']['origin']['handle'],'0')
        self.assertNotEqual(samples[6]['left_axis0']['origin']['error'],0)
        # A left-restricted query may return a foreign origin: report it, never relabel it.
        foreign=samples[7]['left_axis0']['origin']
        self.assertEqual(foreign['device_index'],7)
        self.assertEqual(foreign['actual_role'],2)
        self.assertEqual(foreign['serial']['value'],'frame_testbench_right')
        tracker=samples[8]['left_axis0']['origin']
        self.assertEqual(tracker['device_class'],3)
        self.assertEqual(tracker['actual_role'],0)
        self.assertEqual(tracker['device_path_handle'],'18446744073709551613')
        self.assertEqual(samples[9]['left_axis0']['origin']['actual_role'],0)
        self.assertEqual(samples[10]['left_axis0']['origin']['serial']['error'],3)
        self.assertIsNone(samples[10]['left_axis0']['origin']['serial']['value'])
        update_failed=samples[13]
        self.assertNotEqual(update_failed['update_error'],0)
        self.assertTrue(all(update_failed[n]['active'] is None and update_failed[n]['value'] is None and update_failed[n]['origin'] is None
                            for n in ('right_a','axis0','right_trigger','left_axis0')))
        invalid_success=samples[14]['left_axis0']['origin']
        self.assertEqual(invalid_success['error'],0)
        self.assertFalse(invalid_success['device_index_valid'])
        self.assertEqual(invalid_success['validation_error'],'invalid_origin_handle')
        self.assertIsNone(invalid_success['device_index'])
        self.assertIsNone(invalid_success['actual_role'])
        self.assertEqual(samples[17]['left_producer']['match_count'],2)
        self.assertIsNone(samples[17]['left_producer']['device'])
        self.assertTrue(all(r['input_receipt']=='unknown' and r['controller_forwarding']=='unknown' for r in samples))
        queries=[r for r in records if r['event']=='binding_query']
        self.assertEqual(len(queries),12)
        self.assertEqual({r['action'] for r in queries},{'right_a','axis0','right_trigger','left_axis0'})
        self.assertEqual({r['sample_sequence'] for r in queries},{0,100,200})
        self.assertTrue(all(r['binding_capacity']==16 and r['origins_capacity']==64 for r in queries))
        calls=self.calls()
        self.assertEqual(calls.count('left_axis_value'),len(samples)-1)
        self.assertEqual(calls.count('trigger_value'),len(samples)-1)
        for call in ('init_background','manifest','shutdown','resolve_left','source_left'):self.assertEqual(calls.count(call),1)
        self.assertEqual(calls.count('binding_info_16'),12)
        self.assertFalse(any(c.startswith('set_') for c in calls))

    def test_dual_stick_setup_rejects_left_right_invalid_or_aliased_handles(self):
        for mode in ('left_action_zero','left_action_error','left_action_alias','left_source_zero','left_source_alias','right_action_zero','right_source_zero','source_error'):
            with self.subTest(mode=mode):
                records=self.run_observer(args=[str(ROOT/'resources/input-actions.json'),'--dual-stick'],mode=mode,code=1)
                self.assertEqual(records[-1]['event'],'error')
                self.assertEqual(records[-1]['stage'],'setup')
                self.assertEqual(records[-1]['outcome'],'unknown')
                self.assertFalse(any(r['event']=='sample' for r in records))
        self.assertNotIn('update_actions',self.calls())
        self.assertNotIn('left_axis_value',self.calls())
        self.assertFalse(any(c.startswith('set_') for c in self.calls()))

    def test_opt_in_binding_reads_are_bounded_in_same_client_and_errors_unknown(self):
        records = self.run_observer(args=[str(ROOT / 'resources/input-actions.json'), '--bindings'])
        self.assertTrue(records[0]['binding_diagnostics'])
        queries = [r for r in records if r['event'] == 'binding_query']
        self.assertEqual(len(queries), 6)
        self.assertEqual({r['sample_sequence'] for r in queries}, {0, 100, 200})
        self.assertEqual({r['action'] for r in queries}, {'right_a', 'axis0'})
        self.assertTrue(all(r['binding_capacity'] == 16 and r['origins_capacity'] == 64 for r in queries))
        self.assertTrue(all(r['origins_completeness'] == 'unknown' for r in queries))
        self.assertTrue(all(r['returned_binding_count'] == 17 and not r['binding_count_valid'] for r in queries if r['sample_sequence'] == 100))
        self.assertTrue(all(r['binding_error'] != 0 and r['origins_error'] != 0 for r in queries if r['sample_sequence'] == 200))
        entries = [r for r in records if r['event'] == 'binding_entry']
        self.assertEqual(len(entries), 4)
        self.assertTrue(all(r['sample_sequence'] == 0 for r in entries))
        self.assertTrue(any(r['mode']['value'] is None and r['mode']['validation_error'] == 'unterminated' for r in entries))
        origins = [r for r in records if r['event'] == 'binding_origin']
        self.assertEqual(len(origins), 12)
        self.assertTrue(any(r['origin']['handle'] == '18446744073709551614' and r['origin']['error'] != 0 and r['origin']['device_index'] is None for r in origins))
        self.assertTrue(any(r['origin']['serial'] and r['origin']['serial']['value'] == 'frame_testbench_right' and r['origin']['component']['value'] == 'input' for r in origins))
        self.assertTrue(all(len(json.dumps(r).encode()) < 16384 for r in records))
        calls = self.calls()
        self.assertEqual(calls.count('binding_info_16'), 6)
        self.assertEqual(calls.count('action_origins_64'), 6)
        self.assertEqual(calls.count('init_background'), 1)
        self.assertEqual(calls.count('manifest'), 1)
        self.assertEqual(calls.count('shutdown'), 1)
        self.assertFalse(any(line.startswith('set_') for line in calls))

    def test_shutdown_deadline_keeps_flushed_samples_and_reports_unknown(self):
        start = time.monotonic()
        records = self.run_observer(mode='shutdown_hang', code=124)
        self.assertLess(time.monotonic() - start, 22)
        samples = [row for row in records if row['event'] == 'sample']
        self.assertGreater(len(samples), 100)
        self.assertEqual(records[-1]['event'], 'error')
        self.assertEqual(records[-1]['error'], 'whole_process_deadline')
        self.assertEqual(records[-1]['outcome'], 'unknown')
        self.assertFalse(any(row['event'] == 'end' for row in records))
        self.assertNotIn('shutdown', self.calls())

    def test_bad_manifest_and_binding_files_fail_before_sdk(self):
        manifest = self.path / 'input-actions.json'
        binding = self.path / 'frame-controller-bindings.json'
        for resource in (manifest, binding):
            shutil.copyfile(ROOT / 'resources' / resource.name, resource)
        for which in ('manifest', 'binding'):
            with self.subTest(which=which):
                target = manifest if which == 'manifest' else binding
                original = target.read_bytes()
                target.write_bytes(original + b' ')
                records = self.run_observer(args=[str(manifest)], code=1)
                target.write_bytes(original)
                self.assertEqual(records[0]['stage'], 'validation')
                self.assertEqual(records[0]['error'], 'action_resource_pin_mismatch')
                self.assertEqual(self.calls(), [])
        manifest.unlink()
        manifest.symlink_to(ROOT / 'resources/input-actions.json')
        self.run_observer(args=[str(manifest)], code=1)
        self.assertEqual(self.calls(), [])

    def test_wrong_arguments_fail_before_sdk(self):
        self.run_observer(args=[], code=1)
        self.run_observer(args=['relative/input-actions.json'], code=1)
        self.run_observer(args=[str(ROOT / 'resources/input-actions.json'), '--duration', '1'], code=1)
        self.run_observer(args=[str(ROOT / 'resources/input-actions.json'), '--trigger', '--trigger'], code=1)
        self.run_observer(args=[str(ROOT / 'resources/input-actions.json'), '--bindings', '--bindings'], code=1)
        self.run_observer(args=[str(ROOT / 'resources/input-actions.json'), '--dual-stick', '--dual-stick'], code=1)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
