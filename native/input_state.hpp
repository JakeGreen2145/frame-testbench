#pragma once
#include "openvr_driver.h"
#include <atomic>
#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

namespace frame {
// Runtime calls and snapshot/override changes share one lock. In particular a
// publication copied before release can never land after release's restoration.
class InputState {
    std::mutex mutex_;
    vr::IVRServerDriverHost *host_ = nullptr;
    vr::IVRDriverInput *input_ = nullptr;
    vr::TrackedDeviceIndex_t index_ = vr::k_unTrackedDeviceIndexInvalid;
    vr::PropertyContainerHandle_t container_ = 0;
    std::optional<vr::DriverPose_t> physical_, requested_;
    struct Proximity { std::optional<bool> physical; double offset = 0; };
    std::map<vr::VRInputComponentHandle_t, Proximity> proximity_;
    std::optional<bool> worn_;
    uint64_t sequence_ = 0;
    void publish_locked();
    std::string status_locked(bool ok, const char *error);
public:
    void reset(vr::IVRServerDriverHost *, vr::IVRDriverInput *);
    void activate(uint32_t, vr::PropertyContainerHandle_t);
    void deactivate(uint32_t);
    void component(vr::PropertyContainerHandle_t, const char *, vr::VRInputComponentHandle_t);
    void physical_pose(uint32_t, const vr::DriverPose_t &, uint32_t);
    vr::EVRInputError physical_boolean(vr::VRInputComponentHandle_t, bool, double);
    void tick();
    std::string command(const std::string &);
};

class Control {
    InputState &state_;
    std::atomic<bool> stop_{false};
    std::thread worker_;
    int listener_ = -1;
    std::string path_;
    unsigned long device_ = 0, inode_ = 0;
    void run();
public:
    explicit Control(InputState &state) : state_(state) {}
    ~Control();
    bool start();
    void stop();
};
} // namespace frame
