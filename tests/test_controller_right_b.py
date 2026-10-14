"""Right B opt-in uses the same controlled SDK lifetime; no runtime or hardware."""
import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
import test_controller_actions as existing

ROOT = Path(__file__).resolve().parents[1]


def b_fixture():
    s = existing.action_fixture()
    s = s.replace('std::string name(pchActionName);', '''std::string name(pchActionName);
        if(name=="/actions/observe/in/right_b_click"){
            log("resolve_b");
            if(mode("b_action_error"))return VRInputError_NameNotFound;
            if(mode("b_action_zero")){*pHandle=0;return VRInputError_None;}
            if(mode("b_action_alias")){*pHandle=1;return VRInputError_None;}
        }''')
    s = s.replace('if(name!="/actions/observe/in/right_a_click")abort();', '''if(name=="/actions/observe/in/right_b_click"){
        log("b_value");*pActionData={};pActionData->bActive=updates!=2;
        pActionData->bState=updates==4;pActionData->bChanged=updates==4 || updates==5;
        pActionData->fUpdateTime=updates==12?std::numeric_limits<float>::quiet_NaN():-.01f;
        pActionData->activeOrigin=updates==5?1104:updates==6?1108:updates==7 || updates==15?0:updates==8?18446744073709551614ull:1107;
        if(updates==3)return VRInputError_WrongType;
        return VRInputError_None;
    }
    if(name!="/actions/observe/in/right_a_click")abort();''')
    return s


class RightBTests(unittest.TestCase):
    setUp = existing.ControllerActionTests.setUp
    tearDown = existing.ControllerActionTests.tearDown
    calls = existing.ControllerActionTests.calls
    run_observer = existing.ControllerActionTests.run_observer

    @classmethod
    def setUpClass(cls):
        subprocess.run(['sh', str(ROOT / 'scripts/build-controller-actions.sh')], cwd=ROOT, check=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='right-b-', dir=ROOT / 'build')
        cls.work = Path(cls.tmp.name)
        source = cls.work / 'fake.cpp'
        source.write_text(b_fixture())
        cls.library = cls.work / 'libfakeopenvr.so'
        subprocess.run([os.environ.get('CXX','g++'), '-std=c++17','-shared','-fPIC','-pthread','-I',str(ROOT/'vendor'),str(source),'-o',str(cls.library)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_one_sdk_all_flags_bound_context_error_null_and_no_receipt(self):
        r = self.run_observer([str(ROOT/'resources/input-actions.json'),'--dual-stick','--trigger','--right-b','--bindings'],mode='dual_timeline')
        self.assertTrue(r[0]['right_b_diagnostic'])
        rows = [x for x in r if x['event']=='sample']
        self.assertLessEqual(len(rows),300)
        self.assertGreater(len(rows),100)
        self.assertTrue(all(x.get('input_receipt','unknown')=='unknown' for x in r))
        self.assertEqual(rows[0]['right_b']['value'],False)
        self.assertEqual(rows[3]['right_b']['value'],True)
        self.assertEqual(rows[1]['right_b']['active'],False)
        self.assertIsNone(rows[1]['right_b']['value'])
        self.assertIsNone(rows[1]['right_b']['origin'])
        self.assertIsNone(rows[2]['right_b']['active'])
        self.assertIsNone(rows[2]['right_b']['value'])
        self.assertEqual(rows[4]['right_b']['origin']['actual_role'],1)
        self.assertEqual(rows[5]['right_b']['origin']['validation_error'],'device_index_out_of_range')
        self.assertNotEqual(rows[6]['right_b']['origin']['error'],0)
        self.assertEqual(rows[14]['right_b']['origin']['validation_error'],'invalid_origin_handle')
        self.assertEqual(rows[7]['right_b']['origin']['handle'],'18446744073709551614')
        self.assertNotEqual(rows[7]['right_b']['origin']['error'],0)
        self.assertIsNone(rows[11]['right_b']['update_time'])
        self.assertIsNone(rows[13]['right_b']['active'])
        self.assertTrue(any(x['right_b']['origin'] and x['right_b']['origin']['serial'] and x['right_b']['origin']['serial']['error']!=0 for x in rows))
        q = [x for x in r if x['event']=='binding_query']
        self.assertEqual(len(q),15)
        self.assertEqual(sum(x['action']=='right_b' for x in q),3)
        calls=self.calls()
        for call in ('init_background','manifest','shutdown','resolve_b'):
            self.assertEqual(calls.count(call),1)
        self.assertEqual(calls.count('update_actions'),len(rows))
        self.assertEqual(calls.count('b_value'),len(rows)-1)

    def test_invalid_or_duplicate_flags_never_initialize(self):
        for opts in (['--right-b','--right-b'],['--right-b','--unknown']):
            r=self.run_observer([str(ROOT/'resources/input-actions.json'),*opts],code=1)
            self.assertEqual(r[-1]['stage'],'validation')
        self.assertNotIn('init_background',self.calls())

    def test_b_error_zero_and_alias_fail_setup_no_sampling(self):
        for mode in ('b_action_error','b_action_zero','b_action_alias'):
            r=self.run_observer([str(ROOT/'resources/input-actions.json'),'--right-b'],mode=mode,code=1)
            self.assertFalse(any(x['event']=='sample' for x in r))
            self.assertEqual(r[-1]['stage'],'setup')
        self.assertEqual(self.calls().count('shutdown'),3)

if __name__=='__main__':unittest.main()
