#include "openvr_driver.h"
#include <cassert>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <iostream>
#include <mutex>
#include <string>
using namespace vr;
namespace {
std::mutex mu;
ITrackedDeviceServerDriver *hmd, *controller;
DriverPose_t latest{};
double other_x = 0;
bool worn = false, other_worn = true;
unsigned pose_count = 0, worn_count = 0, host_mask = 0, input_mask = 0;
void *other_interface;
class Host : public IVRServerDriverHost {
public:
    bool TrackedDeviceAdded(const char *s, ETrackedDeviceClass c, ITrackedDeviceServerDriver *d) override {
        if (c == TrackedDeviceClass_HMD) { assert(!strcmp(s, "cv-real-hmd")); hmd = d; assert(d->Activate(7) == VRInitError_None); }
        else { assert(c == TrackedDeviceClass_Controller && !strcmp(s, "cv-controller")); controller = d; }
        return true;
    }
    void TrackedDevicePoseUpdated(uint32_t i, const DriverPose_t &p, uint32_t size) override {
        assert(size == sizeof(p)); std::lock_guard<std::mutex> lock(mu);
        if (i == 7) { latest = p; ++pose_count; } else { assert(i == 9); other_x = p.vecPosition[0]; }
    }
    void VsyncEvent(double t) override { assert(t == 0.125); host_mask |= 1; }
    void VendorSpecificEvent(uint32_t i, EVREventType t, const VREvent_Data_t &d, double time) override {
        assert(i == 9 && t == VREvent_VendorSpecific_Reserved_Start && d.reserved.reserved0 == 123 && time == 0.25); host_mask |= 2;
    }
    bool IsExiting() override { host_mask |= 4; return true; }
    bool PollNextEvent(VREvent_t *e, uint32_t s) override { assert(e && s == sizeof(*e)); e->eventType = 123; host_mask |= 8; return true; }
    void GetRawTrackedDevicePoses(float t, TrackedDevicePose_t *p, uint32_t n) override { assert(t == 0.5f && n == 1); p->bPoseIsValid = true; host_mask |= 16; }
    void RequestRestart(const char *r, const char *e, const char *a, const char *c) override { assert(!strcmp(r,"reason") && !strcmp(e,"exe") && !strcmp(a,"args") && !strcmp(c,"cwd")); host_mask |= 32; }
    uint32_t GetFrameTimings(Compositor_FrameTiming *t, uint32_t n) override { assert(t && n == 1); host_mask |= 64; return 123; }
    void SetDisplayEyeToHead(uint32_t i, const HmdMatrix34_t &l, const HmdMatrix34_t &r) override { assert(i == 7 && l.m[0][0] == 2 && r.m[0][0] == 3); host_mask |= 128; }
    void SetDisplayProjectionRaw(uint32_t i, const HmdRect2_t &l, const HmdRect2_t &r) override { assert(i == 7 && l.vTopLeft.v[0] == 2 && r.vTopLeft.v[0] == 3); host_mask |= 256; }
    void SetRecommendedRenderTargetSize(uint32_t i, uint32_t w, uint32_t h) override { assert(i == 7 && w == 123 && h == 456); host_mask |= 512; }
} host;
class Input : public IVRDriverInput {
    int prox_count = 0;
public:
    EVRInputError CreateBooleanComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h) override {
        if (c == 107 && !strcmp(n,"/proximity")) *h = ++prox_count + 500;
        else if (c == 109 && !strcmp(n,"/proximity")) *h = 503;
        else { assert(c == 107 && !strcmp(n,"/input/button/click")); *h = 504; }
        return VRInputError_None;
    }
    EVRInputError UpdateBooleanComponent(VRInputComponentHandle_t h, bool v, double t) override {
        assert(h >= 501 && h <= 504); assert(t == -0.125 || t == 0);
        std::lock_guard<std::mutex> lock(mu);
        if (h == 501 || h == 502) { worn = v; ++worn_count; } else { other_worn = v; }
        return VRInputError_None;
    }
    EVRInputError CreateScalarComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h, EVRScalarType t, EVRScalarUnits u) override { assert(c == 109 && !strcmp(n,"scalar") && t == VRScalarType_Absolute && u == VRScalarUnits_NormalizedOneSided); *h = 601; input_mask |= 1; return VRInputError_None; }
    EVRInputError UpdateScalarComponent(VRInputComponentHandle_t h, float v, double t) override { assert(h == 601 && v == 0.5f && t == 0.125); input_mask |= 2; return VRInputError_InvalidParam; }
    EVRInputError CreateHapticComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h) override { assert(c == 109 && !strcmp(n,"haptic")); *h = 602; input_mask |= 4; return VRInputError_None; }
    EVRInputError CreateSkeletonComponent(PropertyContainerHandle_t c, const char *n, const char *s, const char *b, EVRSkeletalTrackingLevel l, const VRBoneTransform_t *g, uint32_t count, VRInputComponentHandle_t *h) override { assert(c == 109 && !strcmp(n,"skeleton") && !strcmp(s,"path") && !strcmp(b,"base") && l == VRSkeletalTracking_Full && g && count == 1); *h = 603; input_mask |= 8; return VRInputError_None; }
    EVRInputError UpdateSkeletonComponent(VRInputComponentHandle_t h, EVRSkeletalMotionRange r, const VRBoneTransform_t *p, uint32_t n) override { assert(h == 603 && r == VRSkeletalMotionRange_WithController && p && n == 1); input_mask |= 16; return VRInputError_InvalidParam; }
    EVRInputError CreatePoseComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h) override { assert(c == 109 && !strcmp(n,"pose")); *h = 604; input_mask |= 32; return VRInputError_None; }
    EVRInputError UpdatePoseComponent(VRInputComponentHandle_t h, const HmdMatrix34_t *p, double t) override { assert(h == 604 && p->m[0][0] == 2 && t == 0.125); input_mask |= 64; return VRInputError_InvalidParam; }
    EVRInputError CreateEyeTrackingComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h) override { assert(c == 109 && !strcmp(n,"eye")); *h = 605; input_mask |= 128; return VRInputError_None; }
    EVRInputError UpdateEyeTrackingComponent(VRInputComponentHandle_t h, const VREyeTrackingData_t *p, double t) override { assert(h == 605 && p && t == 0.125); input_mask |= 256; return VRInputError_InvalidParam; }
} input;
class Properties : public IVRProperties {
public:
    ETrackedPropertyError ReadPropertyBatch(PropertyContainerHandle_t, PropertyRead_t *, uint32_t) override { return TrackedProp_Success; }
    ETrackedPropertyError WritePropertyBatch(PropertyContainerHandle_t, PropertyWrite_t *, uint32_t) override { return TrackedProp_Success; }
    const char *GetPropErrorNameFromEnum(ETrackedPropertyError) override { return "success"; }
    PropertyContainerHandle_t TrackedDeviceToPropertyContainer(TrackedDeviceIndex_t i) override { return 100 + i; }
} properties;
class Context : public IVRDriverContext {
public:
    void *GetGenericInterface(const char *n, EVRInitError *e = nullptr) override {
        if (e) *e = VRInitError_None;
        if (!strcmp(n,IVRServerDriverHost_Version)) return &host;
        if (!strcmp(n,IVRDriverInput_Version)) return &input;
        if (!strcmp(n,IVRProperties_Version)) return &properties;
        if (e) *e = VRInitError_Init_InterfaceNotFound;
        return other_interface;
    }
    DriverHandle_t GetDriverHandle() override { return 4321; }
} context;
void metrics() {
    std::lock_guard<std::mutex> lock(mu);
    bool raw = latest.qWorldFromDriverRotation.w == 1 && latest.qDriverFromHeadRotation.w == 1 && latest.poseTimeOffset == 0;
    for (int k = 0; k < 3; ++k) raw = raw && latest.vecWorldFromDriverTranslation[k] == 0 && latest.vecDriverFromHeadTranslation[k] == 0 && latest.vecVelocity[k] == 0 && latest.vecAngularVelocity[k] == 0 && latest.vecAcceleration[k] == 0 && latest.vecAngularAcceleration[k] == 0;
    std::cout << "{\"hmd_pose\":[" << latest.vecPosition[0] << ',' << latest.vecPosition[1] << ',' << latest.vecPosition[2] << ',' << latest.qRotation.w << ',' << latest.qRotation.x << ',' << latest.qRotation.y << ',' << latest.qRotation.z << "],\"raw_transform\":" << raw << ",\"valid\":" << (latest.poseIsValid && latest.deviceIsConnected && latest.result == TrackingResult_Running_OK) << ",\"other_x\":" << other_x << ",\"pose_count\":" << pose_count << ",\"worn_count\":" << worn_count << ",\"worn\":" << worn << ",\"other_worn\":" << other_worn << "}" << std::endl;
}
}
int main(int argc, char **) {
    std::cout << std::boolalpha;
    auto path = std::getenv("FRAME_TESTBENCH_REAL_DRIVER"); assert(path);
    if (argc > 1) {
        // Relative paths and alternate absolute spellings must not redirect.
        std::string sibling(path); sibling.insert(sibling.find_last_of('/') + 1, "./");
        auto plain = dlopen(sibling.c_str(), RTLD_NOW | RTLD_LOCAL);
        assert(plain && dlsym(plain, "FrameFakePhysical")); dlclose(plain);
        auto self = dlopen(nullptr, RTLD_NOW); assert(self); dlclose(self);
        assert(!dlopen("/no-such-other-driver.so", RTLD_NOW));
        std::cout << "scope-ok" << std::endl; return 0;
    }
    auto module = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!module) { std::cerr << dlerror() << '\n'; return 2; }
    using Factory = void *(*)(const char *, int *);
    auto factory = reinterpret_cast<Factory>(dlsym(module,"HmdDriverFactory")); assert(factory);
    int code = -1;
    auto provider = static_cast<IServerTrackedDeviceProvider *>(factory(IServerTrackedDeviceProvider_Version, &code));
    assert(provider && code == 0);
    // NOLOAD via the interposer's explicit bypass must reach the real module.
    auto real_open = reinterpret_cast<void *(*)(const char *, int)>(dlsym(RTLD_DEFAULT,"frame_real_dlopen"));
    auto real = real_open ? real_open(path, RTLD_NOW | RTLD_NOLOAD) : dlopen(path, RTLD_NOW | RTLD_NOLOAD);
    assert(real);
    auto physical = reinterpret_cast<void (*)()>(dlsym(real,"FrameFakePhysical")); assert(physical);
    auto lifecycle = reinterpret_cast<unsigned (*)()>(dlsym(real,"FrameFakeLifecycle")); assert(lifecycle);
    auto get_controller = reinterpret_cast<void *(*)()>(dlsym(real,"FrameFakeController"));
    auto get_component = reinterpret_cast<void *(*)()>(dlsym(real,"FrameFakeComponent"));
    auto get_other = reinterpret_cast<void *(*)()>(dlsym(real,"FrameFakeOther"));
    other_interface = get_other();
    if (provider->Init(&context) != VRInitError_None) return 3;
    physical();
    bool cleaned = false;
    std::cout << "ready" << std::endl;
    std::string cmd;
    while (std::getline(std::cin, cmd)) {
        if (cmd == "quit") break;
        if (cmd == "physical") { physical(); metrics(); }
        else if (cmd == "deactivate") { hmd->Deactivate(); std::cout << "{\"deactivated\":" << ((lifecycle() & 2) != 0) << "}" << std::endl; }
        else if (cmd == "metrics") metrics();
        else if (cmd == "cleanup") { provider->Cleanup(); cleaned = true; std::cout << "{\"cleaned\":" << ((lifecycle() & 16) != 0) << "}" << std::endl; }
        else if (cmd == "forward") {
            provider->RunFrame(); provider->EnterStandby(); provider->LeaveStandby(); assert(provider->ShouldBlockStandbyMode());
            assert(!strcmp(provider->GetInterfaceVersions()[0], k_InterfaceVersions[0]));
            hmd->EnterStandby(); char out[20]; hmd->DebugRequest("debug-test",out,sizeof(out)); assert(!strcmp(out,"debug-ok")); assert(hmd->GetPose().vecPosition[0] == 17);
            bool component_ok = hmd->GetComponent("display-test") == get_component();
            bool factory_ok = factory("other-factory", &code) == get_other() && code == 123;
            std::cout << "{\"all_forwarded\":" << (host_mask == 1023 && input_mask == 511 && (lifecycle() & 493) == 493) << ",\"component_identity\":" << component_ok << ",\"controller_identity\":" << (controller == get_controller()) << ",\"factory_identity\":" << factory_ok << "}" << std::endl;
        }
    }
    if (!cleaned) { hmd->Deactivate(); provider->Cleanup(); }
    assert((lifecycle() & 16) != 0);
    dlclose(real); dlclose(module);
}
