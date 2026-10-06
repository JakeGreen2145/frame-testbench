#include "input_state.hpp"
#include <cmath>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <locale>
using namespace vr;
namespace frame {
namespace {
void number(std::ostream &o, double v) { if (std::isfinite(v)) o << v; else o << "null"; }
void transform_json(std::ostream &o, const double *v, const HmdQuaternion_t &q) {
    o << "{\"position\":[";
    for (int i = 0; i < 3; ++i) { if (i) o << ','; number(o, v[i]); }
    o << "],\"quaternion\":[";
    number(o, q.w); o << ','; number(o, q.x); o << ','; number(o, q.y); o << ','; number(o, q.z);
    o << "]}";
}
void pose_json(std::ostream &o, const std::optional<DriverPose_t> &p) {
    if (!p) { o << "null"; return; }
    o << "{\"position\":[";
    for (int i = 0; i < 3; ++i) { if (i) o << ','; number(o, p->vecPosition[i]); }
    o << "],\"quaternion\":[";
    number(o, p->qRotation.w); o << ','; number(o, p->qRotation.x); o << ',';
    number(o, p->qRotation.y); o << ','; number(o, p->qRotation.z);
    o << "],\"valid\":" << p->poseIsValid << ",\"connected\":" << p->deviceIsConnected;
    o << ",\"world_from_driver\":"; transform_json(o, p->vecWorldFromDriverTranslation, p->qWorldFromDriverRotation);
    o << ",\"driver_from_head\":"; transform_json(o, p->vecDriverFromHeadTranslation, p->qDriverFromHeadRotation);
    o << '}';
}
}
void InputState::reset(IVRServerDriverHost *h, IVRDriverInput *i) {
    std::lock_guard<std::mutex> lock(mutex_);
    host_ = h; input_ = i; index_ = k_unTrackedDeviceIndexInvalid; container_ = 0;
    physical_.reset(); requested_.reset(); worn_.reset(); proximity_.clear(); sequence_ = 0;
}
void InputState::activate(uint32_t i, PropertyContainerHandle_t c) {
    std::lock_guard<std::mutex> lock(mutex_);
    index_ = i; container_ = c; physical_.reset(); proximity_.clear(); ++sequence_;
}
void InputState::deactivate(uint32_t i) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (index_ != i) return;
    index_ = k_unTrackedDeviceIndexInvalid; container_ = 0; proximity_.clear(); physical_.reset(); ++sequence_;
}
void InputState::component(PropertyContainerHandle_t c, const char *n, VRInputComponentHandle_t h) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (container_ && c == container_ && n && !strcmp(n, "/proximity")) {
        proximity_.emplace(h, Proximity{}); ++sequence_;
    }
}
void InputState::physical_pose(uint32_t i, const DriverPose_t &p, uint32_t size) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (i == index_) {
        if (size == sizeof(p)) physical_ = p;
        if (requested_) return;
    }
    host_->TrackedDevicePoseUpdated(i, p, size);
}
EVRInputError InputState::physical_boolean(VRInputComponentHandle_t h, bool value, double offset) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = proximity_.find(h);
    if (it != proximity_.end()) {
        it->second.physical = value; it->second.offset = offset;
        if (worn_) return VRInputError_None;
    }
    return input_->UpdateBooleanComponent(h, value, offset);
}
void InputState::publish_locked() {
    if (index_ == k_unTrackedDeviceIndexInvalid) return;
    if (requested_ && host_) host_->TrackedDevicePoseUpdated(index_, *requested_, sizeof(DriverPose_t));
    if (worn_ && input_) for (const auto &entry : proximity_) input_->UpdateBooleanComponent(entry.first, *worn_, 0);
}
void InputState::tick() { std::lock_guard<std::mutex> lock(mutex_); publish_locked(); }
std::string InputState::status_locked(bool ok, const char *error) {
    std::ostringstream o; o.imbue(std::locale::classic()); o << std::setprecision(17) << std::boolalpha;
    o << "{\"ok\":" << ok << ",\"sequence\":" << sequence_ << ",\"hmd_index\":";
    if (index_ == k_unTrackedDeviceIndexInvalid) o << "null"; else o << index_;
    o << ",\"hmd_container\":" << container_ << ",\"proximity_ready\":" << !proximity_.empty()
      << ",\"proximity_handles\":[";
    bool first = true;
    for (const auto &entry : proximity_) { if (!first) o << ','; o << entry.first; first = false; }
    o << "],\"pose_override\":" << bool(requested_) << ",\"worn_override\":";
    if (worn_) o << *worn_; else o << "null";
    o << ",\"pose\":"; pose_json(o, requested_ ? requested_ : physical_);
    o << ",\"physical_pose\":"; pose_json(o, physical_);
    o << ",\"physical_worn\":";
    if (!proximity_.empty() && proximity_.begin()->second.physical) o << *proximity_.begin()->second.physical;
    else o << "null";
    o << ",\"requested_pose\":";
    if (!requested_) o << "null";
    else {
        const auto &p = *requested_;
        o << '[' << p.vecPosition[0] << ',' << p.vecPosition[1] << ',' << p.vecPosition[2] << ','
          << p.qRotation.w << ',' << p.qRotation.x << ',' << p.qRotation.y << ',' << p.qRotation.z << ']';
    }
    if (error) o << ",\"error\":\"" << error << '\"'; // Internal fixed strings, never unescaped user data.
    o << "}\n";
    return o.str();
}
std::string InputState::command(const std::string &line) {
    std::lock_guard<std::mutex> lock(mutex_);
    std::istringstream in(line); in.imbue(std::locale::classic());
    std::string operation, extra;
    in >> operation;
    if (operation == "status") {
        if (in >> extra) return status_locked(false, "status takes no arguments");
        return status_locked(true, nullptr);
    }
    if (operation == "pose") {
        double values[7];
        for (auto &v : values) if (!(in >> v) || !std::isfinite(v)) return status_locked(false, "pose needs seven finite numbers");
        if (in >> extra) return status_locked(false, "pose needs seven finite numbers");
        double scale = 0;
        for (int i = 3; i < 7; ++i) scale = std::fmax(scale, std::fabs(values[i]));
        if (scale == 0) return status_locked(false, "quaternion must be nonzero");
        double norm = 0;
        for (int i = 3; i < 7; ++i) { values[i] /= scale; norm += values[i] * values[i]; }
        norm = std::sqrt(norm);
        if (!host_ || index_ == k_unTrackedDeviceIndexInvalid) return status_locked(false, "HMD is not active");
        DriverPose_t p{};
        for (int i = 0; i < 3; ++i) p.vecPosition[i] = values[i];
        p.qRotation = {values[3] / norm, values[4] / norm, values[5] / norm, values[6] / norm};
        p.qWorldFromDriverRotation.w = p.qDriverFromHeadRotation.w = 1;
        p.poseIsValid = p.deviceIsConnected = true;
        p.result = TrackingResult_Running_OK;
        requested_ = p;
    } else if (operation == "worn") {
        std::string value;
        if (!(in >> value) || (value != "0" && value != "1") || (in >> extra))
            return status_locked(false, "worn needs 0 or 1");
        if (!input_ || proximity_.empty()) return status_locked(false, "HMD proximity is not ready");
        worn_ = value == "1";
    } else if (operation == "release" || operation == "pose-release" || operation == "worn-release") {
        if (in >> extra) return status_locked(false, "release takes no arguments");
        if (operation != "worn-release") {
            if (requested_ && physical_ && host_ && index_ != k_unTrackedDeviceIndexInvalid)
                host_->TrackedDevicePoseUpdated(index_, *physical_, sizeof(DriverPose_t));
            requested_.reset();
        }
        if (operation != "pose-release") {
            if (worn_ && input_) for (const auto &entry : proximity_) if (entry.second.physical)
                input_->UpdateBooleanComponent(entry.first, *entry.second.physical, entry.second.offset);
            worn_.reset();
        }
    } else return status_locked(false, "invalid command");
    ++sequence_;
    publish_locked();
    return status_locked(true, nullptr);
}
} // namespace frame
