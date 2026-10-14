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
static_assert(vr::k_unTrackedDeviceIndex_Hmd < device_limit, "HMD index must fit the existing pose array");
const char *stage = "validation";
bool binding_diagnostics = false;
bool trigger_diagnostic = false;
bool dual_stick_diagnostic = false;
bool right_b_diagnostic = false;
bool head_pose_diagnostic = false;
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
struct HeadObservation {
    vr::ETrackedDeviceClass class_before = vr::TrackedDeviceClass_Invalid, class_after = vr::TrackedDeviceClass_Invalid;
    bool class_before_returned = false, class_after_returned = false, pose_returned = false;
    vr::TrackedDevicePose_t value{};
    std::chrono::steady_clock::time_point started, completed;
    std::chrono::system_clock::time_point utc_started, utc_completed;
    const char *query_error = nullptr;
    void begin(vr::IVRSystem *system) {
        started = std::chrono::steady_clock::now(); utc_started = std::chrono::system_clock::now();
        try { class_before = system->GetTrackedDeviceClass(vr::k_unTrackedDeviceIndex_Hmd); class_before_returned = true; }
        catch (...) { query_error = "head_class_before_query_exception"; }
    }
    void end(vr::IVRSystem *system) {
        try { class_after = system->GetTrackedDeviceClass(vr::k_unTrackedDeviceIndex_Hmd); class_after_returned = true; }
        catch (...) { if (!query_error) query_error = "head_class_after_query_exception"; }
        completed = std::chrono::steady_clock::now(); utc_completed = std::chrono::system_clock::now();
    }
    std::string json(bool context_unchanged) const {
        bool finite = pose_returned;
        if (pose_returned) for (const auto &row : value.mDeviceToAbsoluteTracking.m)
            for (float component : row) finite = finite && std::isfinite(component);
        const bool correct_class = class_before_returned && class_after_returned
            && class_before == vr::TrackedDeviceClass_HMD && class_after == vr::TrackedDeviceClass_HMD;
        const char *reason = query_error ? query_error : !correct_class ? "head_class_unavailable_or_changed"
            : !pose_returned ? "head_pose_query_unavailable" : !value.bDeviceIsConnected ? "head_disconnected"
            : !value.bPoseIsValid ? "head_pose_invalid" : !finite ? "head_matrix_nonfinite" : nullptr;
        std::ostringstream out;
        out << "{\"device_index\":" << vr::k_unTrackedDeviceIndex_Hmd
            << ",\"tracking_space\":\"standing\",\"prediction_seconds\":0,\"pose_api_error\":null"
            << ",\"pose_query_returned\":" << boolean(pose_returned)
            << ",\"class_before\":" << (class_before_returned ? std::to_string(class_before) : "null")
            << ",\"class_after\":" << (class_after_returned ? std::to_string(class_after) : "null")
            << ",\"query_error\":" << (query_error ? quote(query_error) : "null")
            << ",\"sdk_pose_available\":" << boolean(reason == nullptr)
            << ",\"unavailable_reason\":" << (reason ? quote(reason) : "null")
            << ",\"context_unchanged\":" << boolean(context_unchanged)
            << ",\"query_started_utc_unix_ms\":" << std::chrono::duration_cast<std::chrono::milliseconds>(utc_started.time_since_epoch()).count()
            << ",\"query_completed_utc_unix_ms\":" << std::chrono::duration_cast<std::chrono::milliseconds>(utc_completed.time_since_epoch()).count()
            << ",\"query_started_monotonic_ns\":" << quote(std::to_string(std::chrono::duration_cast<std::chrono::nanoseconds>(started.time_since_epoch()).count()))
            << ",\"query_completed_monotonic_ns\":" << quote(std::to_string(std::chrono::duration_cast<std::chrono::nanoseconds>(completed.time_since_epoch()).count()))
            << ",\"connected\":" << (pose_returned ? boolean(value.bDeviceIsConnected) : "null")
            << ",\"valid\":" << (pose_returned ? boolean(value.bPoseIsValid) : "null")
            << ",\"tracking_result\":" << (pose_returned ? std::to_string(value.eTrackingResult) : "null")
            << ",\"matrix_finite\":" << (pose_returned ? boolean(finite) : "null") << ",\"matrix\":";
        if (!pose_returned) out << "null";
        else {
            out << '[';
            for (int row = 0; row < 3; ++row) {
                if (row) out << ',';
                out << '[';
                for (int col = 0; col < 4; ++col) { if (col) out << ','; number(out, value.mDeviceToAbsoluteTracking.m[row][col]); }
                out << ']';
            }
            out << ']';
        }
        out << '}';return out.str();
    }
};
std::string producer(vr::IVRSystem *system, const std::array<vr::TrackedDevicePose_t, device_limit> &poses,
                     const char *expected_serial = "frame_testbench_right") {
    unsigned matches = 0;
    std::string row;
    for (uint32_t index = 0; index < device_limit; ++index) {
        if (system->GetTrackedDeviceClass(index) != vr::TrackedDeviceClass_Controller) continue;
        const auto identity = serial(system, index);
        if (identity.error != vr::TrackedProp_Success || identity.value != expected_serial) continue;
        ++matches;
        row = "{\"device_index\":" + std::to_string(index) + ",\"serial\":" + identity.json()
            + ",\"actual_role\":" + std::to_string(system->GetControllerRoleForTrackedDeviceIndex(index))
            + ",\"standing_pose\":" + pose(poses[index]) + '}';
    }
    return "{\"match_count\":" + std::to_string(matches) + ",\"device\":" + (matches == 1 ? row : "null") + '}';
}
template<size_t N> std::string bounded_text(const char (&value)[N]);
std::string origin(vr::IVRInput *input, vr::IVRSystem *system, vr::VRInputValueHandle_t handle,
                   bool include_device_context = false) {
    vr::InputOriginInfo_t info{};
    std::fill(std::begin(info.rchRenderModelComponentName), std::end(info.rchRenderModelComponentName), '\x7f');
    info.trackedDeviceIndex = vr::k_unTrackedDeviceIndexInvalid;
    const auto error = input->GetOriginTrackedDeviceInfo(handle, &info, sizeof(info));
    const bool valid = error == vr::VRInputError_None && info.trackedDeviceIndex < device_limit
        && (!include_device_context || handle != vr::k_ulInvalidInputValueHandle);
    std::string device_context;
    if (include_device_context) {
        const auto validation = error != vr::VRInputError_None ? "null"
            : handle == vr::k_ulInvalidInputValueHandle ? quote("invalid_origin_handle")
            : info.trackedDeviceIndex >= device_limit ? quote("device_index_out_of_range") : "null";
        device_context = ",\"device_index_valid\":" + std::string(boolean(valid))
            + ",\"device_class\":" + (valid ? std::to_string(system->GetTrackedDeviceClass(info.trackedDeviceIndex)) : "null")
            + ",\"actual_role\":" + (valid ? std::to_string(system->GetControllerRoleForTrackedDeviceIndex(info.trackedDeviceIndex)) : "null")
            + ",\"validation_error\":" + validation;
    }
    return "{\"handle\":" + quote(std::to_string(handle)) + ",\"error\":" + std::to_string(error)
        + ",\"device_index\":" + (valid ? std::to_string(info.trackedDeviceIndex) : "null")
        + ",\"device_path_handle\":" + (valid ? quote(std::to_string(info.devicePath)) : "null")
        + ",\"serial\":" + (valid ? serial(system, info.trackedDeviceIndex).json() : "null")
        + ",\"component\":" + (error == vr::VRInputError_None ? bounded_text(info.rchRenderModelComponentName) : "null") + device_context + '}';
}
template<size_t N> std::string bounded_text(const char (&value)[N]) {
    const auto end = std::find(value, value + N, '\0');
    return end == value + N ? "{\"value\":null,\"validation_error\":\"unterminated\"}"
        : "{\"value\":" + quote(std::string(value, end)) + ",\"validation_error\":null}";
}
void binding_snapshot(vr::IVRInput *input, vr::IVRSystem *system,
                      vr::VRActionSetHandle_t set, vr::VRActionHandle_t action,
                      const char *name, unsigned sequence) {
    // Read-only, fixed-capacity calls in the existing SDK lifetime. No retries,
    // binding displays, focus requests, priority changes or producer writes.
    std::array<vr::InputBindingInfo_t, 16> bindings;
    for (auto &row : bindings) {
        std::fill(std::begin(row.rchDevicePathName), std::end(row.rchDevicePathName), '\x7f');
        std::fill(std::begin(row.rchInputPathName), std::end(row.rchInputPathName), '\x7f');
        std::fill(std::begin(row.rchModeName), std::end(row.rchModeName), '\x7f');
        std::fill(std::begin(row.rchSlotName), std::end(row.rchSlotName), '\x7f');
        std::fill(std::begin(row.rchInputSourceType), std::end(row.rchInputSourceType), '\x7f');
    }
    uint32_t returned = 0;
    const auto binding_error = input->GetActionBindingInfo(action, bindings.data(), sizeof(bindings[0]), bindings.size(), &returned);
    std::array<vr::VRInputValueHandle_t, 64> origins{};
    const auto origin_error = input->GetActionOrigins(set, action, origins.data(), origins.size());
    const auto prefix = std::string("\"schema_version\":1,\"observer_pid\":") + std::to_string(getpid())
        + ",\"sample_sequence\":" + std::to_string(sequence) + ",\"action\":" + quote(name);
    const bool binding_valid = binding_error == vr::VRInputError_None && returned <= bindings.size();
    emit("{\"event\":\"binding_query\"," + prefix + ",\"binding_error\":" + std::to_string(binding_error)
        + ",\"binding_capacity\":16,\"returned_binding_count\":" + std::to_string(returned)
        + ",\"binding_count_valid\":" + boolean(binding_valid)
        + ",\"binding_capacity_reached\":" + boolean(returned >= bindings.size())
        + ",\"origins_error\":" + std::to_string(origin_error)
        + ",\"origins_capacity\":64,\"origins_completeness\":\"unknown\",\"input_receipt\":\"unknown\"}");
    if (binding_valid) for (uint32_t i = 0; i < returned; ++i) {
        const auto &row = bindings[i];
        emit("{\"event\":\"binding_entry\"," + prefix + ",\"entry_index\":" + std::to_string(i)
            + ",\"device_path\":" + bounded_text(row.rchDevicePathName)
            + ",\"input_path\":" + bounded_text(row.rchInputPathName)
            + ",\"mode\":" + bounded_text(row.rchModeName)
            + ",\"slot\":" + bounded_text(row.rchSlotName)
            + ",\"input_source_type\":" + bounded_text(row.rchInputSourceType) + '}');
    }
    if (origin_error == vr::VRInputError_None) for (size_t i = 0; i < origins.size(); ++i) {
        if (origins[i] == vr::k_ulInvalidInputValueHandle) continue;
        emit("{\"event\":\"binding_origin\"," + prefix + ",\"entry_index\":" + std::to_string(i)
            + ",\"origin\":" + origin(input, system, origins[i], dual_stick_diagnostic || right_b_diagnostic) + '}');
    }
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
    vr::VRActionHandle_t a = 0, axis = 0, trigger = 0, left_axis = 0, b = 0;
    vr::VRInputValueHandle_t right = 0, left = 0;
    check(input->GetActionHandle("/actions/observe/in/right_a_click", &a));
    check(input->GetActionHandle("/actions/observe/in/right_thumbstick_position", &axis));
    if (trigger_diagnostic) check(input->GetActionHandle("/actions/observe/in/right_trigger_value", &trigger));
    if (right_b_diagnostic) check(input->GetActionHandle("/actions/observe/in/right_b_click", &b));
    check(input->GetInputSourceHandle("/user/hand/right", &right));
    if (dual_stick_diagnostic) {
        check(input->GetActionHandle("/actions/observe/in/left_thumbstick_position", &left_axis));
        check(input->GetInputSourceHandle("/user/hand/left", &left));
    }
    if (!set.ulActionSet || !a || !axis || !right) throw std::runtime_error("invalid_action_handle");
    if (trigger_diagnostic && !trigger) throw std::runtime_error("invalid_trigger_action_handle");
    if (dual_stick_diagnostic && (!left_axis || left_axis == axis || left_axis == a || (trigger_diagnostic && left_axis == trigger)))
        throw std::runtime_error("invalid_left_action_handle");
    if (dual_stick_diagnostic && (!left || left == right)) throw std::runtime_error("invalid_left_source_handle");
    if (right_b_diagnostic && (!b || b == a || b == axis || (trigger_diagnostic && b == trigger)
        || (dual_stick_diagnostic && b == left_axis))) throw std::runtime_error("invalid_right_b_action_handle");
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
        vr::InputDigitalActionData_t digital{}, b_data{};
        vr::InputAnalogActionData_t analog{};
        vr::InputAnalogActionData_t trigger_data{}, left_data{};
        auto a_error = update, axis_error = update, trigger_error = update, left_error = update, b_error = update;
        if (update == vr::VRInputError_None) {
            a_error = input->GetDigitalActionData(a, &digital, sizeof(digital), right);
            if (right_b_diagnostic) b_error = input->GetDigitalActionData(b, &b_data, sizeof(b_data), right);
            axis_error = input->GetAnalogActionData(axis, &analog, sizeof(analog), right);
            if (trigger_diagnostic) trigger_error = input->GetAnalogActionData(trigger, &trigger_data, sizeof(trigger_data), right);
            if (dual_stick_diagnostic) left_error = input->GetAnalogActionData(left_axis, &left_data, sizeof(left_data), left);
        }
        const bool a_active = a_error == vr::VRInputError_None && digital.bActive;
        const bool b_active = right_b_diagnostic && b_error == vr::VRInputError_None && b_data.bActive;
        const bool axis_active = axis_error == vr::VRInputError_None && analog.bActive;
        const bool trigger_active = trigger_diagnostic && trigger_error == vr::VRInputError_None && trigger_data.bActive;
        const bool left_active = dual_stick_diagnostic && left_error == vr::VRInputError_None && left_data.bActive;
        std::array<vr::TrackedDevicePose_t, device_limit> poses{};
        HeadObservation head;
        if (head_pose_diagnostic) {
            head.begin(system);
            try {
                system->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseStanding, 0, poses.data(), poses.size());
                head.value = poses[vr::k_unTrackedDeviceIndex_Hmd];head.pose_returned = true;
            } catch (...) { if (!head.query_error) head.query_error = "head_pose_query_exception"; }
            head.end(system);
        } else system->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseStanding, 0, poses.data(), poses.size());
        const auto producer_value = producer(system, poses);
        const auto left_producer = dual_stick_diagnostic ? producer(system, poses, "frame_testbench_left") : "null";
        const auto b_origin = b_active ? origin(input, system, b_data.activeOrigin, true) : "null";
        const auto a_origin = a_active ? origin(input, system, digital.activeOrigin, dual_stick_diagnostic) : "null";
        const auto axis_origin = axis_active ? origin(input, system, analog.activeOrigin, dual_stick_diagnostic) : "null";
        const auto trigger_origin = trigger_active ? origin(input, system, trigger_data.activeOrigin, dual_stick_diagnostic) : "null";
        const auto left_origin = left_active ? origin(input, system, left_data.activeOrigin, true) : "null";
        if (binding_diagnostics && count % 100 == 0) {
            binding_snapshot(input, system, set.ulActionSet, a, "right_a", count);
            binding_snapshot(input, system, set.ulActionSet, axis, "axis0", count);
            if (right_b_diagnostic) binding_snapshot(input, system, set.ulActionSet, b, "right_b", count);
            if (trigger_diagnostic) binding_snapshot(input, system, set.ulActionSet, trigger, "right_trigger", count);
            if (dual_stick_diagnostic) binding_snapshot(input, system, set.ulActionSet, left_axis, "left_axis0", count);
        }
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
        out << ",\"origin\":" << axis_origin << '}';
        if (right_b_diagnostic) {
            out << ",\"right_b\":{\"error\":" << b_error << ",\"active\":"
                << (b_error == vr::VRInputError_None ? boolean(b_data.bActive) : "null") << ",\"value\":"
                << (b_active ? boolean(b_data.bState) : "null") << ",\"changed\":"
                << (b_active ? boolean(b_data.bChanged) : "null") << ",\"update_time\":";
            if (b_active) number(out, b_data.fUpdateTime); else out << "null";
            out << ",\"origin\":" << b_origin << '}';
        }
        if (trigger_diagnostic) {
            out << ",\"right_trigger\":{\"error\":" << trigger_error << ",\"active\":"
                << (trigger_error == vr::VRInputError_None ? boolean(trigger_data.bActive) : "null") << ",\"value\":";
            if (trigger_active) number(out, trigger_data.x); else out << "null";
            out << ",\"origin\":" << trigger_origin << '}';
        }
        if (dual_stick_diagnostic) {
            out << ",\"left_producer\":" << left_producer << ",\"left_axis0\":{\"error\":" << left_error
                << ",\"active\":" << (left_error == vr::VRInputError_None ? boolean(left_data.bActive) : "null") << ",\"value\":";
            if (left_active) { out << '['; number(out, left_data.x); out << ','; number(out, left_data.y); out << ']'; } else out << "null";
            out << ",\"origin\":" << left_origin << '}';
        }
        if (head_pose_diagnostic) out << ",\"head_pose\":" << head.json(scene_before == scene_after
            && dashboard_before == dashboard_after && available_before == available);
        out << ",\"input_receipt\":\"unknown\",\"controller_forwarding\":\"unknown\"}";
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
        if (argc < 2 || argc > 7) throw std::runtime_error("usage: frame-controller-actions ABS_INPUT_ACTIONS_JSON [--bindings] [--trigger] [--dual-stick] [--right-b] [--head-pose]");
        for (int i = 2; i < argc; ++i) {
            const std::string option(argv[i]);
            if (option == "--bindings" && !binding_diagnostics) binding_diagnostics = true;
            else if (option == "--trigger" && !trigger_diagnostic) trigger_diagnostic = true;
            else if (option == "--dual-stick" && !dual_stick_diagnostic) dual_stick_diagnostic = true;
            else if (option == "--right-b" && !right_b_diagnostic) right_b_diagnostic = true;
            else if (option == "--head-pose" && !head_pose_diagnostic) head_pose_diagnostic = true;
            else throw std::runtime_error("invalid_or_duplicate_observer_option");
        }
        const auto manifest = std::filesystem::path(argv[1]);
        validate_manifest(manifest); // Both reviewed files validated before SDK initialization.
        emit("{\"event\":\"start\",\"schema_version\":1,\"observer_pid\":" + std::to_string(getpid())
             + ",\"application_type\":\"background\",\"duration_seconds\":15,\"sample_limit\":300,\"maximum_hz\":20,\"binding_diagnostics\":"
             + boolean(binding_diagnostics) + (trigger_diagnostic ? ",\"trigger_diagnostic\":true" : "")
             + (dual_stick_diagnostic ? ",\"dual_stick_diagnostic\":true" : "")
             + (right_b_diagnostic ? ",\"right_b_diagnostic\":true" : "")
             + (head_pose_diagnostic ? ",\"head_pose_diagnostic\":true" : "")
             + ",\"input_receipt\":\"unknown\"}");
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
