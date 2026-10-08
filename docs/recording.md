# Compositor video recording

`record` produces a silent H.264 MP4 from sampled OpenVR compositor screenshots. It is available in the CLI and as an MCP tool. The recorder does not change proximity, poses, input values, or standby policy.

```sh
# If testing an unattended, unworn headset, prepare it explicitly.
./frame-testbench --host frame compositor awake
./frame-testbench --host frame worn on
./frame-testbench --host frame pose set --position 0 1.6 0

# Fresh local destination; video and timing metadata are fetched over SSH.
./frame-testbench --host frame record --duration 10 --fps 5 --view stereo \
  --fetch artifacts/my-recording

# Input calls can run concurrently from another CLI process or the same MCP server.
# At the end of the input test session:
./frame-testbench --host frame release
./frame-testbench --host frame compositor auto
```

Do not drive a synthetic HMD pose while somebody is wearing the headset. Check physical proximity first. Existing active overrides belong to the current test session; recording neither resets nor takes ownership of them.

## Contract

- `--duration`: requested sampling window, integer 1..60 seconds, default 10. SDK initialization and encoding add time. A screenshot already requested at the end of the window may complete afterward.
- `--fps`: maximum requested sampling cadence, integer 1..10, default 5. It is **not a promised capture frame rate**. The recorder waits for each complete screenshot and skips missed sampling slots without catch-up bursts.
- `--view stereo`: use the stereo screenshot layout provided by SteamVR. `--view preview` uses the runtime's preview image instead. The encoder preserves aspect ratio and limits width to 1920 pixels with even dimensions.
- `--output`: new output directory on the capture host. With SSH this is a headset path.
- `--fetch`: new local directory for `recording.mp4`, `timeline.json`, and a local `result.json`. Requires `--host`.

The capture host needs `ffmpeg` with `libx264`, and `ffprobe`. The remote-only client does not need them. Normal `make` builds the native observer. No driver reinstall or SteamVR restart is needed to update the recorder.

At least two complete samples are required. A very short window or stalled compositor can fail this condition. Inactive/standby headsets may produce blank or frozen views or time out. Recording does not secretly wake the headset or spoof tracking.

## MCP

```json
{"name":"record","arguments":{"duration_seconds":10,"fps":5,"view":"stereo"}}
```

The tool returns structured recording metadata and local file resource links rather than embedding a potentially large video as base64. It fetches remote output into the configured MCP artifacts directory. A `file:` URI is on the MCP server's host, so the calling agent needs access to that host to deliver the attachment. A generic MCP client might not render these links inline.

Recording has a separate lock from input tools. Concurrent pose/button/status calls can therefore execute while the recording request is in progress. Recording calls in one server serialize. This is not a lock across independent MCP servers or CLI processes; coordinate one recorder per headset.

Cancellation of an MCP request does not abandon the worker midway through capture or encoding. It completes within backend deadlines and retains its files. Use a client timeout long enough for capture, encoding, and SSH transfer, such as 420 seconds.

## Timing and evidence

`timeline.json` retains every sample's monotonic request and completion offsets. These bracket the screenshot operation; they are not measured photon-exposure timestamps. Video presentation time starts at the first completed screenshot. Each image remains visible until the next completion, preserving observed gaps instead of speeding up a slow capture. The final sample is held for one requested sampling interval. The container also has a duplicate terminator frame to represent that last interval.

The JSON distinguishes `requested_fps`, `measured_sample_fps`, `sample_count`, `elapsed_seconds`, `video_duration_seconds`, `encoded_duration_seconds`, and `encoded_frame_count`. The video is variable-frame-rate. Repeated pixels can mean a static scene or a frozen source; timing metadata alone does not establish freshness.

This is **sampled compositor video**, not a native headset-rate mirror, a full-lens recording, an audio recording, or camera-alignment proof. It inherits the cropped field of view and overlay behavior of OpenVR stereo screenshots. Screenshot requests can perturb application rendering, so do not use this recorder to certify frame pacing or latency. In a bounded Frame probe, `GetMirrorTextureGL` returned `VRCompositorError_InvalidTexture` with a current EGL/OpenGL context even while awake. No unverified continuous mirror fallback is enabled.

## Bounds and failure behavior

One Background OpenVR session owns the capture sequence. Samples are sequential and complete only after both PNGs pass chunk/CRC/IEND checks. Native capture limits accepted recordings to 600 samples and 512 MiB of completed screenshot pairs. The byte check occurs after each complete pair; a failed over-budget capture retains that pair, so this is not a hard quota on asynchronous SDK disk writes. The recording window is at most 60 seconds; the default native watchdog adds 20 seconds, and an explicit observer timeout remains bounded at 120 seconds. Encoding is bounded at 90 seconds, probing at 15, and each SSH copy at 60. Overrides persist independently of those process lifetimes.

The encoder writes `recording.partial.mp4`, validates its codec, dimensions, frame count and measured duration, then renames it to `recording.mp4`. Failures return errors and retain diagnostic files without claiming a successful result. Existing output directories are never reused. Raw samples and logs stay on the capture host for diagnosis; the client fetches only the video and metadata. These can contain private screens, so keep artifacts out of the public repository.
