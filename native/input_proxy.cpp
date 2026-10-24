#include "input_state.hpp"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <memory>
#include <vector>
using namespace vr;
extern "C" __attribute__((visibility("default"))) void *HmdDriverFactory(const char *, int *);
namespace frame {
template<class F> void observe(F &&function) noexcept { try { function(); } catch (...) {} }
class ControllerProxy final : public ITrackedDeviceServerDriver {
    ITrackedDeviceServerDriver *real_;
    IVRProperties *properties_;
    ControllerTrace &trace_;
    uint64_t id_;
public:
    ControllerProxy(ITrackedDeviceServerDriver *real, IVRProperties *properties, ControllerTrace &trace, uint64_t id)
        : real_(real), properties_(properties), trace_(trace), id_(id) {}
    EVRInitError Activate(uint32_t index) override {
        PropertyContainerHandle_t container=0;
        observe([&] { if(properties_) container=properties_->TrackedDeviceToPropertyContainer(index); trace_.activating(id_,index,container); });
        const auto result=real_->Activate(index);
        observe([&] { trace_.activated(id_,result); trace_.refresh(properties_); });
        return result;
    }
    void Deactivate() override { observe([&] { trace_.deactivated(id_); }); real_->Deactivate(); }
    void EnterStandby() override { real_->EnterStandby(); }
    void *GetComponent(const char *name) override { return real_->GetComponent(name); }
    void DebugRequest(const char *request,char *response,uint32_t size) override { real_->DebugRequest(request,response,size); }
    DriverPose_t GetPose() override { const auto pose=real_->GetPose(); observe([&] { trace_.get_pose(id_,pose); }); return pose; }
};
class Device final : public ITrackedDeviceServerDriver {
    ITrackedDeviceServerDriver *real_;
    IVRProperties *properties_;
    InputState &state_;
    uint32_t index_ = k_unTrackedDeviceIndexInvalid;
public:
    Device(ITrackedDeviceServerDriver *real, IVRProperties *properties, InputState &state)
        : real_(real), properties_(properties), state_(state) {}
    EVRInitError Activate(uint32_t index) override {
        index_ = index;
        // Activate creates /proximity synchronously; capture its container first.
        state_.activate(index, properties_ ? properties_->TrackedDeviceToPropertyContainer(index) : 0);
        const auto result = real_->Activate(index);
        if (result != VRInitError_None) state_.deactivate(index);
        return result;
    }
    void Deactivate() override { state_.deactivate(index_); real_->Deactivate(); index_ = k_unTrackedDeviceIndexInvalid; }
    void EnterStandby() override { real_->EnterStandby(); }
    void *GetComponent(const char *name) override { return real_->GetComponent(name); }
    void DebugRequest(const char *request, char *response, uint32_t size) override { real_->DebugRequest(request, response, size); }
    DriverPose_t GetPose() override { return real_->GetPose(); }
};

class Host final : public IVRServerDriverHost {
    InputState &state_;
    std::mutex devices_mutex_;
    std::vector<std::unique_ptr<Device>> devices_;
    std::vector<std::unique_ptr<ControllerProxy>> controllers_;
public:
    IVRServerDriverHost *real = nullptr;
    IVRProperties *properties = nullptr;
    explicit Host(InputState &s) : state_(s) {}
    void clear() { std::lock_guard<std::mutex> lock(devices_mutex_); devices_.clear(); controllers_.clear(); }
    bool TrackedDeviceAdded(const char *serial, ETrackedDeviceClass type, ITrackedDeviceServerDriver *driver) override {
        if (type == TrackedDeviceClass_Controller && driver && state_.trace().enabled()) {
            uint64_t id=0;
            observe([&] { id=state_.trace().added(serial); });
            if(id) {
                ControllerProxy *pointer=nullptr;
                observe([&] {
                    auto wrapped=std::make_unique<ControllerProxy>(driver,properties,state_.trace(),id);
                    auto *candidate=wrapped.get();
                    std::lock_guard<std::mutex> lock(devices_mutex_); controllers_.push_back(std::move(wrapped));
                    pointer=candidate;
                });
                if(pointer) {
                    const bool accepted=real->TrackedDeviceAdded(serial,type,pointer);
                    observe([&] { state_.trace().registered(id,accepted); });
                    return accepted;
                }
            }
            return real->TrackedDeviceAdded(serial,type,driver);
        }
        if (type != TrackedDeviceClass_HMD || !driver) return real->TrackedDeviceAdded(serial, type, driver);
        auto wrapped = std::make_unique<Device>(driver, properties, state_);
        auto *pointer = wrapped.get();
        { std::lock_guard<std::mutex> lock(devices_mutex_); devices_.push_back(std::move(wrapped)); }
        return real->TrackedDeviceAdded(serial, type, pointer);
    }
    void TrackedDevicePoseUpdated(uint32_t i, const DriverPose_t &p, uint32_t size) override {
        observe([&] { state_.trace().publication(i,p,size); }); state_.physical_pose(i, p, size);
    }
    void VsyncEvent(double t) override { real->VsyncEvent(t); }
    void VendorSpecificEvent(uint32_t i, EVREventType t, const VREvent_Data_t &d, double time) override { real->VendorSpecificEvent(i, t, d, time); }
    bool IsExiting() override { return real->IsExiting(); }
    bool PollNextEvent(VREvent_t *e, uint32_t s) override { return real->PollNextEvent(e, s); }
    void GetRawTrackedDevicePoses(float t, TrackedDevicePose_t *p, uint32_t n) override { real->GetRawTrackedDevicePoses(t, p, n); }
    void RequestRestart(const char *r, const char *e, const char *a, const char *c) override { real->RequestRestart(r, e, a, c); }
    uint32_t GetFrameTimings(Compositor_FrameTiming *t, uint32_t n) override { return real->GetFrameTimings(t, n); }
    void SetDisplayEyeToHead(uint32_t i, const HmdMatrix34_t &l, const HmdMatrix34_t &r) override { real->SetDisplayEyeToHead(i, l, r); }
    void SetDisplayProjectionRaw(uint32_t i, const HmdRect2_t &l, const HmdRect2_t &r) override { real->SetDisplayProjectionRaw(i, l, r); }
    void SetRecommendedRenderTargetSize(uint32_t i, uint32_t w, uint32_t h) override { real->SetRecommendedRenderTargetSize(i, w, h); }
};

class Input final : public IVRDriverInput {
    InputState &state_;
public:
    IVRDriverInput *real = nullptr;
    explicit Input(InputState &s) : state_(s) {}
    EVRInputError CreateBooleanComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h) override {
        const auto result = real->CreateBooleanComponent(c, n, h);
        observe([&] { state_.trace().component(c,n,result==VRInputError_None && h ? *h : 0,true,0,0,result); });
        if (result == VRInputError_None && h && *h != k_ulInvalidInputComponentHandle) state_.component(c, n, *h);
        return result;
    }
    EVRInputError UpdateBooleanComponent(VRInputComponentHandle_t h, bool value, double offset) override {
        const auto result=state_.physical_boolean(h,value,offset); observe([&] { state_.trace().update(h,true,result); }); return result;
    }
    EVRInputError CreateScalarComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h, EVRScalarType t, EVRScalarUnits u) override {
        const auto result=real->CreateScalarComponent(c,n,h,t,u);
        observe([&] { state_.trace().component(c,n,result==VRInputError_None && h ? *h : 0,false,t,u,result); }); return result;
    }
    EVRInputError UpdateScalarComponent(VRInputComponentHandle_t h, float value, double offset) override {
        const auto result=real->UpdateScalarComponent(h,value,offset); observe([&] { state_.trace().update(h,false,result); }); return result;
    }
    EVRInputError CreateHapticComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h) override { return real->CreateHapticComponent(c, n, h); }
    EVRInputError CreateSkeletonComponent(PropertyContainerHandle_t c, const char *n, const char *p, const char *b, EVRSkeletalTrackingLevel l, const VRBoneTransform_t *g, uint32_t count, VRInputComponentHandle_t *h) override { return real->CreateSkeletonComponent(c, n, p, b, l, g, count, h); }
    EVRInputError UpdateSkeletonComponent(VRInputComponentHandle_t h, EVRSkeletalMotionRange r, const VRBoneTransform_t *p, uint32_t n) override { return real->UpdateSkeletonComponent(h, r, p, n); }
    EVRInputError CreatePoseComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h) override { return real->CreatePoseComponent(c, n, h); }
    EVRInputError UpdatePoseComponent(VRInputComponentHandle_t h, const HmdMatrix34_t *p, double t) override { return real->UpdatePoseComponent(h, p, t); }
    EVRInputError CreateEyeTrackingComponent(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t *h) override { return real->CreateEyeTrackingComponent(c, n, h); }
    EVRInputError UpdateEyeTrackingComponent(VRInputComponentHandle_t h, const VREyeTrackingData_t *p, double t) override { return real->UpdateEyeTrackingComponent(h, p, t); }
};

class Context final : public IVRDriverContext {
public:
    IVRDriverContext *real = nullptr;
    Host host;
    Input input;
    InputState &state;
    explicit Context(InputState &s) : host(s), input(s), state(s) {}
    void *GetGenericInterface(const char *name, EVRInitError *error = nullptr) override {
        void *result = real->GetGenericInterface(name, error);
        if (!result || !name) return result;
        observe([&] {
            const bool alias=(result==host.real || result==input.real) && strcmp(name,IVRServerDriverHost_Version) && strcmp(name,IVRDriverInput_Version);
            state.trace().interface_request(name,alias);
        });
        if (!strcmp(name, IVRServerDriverHost_Version) && result == host.real) return &host;
        if (!strcmp(name, IVRDriverInput_Version) && result == input.real) return &input;
        return result;
    }
    DriverHandle_t GetDriverHandle() override { return real->GetDriverHandle(); }
};
// Provider-owned synthetic devices never wrap or take ownership of physical controllers.
class Controller final : public ITrackedDeviceServerDriver {
    InputState &state_;
    unsigned hand_;
public:
    IVRProperties *properties = nullptr;
    IVRDriverInput *input = nullptr;
    Controller(InputState &state, unsigned hand) : state_(state), hand_(hand) {}
    const char *serial() const { return hand_ == 0 ? "frame_testbench_left" : "frame_testbench_right"; }
    EVRInitError Activate(uint32_t index) override {
        if (!properties || !input || index == k_unTrackedDeviceIndexInvalid) return VRInitError_Driver_Failed;
        CVRPropertyHelpers props(properties);
        const auto container = props.TrackedDeviceToPropertyContainer(index);
        if (!container) return VRInitError_Driver_Failed;
        if (props.SetStringProperty(container, Prop_SerialNumber_String, serial()) != TrackedProp_Success ||
            props.SetStringProperty(container, Prop_RenderModelName_String, "generic_controller") != TrackedProp_Success ||
            props.SetStringProperty(container, Prop_ControllerType_String, "frame_controller") != TrackedProp_Success ||
            props.SetStringProperty(container, Prop_InputProfilePath_String, "{frame_controller}/input/frame_controller_profile.json") != TrackedProp_Success ||
            props.SetStringProperty(container, Prop_ModelNumber_String, "Steam Frame Controller") != TrackedProp_Success ||
            props.SetStringProperty(container, Prop_ManufacturerName_String, "Valve") != TrackedProp_Success ||
            props.SetInt32Property(container, Prop_ControllerRoleHint_Int32,
                hand_ == 0 ? TrackedControllerRole_LeftHand : TrackedControllerRole_RightHand) != TrackedProp_Success)
            return VRInitError_Driver_Failed;
        auto components = controller_input_schema(hand_);
        for (auto &entry : components) {
            auto &c = entry.second;
            c.creation_error = c.boolean ? input->CreateBooleanComponent(container, entry.first.c_str(), &c.handle) :
                input->CreateScalarComponent(container, entry.first.c_str(), &c.handle, VRScalarType_Absolute,
                    c.two_sided ? VRScalarUnits_NormalizedTwoSided : VRScalarUnits_NormalizedOneSided);
            if (c.creation_error == VRInputError_None && c.handle == k_ulInvalidInputComponentHandle)
                c.creation_error = VRInputError_InvalidHandle;
            if (c.creation_error != VRInputError_None) c.handle = k_ulInvalidInputComponentHandle;
        }
        state_.controller_activate(hand_, index, std::move(components));
        return VRInitError_None;
    }
    void Deactivate() override { state_.controller_deactivate(hand_); }
    void EnterStandby() override {}
    void *GetComponent(const char *) override { return nullptr; }
    void DebugRequest(const char *, char *response, uint32_t size) override { if (response && size) response[0] = 0; }
    DriverPose_t GetPose() override { return state_.controller_pose(hand_); }
};

class Provider final : public IServerTrackedDeviceProvider {
    InputState state_;
    Context context_{state_};
    Control control_{state_};
    Controller left_{state_, 0}, right_{state_, 1};
public:
    IServerTrackedDeviceProvider *real = nullptr;
    EVRInitError Init(IVRDriverContext *context) override {
        context_.real = context;
        context_.host.real = static_cast<IVRServerDriverHost *>(context->GetGenericInterface(IVRServerDriverHost_Version));
        context_.input.real = static_cast<IVRDriverInput *>(context->GetGenericInterface(IVRDriverInput_Version));
        context_.host.properties = static_cast<IVRProperties *>(context->GetGenericInterface(IVRProperties_Version));
        state_.reset(context_.host.real, context_.input.real);
        state_.trace_properties(context_.host.properties);
        auto result = real->Init(&context_);
        if (result != VRInitError_None) return result;
        if (!control_.start()) {
            std::fprintf(stderr, "frame-input: could not create private control socket\n");
            real->Cleanup(); context_.host.clear();
            return VRInitError_Driver_Failed;
        }
        // The runtime may synchronously call Activate or GetPose during registration.
        // Never hold the state lock here. Device storage lasts as long as the provider.
        for (auto *controller : {&left_, &right_}) {
            controller->properties = context_.host.properties;
            controller->input = context_.input.real;
            if (context_.host.real && !context_.host.real->TrackedDeviceAdded(
                    controller->serial(), TrackedDeviceClass_Controller, controller))
                controller->Deactivate();
        }
        return VRInitError_None;
    }
    void Cleanup() override {
        control_.stop();
        state_.command("controller-release all");
        left_.Deactivate(); right_.Deactivate();
        left_.properties = right_.properties = nullptr;
        left_.input = right_.input = nullptr;
        real->Cleanup(); context_.host.clear();
        state_.reset(nullptr, nullptr);
        state_.trace_properties(nullptr);
    }
    const char *const *GetInterfaceVersions() override { return real->GetInterfaceVersions(); }
    void RunFrame() override { real->RunFrame(); }
    bool ShouldBlockStandbyMode() override { return real->ShouldBlockStandbyMode(); }
    void EnterStandby() override { real->EnterStandby(); }
    void LeaveStandby() override { real->LeaveStandby(); }
};
}
namespace {
using Factory = void *(*)(const char *, int *);
Factory real_factory = nullptr;
std::once_flag loaded;
std::mutex factory_mutex;
frame::Provider provider;
void load_real() {
    const char *path = std::getenv("FRAME_TESTBENCH_REAL_DRIVER");
    if (!path) path = "/opt/steamvr/drivers/cv/bin/linuxarm64/driver_cv.so";
    if (path[0] != '/') return;
    using Open = void *(*)(const char *, int);
    auto open = reinterpret_cast<Open>(dlsym(RTLD_DEFAULT, "frame_real_dlopen"));
    // Direct loading without LD_PRELOAD also works, useful for offline ABI tests.
    void *module = open ? open(path, RTLD_NOW | RTLD_LOCAL) : dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!module) { std::fprintf(stderr, "frame-input: real cv load failed: %s\n", dlerror()); return; }
    const auto factory = reinterpret_cast<Factory>(dlsym(module, "HmdDriverFactory"));
    if (factory && factory != &HmdDriverFactory) real_factory = factory;
    // Keep the real cv module loaded for its process lifetime. Unloading while
    // the runtime holds a display component or driver-owned thread is unsafe.
}
}
extern "C" __attribute__((visibility("default")))
void *HmdDriverFactory(const char *name, int *error) {
    std::call_once(loaded, load_real);
    if (!real_factory || !name) { if (error) *error = VRInitError_Init_InterfaceNotFound; return nullptr; }
    void *result = real_factory(name, error);
    if (!result || strcmp(name, IServerTrackedDeviceProvider_Version)) return result;
    std::lock_guard<std::mutex> lock(factory_mutex);
    provider.real = static_cast<IServerTrackedDeviceProvider *>(result);
    return &provider;
}
