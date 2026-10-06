"""CPU-only tests: compile the real executable and a public-header OpenVR boundary.

No test loads SteamVR. Unused virtual methods abort, so unexpected side effects
fail the subprocess rather than touching hardware. Only Python stdlib and g++.
"""
import json
import os
import re
import struct
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))


def fake_source():
    header = (ROOT / 'vendor/openvr.h').read_text()
    # Derive actual vtable signatures, never guessed ABI layouts. Every method
    # outside the explicit read/capture/settings/debug contract aborts.
    overrides = {
        'GetDeviceToAbsoluteTrackingPose': '''if(fPredictedSecondsToPhotonsFromNow != 0 || unTrackedDevicePoseArrayCount != 1) abort();
            auto &p=pTrackedDevicePoseArray[0]; p={}; p.bDeviceIsConnected=true;
            p.bPoseIsValid=!mode("invalid_pose"); p.eTrackingResult=TrackingResult_Running_OK;
            p.mDeviceToAbsoluteTracking=matrix(float(eOrigin)); p.vVelocity.v[0]=0.25f;''',
        'GetSeatedZeroPoseToStandingAbsoluteTrackingPose': 'return matrix(10);',
        'GetRawZeroPoseToStandingAbsoluteTrackingPose': 'return matrix(20);',
        'GetTrackedDeviceActivityLevel': 'return k_EDeviceActivityLevel_UserInteraction;',
        'GetStringTrackedDeviceProperty': '''if(pError) *pError=TrackedProp_Success;
            const char *s=prop==Prop_ModelNumber_String?"Fake \\\"HMD\\\"":prop==Prop_SerialNumber_String?"TEST-001":"fake_driver";
            if(mode("property_error")){if(pError) *pError=TrackedProp_UnknownProperty; return 0;}
            uint32_t n=uint32_t(strlen(s)+1); if(unBufferSize<n){if(pError) *pError=TrackedProp_BufferTooSmall; return n;}
            memcpy(pchValue,s,n); return n;''',
        'GetTimeSinceLastVsync': '*pfSecondsSinceLastVsync=0.01f; *pulFrameCounter=12345; return true;',
        'GetFrameTiming': '''if(pTiming->m_nSize!=sizeof(*pTiming)) abort();
            if(mode("no_timing")) return false;
            pTiming->m_nFrameIndex=100+unFramesAgo; pTiming->m_nNumFramePresents=2;
            pTiming->m_flSystemTimeInSeconds=42.5; pTiming->m_HmdPose.bPoseIsValid=true;
            pTiming->m_HmdPose.mDeviceToAbsoluteTracking=matrix(30); return true;''',
        'GetTrackingSpace': 'return TrackingUniverseStanding;',
        'GetBool': '''check_setting(pchSection,pchSettingsKey); log("get_bool");
            if(peError) *peError=mode("read_error")?VRSettingsError_ReadFailed:VRSettingsError_None;
            return mode("mismatch")?true:setting;''',
        'SetBool': '''check_setting(pchSection,pchSettingsKey); log(bValue?"set_true":"set_false");
            if(peError) *peError=mode("write_error")?VRSettingsError_WriteFailed:VRSettingsError_None;
            setting=bValue;''',
        'RequestScreenshot': '''if(type!=VRScreenshotType_Stereo) abort(); log("request_screenshot");
            if(mode("screenshot_error")) return VRScreenshotError_RequestFailed;
            *pOutScreenshotHandle=77;
            std::string a=pchPreviewFilename,b=pchVRFilename;
            worker=std::thread([a,b]{
                auto bytes=image;
                if(mode("corrupt")) bytes[45]^=1;
                std::ofstream f(a,std::ios::binary); f.write((char*)bytes.data(),33); f.flush();
                if(mode("incomplete")) return;
                std::this_thread::sleep_for(std::chrono::milliseconds(300));
                f.write((char*)bytes.data()+33,bytes.size()-33); f.close();
                std::this_thread::sleep_for(std::chrono::milliseconds(200));
                std::ofstream g(b,std::ios::binary); g.write((char*)bytes.data(),bytes.size()); g.close();
                log("pngs_complete");
            }); return VRScreenshotError_None;''',
        'DriverDebugRequest': '''log("debug");
            if(unDeviceIndex!=3 || std::string(pchRequest)!="test request") abort();
            const char *s="response\\n\\\"ok\\\""; auto n=uint32_t(strlen(s)+1);
            if(mode("debug_large")) return unResponseBufferSize+1;
            if(unResponseBufferSize>=n) memcpy(pchResponseBuffer,s,n); return n;''',
    }
    source = '''#include "openvr.h"
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <thread>
#include <chrono>
#include <vector>
#include <cstdio>
using namespace vr;
bool setting=true;
std::thread worker;
bool mode(const char *m){const char *v=getenv("FAKE_MODE");return v && std::string(v)==m;}
void log(const char *s){const char *p=getenv("FAKE_LOG");if(p) std::ofstream(p,std::ios::app)<<s<<"\\n";}
void check_setting(const char *s,const char *k){if(std::string(s)!="power" || std::string(k)!="pauseCompositorOnStandby") abort();}
HmdMatrix34_t matrix(float x){HmdMatrix34_t m={};for(int i=0;i<3;i++)m.m[i][i]=1;m.m[0][3]=x;return m;}
'''
    source += 'std::vector<unsigned char> image={' + ','.join(str(b) for b in png()) + '};\n'
    for interface in ('IVRSystem', 'IVRCompositor', 'IVRSettings', 'IVRScreenshots', 'IVRDebug'):
        match = re.search(r'class\s+' + interface + r'\s*\{(.*?)\n\s*\};', header, re.DOTALL)
        assert match is not None, interface
        body = match.group(1)
        body = re.sub(r'/\*.*?\*/|//[^\n]*', '', body, flags=re.DOTALL)
        methods = re.findall(r'virtual\s+(.*?)\s*=\s*0\s*;', body, re.DOTALL)
        source += f'class Fake{interface}: public {interface} {{ public:\n'
        for method in methods:
            name_match = re.search(r'(\w+)\s*\(', method)
            assert name_match is not None, method
            name = name_match.group(1)
            source += method + ' override {' + overrides.get(name, 'abort();') + '}\n'
        source += f'}} object{interface};\n'
    source += '''extern "C" {
uint32_t VR_InitInternal2(EVRInitError *e,EVRApplicationType t,const char*){
 if(t!=VRApplication_Background) abort(); log("init_background"); printf("sdk noise\\n"); fflush(stdout);
 if(mode("hang")) for(;;) std::this_thread::sleep_for(std::chrono::seconds(1));
 *e=mode("init_error")?VRInitError_Init_HmdNotFound:VRInitError_None;return 1;
}
void VR_ShutdownInternal(){log("shutdown_enter");if(worker.joinable()) worker.join();log("shutdown");printf("shutdown noise\\n");}
bool VR_IsInterfaceVersionValid(const char *v){log(v);return !mode("bad_version");}
void *VR_GetGenericInterface(const char *v,EVRInitError *e){*e=VRInitError_None;
 if(mode("missing_interface")){*e=VRInitError_Init_InterfaceNotFound;return nullptr;}
'''
    for interface in ('IVRSystem', 'IVRCompositor', 'IVRSettings', 'IVRScreenshots', 'IVRDebug'):
        source += f'if(std::string(v)=={interface}_Version) return &object{interface};\n'
    return source + '*e=VRInitError_Init_InterfaceNotFound;return nullptr;}\n}\n'


class ObserveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = ROOT / 'scripts/build-observe.sh'
        if not script.exists():
            raise AssertionError('missing native observation build script')
        subprocess.run(['sh', str(script)], cwd=ROOT, check=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='observe-', dir=ROOT / 'build')
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
        self.path = Path(self.case.name)
        self.log = self.path / 'calls.log'
        self.env = dict(os.environ, OPENVR_API_LIBRARY=str(self.library), FAKE_LOG=str(self.log),
                        FAKE_MODE='', FRAME_OBSERVE_TIMEOUT_SECONDS='3')

    def tearDown(self):
        self.case.cleanup()

    def run_observe(self, *args, ok=True, mode=''):
        self.env['FAKE_MODE'] = mode
        p = subprocess.run([str(ROOT / 'build/frame-observe'), *map(str, args)], env=self.env,
                           capture_output=True, text=True, timeout=6, check=False)
        self.assertEqual(p.returncode == 0, ok, (p.returncode, p.stdout, p.stderr))
        data = json.loads(p.stdout)
        self.assertIs(data['ok'], ok)
        if not ok:
            self.assertTrue(data['error'])
        return data

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_bad_cli_is_rejected_before_sdk(self):
        for args in [(), ('wat',), ('status', 'extra'), ('capture',), ('capture', 'relative'),
                     ('setting', 'yes'), ('setting',), ('debug', '-1', 'x'), ('debug', '64', 'x'),
                     ('debug', '3x', 'x'), ('debug', '3', '')]:
            with self.subTest(args=args):
                self.run_observe(*args, ok=False)
                self.assertEqual(self.calls(), [])

    def test_missing_library(self):
        self.env['OPENVR_API_LIBRARY'] = str(self.path / 'missing.so')
        self.run_observe('status', ok=False)
        self.assertEqual(self.calls(), [])

    def test_bad_sdk(self):
        for mode in ('init_error', 'bad_version', 'missing_interface'):
            with self.subTest(mode=mode):
                self.run_observe('status', ok=False, mode=mode)

    def test_status_is_read_only_and_has_distinct_coordinate_spaces(self):
        d = self.run_observe('status')
        self.assertEqual(d['hmd']['model'], 'Fake "HMD"')
        self.assertEqual(d['hmd']['driver'], 'fake_driver')
        self.assertEqual(d['hmd']['serial'], 'TEST-001')
        self.assertEqual(d['hmd']['activity_level'], 1)
        for space, x in [('standing', 1), ('raw', 2), ('seated', 0)]:
            p = d['hmd']['poses'][space]
            self.assertTrue(p['valid'])
            self.assertTrue(p['connected'])
            self.assertEqual(p['tracking_result'], 200)
            self.assertEqual(p['matrix'], [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0]])
            self.assertEqual(p['velocity'], [0.25, 0, 0])
        self.assertEqual(d['transforms']['raw_to_standing'][0][3], 20)
        self.assertEqual(d['transforms']['seated_to_standing'][0][3], 10)
        self.assertEqual(d['compositor']['frame_index'], 100)
        self.assertEqual(d['compositor']['previous_frame_index'], 101)
        self.assertEqual(d['compositor']['vsync_frame_counter'], 12345)
        self.assertTrue(d['settings']['pauseCompositorOnStandby'])
        self.assertEqual(self.calls().count('init_background'), 1)
        self.assertEqual(self.calls()[-1], 'shutdown')
        self.assertFalse(any(x.startswith('set_') for x in self.calls()))

    def test_invalid_pose_is_not_claimed_valid(self):
        d = self.run_observe('status', mode='invalid_pose')
        self.assertFalse(d['hmd']['poses']['standing']['valid'])

    def test_absent_timing_is_explicit(self):
        d = self.run_observe('status', mode='no_timing')
        self.assertFalse(d['compositor']['timing_available'])
        self.assertIsNone(d['compositor']['frame_index'])

    def test_property_error_is_explicit(self):
        d = self.run_observe('status', mode='property_error')
        self.assertIsNone(d['hmd']['model'])
        self.assertNotEqual(d['hmd']['property_errors']['model'], 0)

    def test_setting_reads_back_and_is_not_restored(self):
        for arg, value in [('false', False), ('true', True), ('0', False), ('1', True)]:
            self.log.unlink(missing_ok=True)
            d = self.run_observe('setting', arg)
            self.assertIs(d['pauseCompositorOnStandby'], value)
            calls = self.calls()
            self.assertEqual(sum(x.startswith('set_') for x in calls), 1)
            self.assertLess(calls.index('set_true' if value else 'set_false'), calls.index('get_bool'))

    def test_settings_errors_and_mismatch_fail(self):
        for mode in ('write_error', 'read_error', 'mismatch'):
            self.run_observe('setting', 'false', ok=False, mode=mode)
        self.run_observe('status', ok=False, mode='read_error')

    def test_capture_refuses_existing_directory_file_and_symlink(self):
        for p in (self.path, self.path / 'file', self.path / 'link'):
            if p.name == 'file':
                p.write_text('keep')
            if p.name == 'link':
                p.symlink_to(self.path / 'missing')
            self.run_observe('capture', p, ok=False)
        self.assertEqual(self.calls(), [])
        self.assertEqual((self.path / 'file').read_text(), 'keep')

    def test_capture_waits_for_two_complete_pngs(self):
        start = time.monotonic()
        d = self.run_observe('capture', self.path / 'new')
        self.assertGreaterEqual(time.monotonic() - start, 0.45)
        self.assertEqual(d['screenshot_handle'], 77)
        for key in ('preview', 'stereo'):
            p = Path(d[key])
            self.assertEqual(p, self.path / 'new' / (key + '.png'))
            self.assertEqual(p.read_bytes(), png())
        self.assertEqual(self.calls().count('request_screenshot'), 1)
        self.assertLess(self.calls().index('pngs_complete'), self.calls().index('shutdown_enter'))
        self.assertFalse(any(x.startswith('set_') for x in self.calls()))

    def test_capture_rejects_incomplete_or_corrupt_png(self):
        self.env['FRAME_OBSERVE_TIMEOUT_SECONDS'] = '1'
        for mode in ('incomplete', 'corrupt'):
            self.run_observe('capture', self.path / mode, ok=False, mode=mode)

    def test_screenshot_api_error(self):
        self.run_observe('capture', self.path / 'new', ok=False, mode='screenshot_error')

    def test_hung_sdk_is_bounded_with_json_error(self):
        self.env['FRAME_OBSERVE_TIMEOUT_SECONDS'] = '1'
        self.run_observe('status', ok=False, mode='hang')

    def test_timeout_setting_is_validated_before_sdk(self):
        for val in ('0', '-1', '3x', '121'):
            self.env['FRAME_OBSERVE_TIMEOUT_SECONDS'] = val
            self.run_observe('status', ok=False)
        self.assertEqual(self.calls(), [])

    def test_debug_uses_public_interface(self):
        d = self.run_observe('debug', '3', 'test request')
        self.assertEqual(d['device_index'], 3)
        self.assertEqual(d['response'], 'response\n"ok"')
        self.assertEqual(self.calls().count('debug'), 1)
        self.run_observe('debug', '3', 'test request', ok=False, mode='debug_large')


if __name__ == '__main__':
    unittest.main()
