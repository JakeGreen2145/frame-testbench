#include "sdk.hpp"
#include "controller_action_pins.hpp"
#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <fcntl.h>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <thread>
#include <unistd.h>

namespace {
int output_fd = STDOUT_FILENO;
constexpr unsigned max_samples = 300;
constexpr auto duration = std::chrono::seconds(15);
constexpr auto interval = std::chrono::milliseconds(50);
constexpr uint32_t device_limit = std::min<uint32_t>(64, vr::k_unMaxTrackedDeviceCount);
const char *stage = "validation";
void timed_out(int) {
    constexpr char message[] = "{\"event\":\"error\",\"schema_version\":1,\"error\":\"whole_process_deadline\",\"outcome\":\"unknown\",\"input_receipt\":\"unknown\"}\n";
    const auto ignored = write(output_fd, message, sizeof(message) - 1);
    (void)ignored;
    _exit(124);
}
std::string quote(const std::string &value) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 0x20) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c);
        else out << c;
    }
    out << '"';
    return out.str();
}
const char *boolean(bool value) { return value ? "true" : "false"; }
void number(std::ostream &out, float value) {
    if (std::isfinite(value)) out << std::setprecision(10) << value;
    else out << "null";
}
void emit(const std::string &value) {
    const auto line = value + '\n';
    size_t offset = 0;
    while (offset < line.size()) {
        auto size = write(output_fd, line.data() + offset, line.size() - offset);
        if (size < 0 && errno == EINTR) continue;
        if (size <= 0) throw std::runtime_error("output_unavailable");
        offset += size_t(size);
    }
}
std::string normalized_file(const std::filesystem::path &path) {
    if (std::filesystem::is_symlink(path) || !std::filesystem::is_regular_file(path)
        || std::filesystem::file_size(path) > 65536) throw std::runtime_error("invalid_action_resource");
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("invalid_action_resource");
    std::string value;
    char c;
    while (input.get(c)) {
        if (value.size() >= 65536) throw std::runtime_error("action_resource_limit");
        if (c == '\r' && input.peek() == '\n') continue;
        value += c;
    }
    if (!input.eof()) throw std::runtime_error("invalid_action_resource");
    return value;
}
void validate_manifest(const std::filesystem::path &path) {
    if (!path.is_absolute() || path.filename() != "input-actions.json") throw std::runtime_error("invalid_manifest_path");
    if (normalized_file(path) != pinned_manifest
        || normalized_file(path.parent_path() / "frame-controller-bindings.json") != pinned_bindings)
        throw std::runtime_error("action_resource_pin_mismatch");
}
struct Property {
    std::string value;
    vr::ETrackedPropertyError error;
    uint32_t required;
    std::string json() const {
        return std::string("{\"value\":") + (error == vr::TrackedProp_Success ? quote(value) : "null")
            + ",\"error\":" + std::to_string(error) + ",\"required_bytes\":" + std::to_string(required) + '}';
    }
};
Property serial(vr::IVRSystem *system, uint32_t index) {
    std::array<char, 256> buffer{};
    auto error = vr::TrackedProp_Success;
    auto required = system->GetStringTrackedDeviceProperty(index, vr::Prop_SerialNumber_String, buffer.data(), buffer.size(), &error);
    if (error == vr::TrackedProp_Success && (!required || required > buffer.size() || buffer[required - 1] != '\0'))
        error = vr::TrackedProp_BufferTooSmall;
    return {error == vr::TrackedProp_Success ? std::string(buffer.data(), required - 1) : "", error, required};
}
std::string pose(const vr::TrackedDevicePose_t &value) {
    std::ostringstream out;
    out << "{\"connected\":" << boolean(value.bDeviceIsConnected) << ",\"valid\":" << boolean(value.bPoseIsValid)
        << ",\"tracking_result\":" << value.eTrackingResult << ",\"matrix\":[";
    for (int row = 0; row < 3; ++row) {
        if (row) out << ',';
        out << '[';
        for (int col = 0; col < 4; ++col) { if (col) out << ','; number(out, value.mDeviceToAbsoluteTracking.m[row][col]); }
        out << ']';
    }
    out << "]}";
    return out.str();
}
std::string producer(vr::IVRSystem *system, const std::array<vr::TrackedDevicePose_t, device_limit> &poses) {
    unsigned matches = 0;
    std::string row;
    for (uint32_t index = 0; index < device_limit; ++index) {
        if (system->GetTrackedDeviceClass(index) != vr::TrackedDeviceClass_Controller) continue;
        const auto identity = serial(system, index);
        if (identity.error != vr::TrackedProp_Success || identity.value != "frame_testbench_right") continue;
        ++matches;
        row = "{\"device_index\":" + std::to_string(index) + ",\"serial\":" + identity.json()
            + ",\"actual_role\":" + std::to_string(system->GetControllerRoleForTrackedDeviceIndex(index))
            + ",\"standing_pose\":" + pose(poses[index]) + '}';
    }
    return "{\"match_count\":" + std::to_string(matches) + ",\"device\":" + (matches == 1 ? row : "null") + '}';
}
std::string origin(vr::IVRInput *input, vr::IVRSystem *system, vr::VRInputValueHandle_t handle) {
    vr::InputOriginInfo_t info{};
    info.trackedDeviceIndex = vr::k_unTrackedDeviceIndexInvalid;
    const auto error = input->GetOriginTrackedDeviceInfo(handle, &info, sizeof(info));
    const bool valid = error == vr::VRInputError_None && info.trackedDeviceIndex < device_limit;
    return "{\"handle\":" + quote(std::to_string(handle)) + ",\"error\":" + std::to_string(error)
        + ",\"device_index\":" + (valid ? std::to_string(info.trackedDeviceIndex) : "null")
        + ",\"device_path_handle\":" + (valid ? quote(std::to_string(info.devicePath)) : "null")
        + ",\"serial\":" + (valid ? serial(system, info.trackedDeviceIndex).json() : "null") + '}';
}
std::string application_key(vr::IVRApplications *apps, uint32_t pid) {
    std::array<char, 256> buffer;
    buffer.fill('\x7f');
    const auto error = apps->GetApplicationKeyByProcessId(pid, buffer.data(), buffer.size());
    const auto end = std::find(buffer.begin(), buffer.end(), '\0');
    const bool valid = error == vr::VRApplicationError_None && end != buffer.end() && end != buffer.begin();
    const auto value = valid ? quote(std::string(buffer.begin(), end)) : "null";
    return std::string("{\"value\":") + value + ",\"error\":" + std::to_string(error)
        + ",\"validation_error\":" + (error == vr::VRApplicationError_None && !valid ? quote("empty_or_unterminated") : "null") + '}';
}
void check(vr::EVRInputError error) {
    if (error != vr::VRInputError_None) throw std::runtime_error("input_setup_error_" + std::to_string(error));
}
unsigned observe(frame_observe::Sdk &sdk) {
    auto *system = sdk.get<vr::IVRSystem>(vr::IVRSystem_Version);
    auto *input = sdk.get<vr::IVRInput>(vr::IVRInput_Version);
    auto *overlay = sdk.get<vr::IVROverlay>(vr::IVROverlay_Version);
    auto *compositor = sdk.get<vr::IVRCompositor>(vr::IVRCompositor_Version);
    auto *apps = sdk.get<vr::IVRApplications>(vr::IVRApplications_Version);
    vr::VRActiveActionSet_t set{}; // Default priority, all-device set; never raises priority.
    check(input->GetActionSetHandle("/actions/observe", &set.ulActionSet));
    vr::VRActionHandle_t a = 0, axis = 0;
    vr::VRInputValueHandle_t right = 0;
    check(input->GetActionHandle("/actions/observe/in/right_a_click", &a));
    check(input->GetActionHandle("/actions/observe/in/right_thumbstick_position", &axis));
    check(input->GetInputSourceHandle("/user/hand/right", &right));
    if (!set.ulActionSet || !a || !axis || !right) throw std::runtime_error("invalid_action_handle");
    stage = "sampling";
    const auto start = std::chrono::steady_clock::now();
    const auto end = start + duration;
    unsigned count = 0;
    while (count < max_samples && std::chrono::steady_clock::now() < end) {
        const auto tick = std::chrono::steady_clock::now();
        const uint32_t scene_before = compositor->GetCurrentSceneFocusProcess();
        const bool dashboard_before = overlay->IsDashboardVisible();
        const bool available_before = system->IsInputAvailable();
        const auto key = application_key(apps, scene_before);
        const auto update = input->UpdateActionState(&set, sizeof(set), 1);
        vr::InputDigitalActionData_t digital{};
        vr::InputAnalogActionData_t analog{};
        auto a_error = update, axis_error = update;
        if (update == vr::VRInputError_None) {
            a_error = input->GetDigitalActionData(a, &digital, sizeof(digital), right);
            axis_error = input->GetAnalogActionData(axis, &analog, sizeof(analog), right);
        }
        const bool a_active = a_error == vr::VRInputError_None && digital.bActive;
        const bool axis_active = axis_error == vr::VRInputError_None && analog.bActive;
        std::array<vr::TrackedDevicePose_t, device_limit> poses{};
        system->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseStanding, 0, poses.data(), poses.size());
        const auto producer_value = producer(system, poses);
        const auto a_origin = a_active ? origin(input, system, digital.activeOrigin) : "null";
        const auto axis_origin = axis_active ? origin(input, system, analog.activeOrigin) : "null";
        const bool available = system->IsInputAvailable();
        const auto scene_after = compositor->GetCurrentSceneFocusProcess();
        const bool dashboard_after = overlay->IsDashboardVisible();
        const auto now = std::chrono::system_clock::now().time_since_epoch();
        std::ostringstream out;
        out << "{\"event\":\"sample\",\"schema_version\":1,\"observer_pid\":" << getpid() << ",\"sequence\":" << count
            << ",\"utc_unix_ms\":" << std::chrono::duration_cast<std::chrono::milliseconds>(now).count()
            << ",\"elapsed_ms\":" << std::chrono::duration_cast<std::chrono::milliseconds>(tick - start).count()
            << ",\"scene_pid_before\":" << scene_before << ",\"scene_pid_after\":" << scene_after
            << ",\"scene_app_key\":" << key << ",\"dashboard_before\":" << boolean(dashboard_before)
            << ",\"dashboard_after\":" << boolean(dashboard_after) << ",\"input_available_before\":" << boolean(available_before)
            << ",\"input_available\":" << boolean(available)
            << ",\"update_error\":" << update << ",\"producer\":" << producer_value
            << ",\"right_a\":{\"error\":" << a_error << ",\"active\":" << (a_error == vr::VRInputError_None ? boolean(digital.bActive) : "null")
            << ",\"value\":" << (a_active ? boolean(digital.bState) : "null") << ",\"changed\":" << (a_active ? boolean(digital.bChanged) : "null")
            << ",\"update_time\":";
        if (a_active) number(out, digital.fUpdateTime); else out << "null";
        out << ",\"origin\":" << a_origin << "},\"axis0\":{\"error\":" << axis_error
            << ",\"active\":" << (axis_error == vr::VRInputError_None ? boolean(analog.bActive) : "null") << ",\"value\":";
        if (axis_active) { out << '['; number(out, analog.x); out << ','; number(out, analog.y); out << ']'; } else out << "null";
        out << ",\"origin\":" << axis_origin << "},\"input_receipt\":\"unknown\",\"controller_forwarding\":\"unknown\"}";
        emit(out.str()); // One complete unbuffered record, before shutdown or another SDK read.
        ++count;
        std::this_thread::sleep_until(std::min(tick + interval, end));
    }
    return count;
}
} // namespace

int main(int argc, char **argv) {
    output_fd = dup(STDOUT_FILENO);
    if (output_fd < 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) return 1;
    const int flags = fcntl(output_fd, F_GETFL);
    if (flags < 0 || fcntl(output_fd, F_SETFL, flags | O_NONBLOCK) < 0) return 1;
    try {
        struct sigaction action{};
        action.sa_handler = timed_out;
        sigemptyset(&action.sa_mask);
        if (sigaction(SIGALRM, &action, nullptr) != 0) throw std::runtime_error("timeout_handler_unavailable");
        alarm(20);
        if (argc != 2) throw std::runtime_error("usage: frame-controller-actions ABS_INPUT_ACTIONS_JSON");
        const auto manifest = std::filesystem::path(argv[1]);
        validate_manifest(manifest); // Both reviewed files validated before SDK initialization.
        emit("{\"event\":\"start\",\"schema_version\":1,\"observer_pid\":" + std::to_string(getpid())
             + ",\"application_type\":\"background\",\"duration_seconds\":15,\"sample_limit\":300,\"maximum_hz\":20,\"input_receipt\":\"unknown\"}");
        stage = "setup";
        unsigned count;
        {
            frame_observe::Sdk sdk;
            auto *input = sdk.get<vr::IVRInput>(vr::IVRInput_Version);
            check(input->SetActionManifestPath(manifest.c_str()));
            count = observe(sdk);
            stage = "shutdown";
        }
        alarm(0);
        emit("{\"event\":\"end\",\"schema_version\":1,\"sample_count\":" + std::to_string(count)
             + ",\"outcome\":\"observation_window_complete\",\"input_receipt\":\"unknown\",\"controller_forwarding\":\"unknown\"}");
        return 0;
    } catch (const std::exception &error) {
        alarm(0);
        try { emit("{\"event\":\"error\",\"schema_version\":1,\"stage\":" + quote(stage) + ",\"error\":" + quote(error.what())
                   + ",\"outcome\":\"unknown\",\"input_receipt\":\"unknown\"}"); } catch (...) {}
        return 1;
    }
}
