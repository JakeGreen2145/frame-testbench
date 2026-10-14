#include "sdk.hpp"
#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <csignal>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <unistd.h>

namespace {
int output_fd = STDOUT_FILENO;
void timed_out(int) {
    constexpr char message[] = "{\"ok\":false,\"error\":\"controller property observation timed out\"}\n";
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
void number(std::ostream &out, float value) {
    if (std::isfinite(value)) out << std::setprecision(10) << value;
    else out << "null";
}
std::string standing_pose(const vr::TrackedDevicePose_t &pose) {
    std::ostringstream out;
    out << "{\"valid\":" << (pose.bPoseIsValid ? "true" : "false")
        << ",\"connected\":" << (pose.bDeviceIsConnected ? "true" : "false")
        << ",\"tracking_result\":" << pose.eTrackingResult << ",\"matrix\":[";
    for (int row = 0; row < 3; ++row) {
        if (row) out << ',';
        out << '[';
        for (int column = 0; column < 4; ++column) {
            if (column) out << ',';
            number(out, pose.mDeviceToAbsoluteTracking.m[row][column]);
        }
        out << ']';
    }
    out << "]}";
    return out.str();
}
std::string string_property(vr::IVRSystem *system, uint32_t index, vr::ETrackedDeviceProperty property) {
    std::array<char, 256> buffer{};
    vr::ETrackedPropertyError error = vr::TrackedProp_Success;
    const uint32_t required = system->GetStringTrackedDeviceProperty(index, property, buffer.data(), buffer.size(), &error);
    if (error == vr::TrackedProp_Success && (required == 0 || required > buffer.size() || buffer[required - 1] != '\0'))
        error = vr::TrackedProp_BufferTooSmall;
    std::ostringstream out;
    out << "{\"value\":";
    if (error == vr::TrackedProp_Success) out << quote(std::string(buffer.data(), required - 1));
    else out << "null";
    out << ",\"error\":" << error << ",\"required_bytes\":" << required << '}';
    return out.str();
}
std::string integer_property(vr::IVRSystem *system, uint32_t index, vr::ETrackedDeviceProperty property) {
    vr::ETrackedPropertyError error = vr::TrackedProp_Success;
    const int32_t value = system->GetInt32TrackedDeviceProperty(index, property, &error);
    return std::string("{\"value\":") + (error == vr::TrackedProp_Success ? std::to_string(value) : "null")
        + ",\"error\":" + std::to_string(error) + '}';
}
std::string observe(vr::IVRSystem *system) {
    constexpr uint32_t limit = std::min<uint32_t>(64, vr::k_unMaxTrackedDeviceCount);
    std::array<vr::TrackedDevicePose_t, limit> poses{};
    system->GetDeviceToAbsoluteTrackingPose(vr::TrackingUniverseStanding, 0, poses.data(), poses.size());
    std::ostringstream out;
    out << "{\"ok\":true,\"schema_version\":1,\"command\":\"controller-properties\",\"device_limit\":" << limit
        << ",\"string_capacity_bytes\":256,\"controller_forwarding\":\"unknown\",\"controllers\":[";
    bool first = true;
    struct StringProperty { const char *name; vr::ETrackedDeviceProperty key; };
    const StringProperty strings[] = {
        {"driver", vr::Prop_TrackingSystemName_String}, {"model", vr::Prop_ModelNumber_String},
        {"serial", vr::Prop_SerialNumber_String}, {"registered_device_type", vr::Prop_RegisteredDeviceType_String},
        {"controller_type", vr::Prop_ControllerType_String}, {"input_profile_path", vr::Prop_InputProfilePath_String}};
    for (uint32_t index = 0; index < limit; ++index) {
        const auto device_class = system->GetTrackedDeviceClass(index);
        if (device_class != vr::TrackedDeviceClass_Controller) continue;
        if (!first) out << ',';
        first = false;
        out << "{\"device_index\":" << index << ",\"device_class\":" << device_class
            << ",\"actual_role\":" << system->GetControllerRoleForTrackedDeviceIndex(index)
            << ",\"role_hint\":" << integer_property(system, index, vr::Prop_ControllerRoleHint_Int32)
            << ",\"hand_selection_priority\":" << integer_property(system, index, vr::Prop_ControllerHandSelectionPriority_Int32);
        for (const auto &property : strings)
            out << ',' << quote(property.name) << ':' << string_property(system, index, property.key);
        out << ",\"standing_pose\":" << standing_pose(poses[index]) << '}';
    }
    out << "]}";
    return out.str();
}
void emit(const std::string &value) {
    const auto line = value + '\n';
    size_t offset = 0;
    while (offset < line.size()) {
        const auto size = write(output_fd, line.data() + offset, line.size() - offset);
        if (size < 0 && errno == EINTR) continue;
        if (size <= 0) break;
        offset += size_t(size);
    }
}
} // namespace

int main(int argc, char **) {
    output_fd = dup(STDOUT_FILENO);
    if (output_fd < 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) return 1;
    try {
        if (argc != 1) throw std::runtime_error("usage: frame-controller-properties (no arguments)");
        struct sigaction action{};
        action.sa_handler = timed_out;
        sigemptyset(&action.sa_mask);
        if (sigaction(SIGALRM, &action, nullptr) != 0) throw std::runtime_error("cannot install timeout handler");
        alarm(5); // Includes SDK initialization, all reads and shutdown; never retries a property.
        std::string result;
        {
            frame_observe::Sdk sdk;
            result = observe(sdk.get<vr::IVRSystem>(vr::IVRSystem_Version));
        }
        alarm(0);
        emit(result);
        return 0;
    } catch (const std::exception &error) {
        alarm(0);
        emit("{\"ok\":false,\"error\":" + quote(error.what()) + '}');
        return 1;
    }
}
