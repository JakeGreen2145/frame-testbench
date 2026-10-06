#include "sdk.hpp"
#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <thread>
#include <sys/stat.h>
#include <unistd.h>

namespace fs = std::filesystem;
using frame_observe::Sdk;
namespace {
int output_fd = STDOUT_FILENO;

void timeout_handler(int) {
    constexpr char message[] = "{\"ok\":false,\"error\":\"operation timed out; capture files may be incomplete\"}\n";
    // Only async-signal-safe operations. Do not block on a hung SDK destructor.
    const auto ignored = write(output_fd, message, sizeof(message) - 1);
    (void)ignored;
    _exit(124);
}

std::string quote(const std::string &s) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 0x20) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c);
        else out << c;
    }
    out << '"';
    return out.str();
}
const char *boolean(bool v) { return v ? "true" : "false"; }
void number(std::ostream &out, double v) {
    if (std::isfinite(v)) out << std::setprecision(10) << v;
    else out << "null";
}
std::string matrix(const vr::HmdMatrix34_t &m) {
    std::ostringstream out;
    out << '[';
    for (int r = 0; r < 3; ++r) {
        if (r) out << ',';
        out << '[';
        for (int c = 0; c < 4; ++c) { if (c) out << ','; number(out, m.m[r][c]); }
        out << ']';
    }
    out << ']';
    return out.str();
}
std::string vector(const vr::HmdVector3_t &v) {
    std::ostringstream out;
    out << '[';
    for (int i = 0; i < 3; ++i) { if (i) out << ','; number(out, v.v[i]); }
    out << ']';
    return out.str();
}
std::string pose(const vr::TrackedDevicePose_t &p) {
    std::ostringstream out;
    out << "{\"valid\":" << boolean(p.bPoseIsValid)
        << ",\"connected\":" << boolean(p.bDeviceIsConnected)
        << ",\"tracking_result\":" << p.eTrackingResult
        << ",\"matrix\":" << matrix(p.mDeviceToAbsoluteTracking)
        << ",\"velocity\":" << vector(p.vVelocity)
        << ",\"angular_velocity\":" << vector(p.vAngularVelocity) << '}';
    return out.str();
}

bool pause_setting(vr::IVRSettings *settings) {
    vr::EVRSettingsError error = vr::VRSettingsError_None;
    bool value = settings->GetBool(vr::k_pch_Power_Section,
        vr::k_pch_Power_PauseCompositorOnStandby_Bool, &error);
    if (error != vr::VRSettingsError_None)
        throw std::runtime_error("reading power.pauseCompositorOnStandby failed: " + std::to_string(error));
    return value;
}

std::string status(Sdk &sdk) {
    auto *system = sdk.get<vr::IVRSystem>(vr::IVRSystem_Version);
    auto *compositor = sdk.get<vr::IVRCompositor>(vr::IVRCompositor_Version);
    auto *settings = sdk.get<vr::IVRSettings>(vr::IVRSettings_Version);
    struct Property { const char *name; vr::ETrackedDeviceProperty key; };
    const Property properties[] = {{"driver", vr::Prop_TrackingSystemName_String},
        {"model", vr::Prop_ModelNumber_String}, {"serial", vr::Prop_SerialNumber_String}};
    std::ostringstream out, errors;
    out << "{\"ok\":true,\"command\":\"status\",\"hmd\":{\"device_index\":0";
    errors << '{';
    for (size_t i = 0; i < 3; ++i) {
        const auto &property = properties[i];
        std::array<char, vr::k_unMaxPropertyStringSize> value{};
        vr::ETrackedPropertyError error = vr::TrackedProp_Success;
        const auto count = system->GetStringTrackedDeviceProperty(vr::k_unTrackedDeviceIndex_Hmd,
            property.key, value.data(), value.size(), &error);
        if (error == vr::TrackedProp_Success && (count == 0 || count > value.size() || value[count - 1] != '\0'))
            error = vr::TrackedProp_BufferTooSmall;
        out << ',' << quote(property.name) << ':';
        if (error == vr::TrackedProp_Success) out << quote(std::string(value.data(), count - 1));
        else out << "null";
        if (i) errors << ',';
        errors << quote(property.name) << ':' << error;
    }
    errors << '}';
    out << ",\"property_errors\":" << errors.str()
        << ",\"activity_level\":" << system->GetTrackedDeviceActivityLevel(vr::k_unTrackedDeviceIndex_Hmd)
        << ",\"poses\":{";
    const vr::ETrackingUniverseOrigin origins[] = {vr::TrackingUniverseStanding,
        vr::TrackingUniverseRawAndUncalibrated, vr::TrackingUniverseSeated};
    const char *names[] = {"standing", "raw", "seated"};
    for (int i = 0; i < 3; ++i) {
        vr::TrackedDevicePose_t p{};
        system->GetDeviceToAbsoluteTrackingPose(origins[i], 0, &p, 1);
        if (i) out << ',';
        out << quote(names[i]) << ':' << pose(p);
    }
    out << "}},\"transforms\":{\"raw_to_standing\":"
        << matrix(system->GetRawZeroPoseToStandingAbsoluteTrackingPose())
        << ",\"seated_to_standing\":" << matrix(system->GetSeatedZeroPoseToStandingAbsoluteTrackingPose()) << '}';
    vr::Compositor_FrameTiming current{}, previous{};
    current.m_nSize = sizeof(current);
    previous.m_nSize = sizeof(previous);
    const bool current_ok = compositor->GetFrameTiming(&current, 0);
    const bool previous_ok = compositor->GetFrameTiming(&previous, 1);
    float vsync_seconds = 0;
    uint64_t vsync_counter = 0;
    const bool vsync_ok = system->GetTimeSinceLastVsync(&vsync_seconds, &vsync_counter);
    out << ",\"compositor\":{\"timing_available\":" << boolean(current_ok)
        << ",\"frame_index\":" << (current_ok ? std::to_string(current.m_nFrameIndex) : "null")
        << ",\"previous_frame_index\":" << (previous_ok ? std::to_string(previous.m_nFrameIndex) : "null")
        << ",\"tracking_space\":" << compositor->GetTrackingSpace()
        << ",\"vsync_frame_counter\":" << (vsync_ok ? std::to_string(vsync_counter) : "null")
        << ",\"seconds_since_vsync\":";
    if (vsync_ok) number(out, vsync_seconds); else out << "null";
    out << ",\"system_time_seconds\":";
    if (current_ok) number(out, current.m_flSystemTimeInSeconds); else out << "null";
    out << ",\"num_frame_presents\":" << (current_ok ? std::to_string(current.m_nNumFramePresents) : "null")
        << ",\"num_dropped_frames\":" << (current_ok ? std::to_string(current.m_nNumDroppedFrames) : "null")
        << ",\"render_pose\":" << (current_ok ? pose(current.m_HmdPose) : "null")
        << "},\"settings\":{\"pauseCompositorOnStandby\":" << boolean(pause_setting(settings)) << "}}";
    return out.str();
}

std::string set_pause(Sdk &sdk, bool requested) {
    auto *settings = sdk.get<vr::IVRSettings>(vr::IVRSettings_Version);
    vr::EVRSettingsError error = vr::VRSettingsError_None;
    settings->SetBool(vr::k_pch_Power_Section, vr::k_pch_Power_PauseCompositorOnStandby_Bool, requested, &error);
    if (error != vr::VRSettingsError_None)
        throw std::runtime_error("writing power.pauseCompositorOnStandby failed: " + std::to_string(error));
    bool actual = pause_setting(settings);
    if (actual != requested) throw std::runtime_error("power.pauseCompositorOnStandby readback mismatch");
    return std::string("{\"ok\":true,\"command\":\"setting\",\"pauseCompositorOnStandby\":") + boolean(actual) + '}';
}

uint32_t big_endian(const unsigned char *p) {
    return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3];
}
uint32_t crc_update(uint32_t crc, const unsigned char *p, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        crc ^= p[i];
        for (int b = 0; b < 8; ++b) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return crc;
}
// Require signature, IHDR dimensions, IDAT, every chunk CRC, and terminal IEND.
// A path, a stable file size, or an early PNG signature alone is not completion.
// This checks container completeness, not the decoded pixels or headset optics.
bool complete_png(const fs::path &path) {
    std::error_code error;
    if (!fs::is_regular_file(fs::symlink_status(path, error))) return false;
    std::ifstream file(path, std::ios::binary);
    std::array<unsigned char, 8> header{};
    const unsigned char signature[] = {137, 80, 78, 71, 13, 10, 26, 10};
    if (!file.read(reinterpret_cast<char *>(header.data()), 8) || std::memcmp(header.data(), signature, 8)) return false;
    bool ihdr = false, idat = false;
    uint64_t total = 8;
    std::array<unsigned char, 65536> buffer{};
    for (;;) {
        if (!file.read(reinterpret_cast<char *>(header.data()), 8)) return false;
        uint32_t size = big_endian(header.data());
        total += uint64_t(size) + 12;
        if (total > 256U * 1024U * 1024U) return false;
        const std::string type(reinterpret_cast<char *>(header.data() + 4), 4);
        if (!ihdr && (type != "IHDR" || size != 13)) return false;
        if (ihdr && type == "IHDR") return false;
        uint32_t crc = crc_update(0xffffffffU, header.data() + 4, 4);
        uint32_t left = size;
        while (left) {
            size_t count = std::min<size_t>(left, buffer.size());
            if (!file.read(reinterpret_cast<char *>(buffer.data()), count)) return false;
            if (type == "IHDR" && (!big_endian(buffer.data()) || !big_endian(buffer.data() + 4))) return false;
            crc = crc_update(crc, buffer.data(), count);
            left -= uint32_t(count);
        }
        std::array<unsigned char, 4> expected{};
        if (!file.read(reinterpret_cast<char *>(expected.data()), 4) || big_endian(expected.data()) != (crc ^ 0xffffffffU)) return false;
        if (type == "IHDR") ihdr = true;
        if (type == "IDAT" && size) idat = true;
        if (type == "IEND") return size == 0 && ihdr && idat && file.peek() == std::char_traits<char>::eof();
    }
}

std::string capture(Sdk &sdk, const fs::path &directory) {
    auto *screenshots = sdk.get<vr::IVRScreenshots>(vr::IVRScreenshots_Version);
    // Atomic mkdir is the final no-overwrite check, including races since CLI validation.
    if (mkdir(directory.c_str(), 0700) != 0)
        throw std::runtime_error("cannot create fresh capture directory: " + std::string(std::strerror(errno)));
    const std::string preview = (directory / "preview.png").string();
    const std::string stereo = (directory / "stereo.png").string();
    vr::ScreenshotHandle_t handle = vr::k_unScreenshotHandleInvalid;
    const auto error = screenshots->RequestScreenshot(&handle, vr::VRScreenshotType_Stereo, preview.c_str(), stereo.c_str());
    if (error != vr::VRScreenshotError_None || handle == vr::k_unScreenshotHandleInvalid)
        throw std::runtime_error("RequestScreenshot failed: " + std::to_string(error));
    // The process-wide alarm also bounds hung SDK calls and shutdown. Failed or
    // timed-out directories are retained for inspection, never reused or deleted.
    while (!complete_png(preview) || !complete_png(stereo))
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    return "{\"ok\":true,\"command\":\"capture\",\"screenshot_handle\":" + std::to_string(handle)
        + ",\"preview\":" + quote(preview) + ",\"stereo\":" + quote(stereo)
        + ",\"capture_scope\":\"OpenVR stereo screenshot; may include overlays, cropped FOV; not full lens output or camera proof\"}";
}

std::string debug(Sdk &sdk, uint32_t device, const std::string &request) {
    auto *api = sdk.get<vr::IVRDebug>(vr::IVRDebug_Version);
    // One invocation only: a debug request can have side effects. Never repeat
    // it merely to size the response buffer.
    std::array<char, 65536> response{};
    const auto length = api->DriverDebugRequest(device, request.c_str(), response.data(), response.size());
    if (length > response.size() || !std::memchr(response.data(), '\0', response.size()))
        throw std::runtime_error("DriverDebugRequest response exceeds buffer");
    return "{\"ok\":true,\"command\":\"debug\",\"device_index\":" + std::to_string(device)
        + ",\"response\":" + quote(response.data()) + '}';
}

unsigned integer(const std::string &s, unsigned minimum, unsigned maximum) {
    if (s.empty()) throw std::runtime_error("expected integer");
    unsigned value = 0;
    for (unsigned char c : s) {
        if (c < '0' || c > '9') throw std::runtime_error("expected decimal integer");
        const unsigned digit = c - '0';
        if (value > maximum / 10 || (value == maximum / 10 && digit > maximum % 10))
            throw std::runtime_error("integer outside allowed range");
        value = value * 10 + digit;
    }
    if (value < minimum) throw std::runtime_error("integer outside allowed range");
    return value;
}

void emit(const std::string &value) {
    const std::string line = value + '\n';
    size_t offset = 0;
    while (offset < line.size()) {
        const auto n = write(output_fd, line.data() + offset, line.size() - offset);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        offset += size_t(n);
    }
}
} // namespace

int main(int argc, char **argv) {
    // Isolate SDK stdout chatter, including dlopen constructors and shutdown.
    output_fd = dup(STDOUT_FILENO);
    if (output_fd < 0 || dup2(STDERR_FILENO, STDOUT_FILENO) < 0) return 1;
    try {
        const std::string usage = "usage: frame-observe status | capture ABS_OUTPUT_DIR | setting true|false|1|0 | debug DEVICE_INDEX REQUEST";
        if (argc < 2) throw std::runtime_error(usage);
        const std::string command = argv[1];
        fs::path directory;
        bool requested = false;
        unsigned device = 0;
        if (command == "status" && argc == 2) {}
        else if (command == "capture" && argc == 3) {
            directory = fs::path(argv[2]);
            if (!directory.is_absolute()) throw std::runtime_error("capture output must be absolute");
            std::error_code error;
            auto existing = fs::symlink_status(directory, error);
            if (fs::exists(existing)) throw std::runtime_error("capture output already exists; use a fresh directory");
            if (error && error != std::errc::no_such_file_or_directory)
                throw std::runtime_error("cannot inspect capture output: " + error.message());
        } else if (command == "setting" && argc == 3) {
            const std::string value = argv[2];
            if (value != "true" && value != "false" && value != "1" && value != "0") throw std::runtime_error(usage);
            requested = value == "true" || value == "1";
        } else if (command == "debug" && argc == 4 && argv[3][0]) {
            device = integer(argv[2], 0, vr::k_unMaxTrackedDeviceCount - 1);
        } else throw std::runtime_error(usage);
        unsigned timeout = 20;
        if (const char *value = std::getenv("FRAME_OBSERVE_TIMEOUT_SECONDS")) timeout = integer(value, 1, 120);
        struct sigaction action{};
        action.sa_handler = timeout_handler;
        sigemptyset(&action.sa_mask);
        if (sigaction(SIGALRM, &action, nullptr) != 0) throw std::runtime_error("cannot install timeout handler");
        alarm(timeout);
        std::string result;
        {
            Sdk sdk;
            if (command == "status") result = status(sdk);
            else if (command == "setting") result = set_pause(sdk, requested);
            else if (command == "capture") result = capture(sdk, directory);
            else result = debug(sdk, device, argv[3]);
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
