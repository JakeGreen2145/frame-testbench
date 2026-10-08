# Compositor video recording

`record` defaults to a **continuous 1080p30 headset-view recording**, using Framecorder's Vulkan acquisition and Qualcomm hardware encoder. It produces a silent H.264 MP4 and timing metadata without repeated screenshot requests or intermediate PNGs. It does not change proximity, poses, inputs, or standby policy.

## One-time capture-host setup

On the ARM64 Frame, inside the checkout:

```sh
python3 scripts/setup-framecorder.py
```

This explicitly downloads the pinned Framecorder `v0.1.1` release, checks pinned archive and executable SHA-256 hashes, and extracts **only** `bin/framecorder` into `tools/framecorder-v0.1.1/framecorder`. It does not execute the release's installer, install its UI/sync services, grant capabilities, or touch `/opt`. Existing different executables are not overwritten. `--archive PATH` accepts an offline copy with the same required checksum.

Framecorder is a separate MIT-licensed dependency; [its notice](framecorder-LICENSE.txt) and [our source research](continuous-capture-research.md) are included. No upstream binary is committed. An operator can select a separately managed executable with `FRAME_TESTBENCH_FRAMECORDER`; that must be set on the capture host, not merely the SSH client. The normal recording command never downloads or installs software automatically.

The capture host also needs `ffprobe`, an active SteamVR runtime, and access to the Frame's DRM/Vulkan/encoder devices. Normal `make` builds our observer. No driver reinstall or SteamVR restart is required for this recording update.

## Usage

```sh
# Normal continuous GPU / hardware-encoder path. Fresh local destination.
./frame-testbench --host frame record --duration 10 \
  --fetch artifacts/my-recording

# Explicit form:
./frame-testbench --host frame record --duration 10 --fps 30 --view headset \
  --fetch artifacts/another-recording

# Legacy screenshot sampling is explicit; it is not an automatic fallback.
./frame-testbench --host frame record --duration 10 --fps 5 --view stereo \
  --fetch artifacts/sampled-stereo
```

For an unattended test, prepare the unworn headset **explicitly** before recording:

```sh
./frame-testbench --host frame status
# Only if physically unworn and this session owns the test overrides:
./frame-testbench --host frame compositor awake
./frame-testbench --host frame worn on
./frame-testbench --host frame pose set --position 0 1.6 0
# ...record and exercise input...
./frame-testbench --host frame release
./frame-testbench --host frame compositor auto
```

Do not drive a synthetic HMD pose while somebody is wearing the headset. Recording neither resets nor takes ownership of existing overrides. Inactive displays can return dark/stale pixels or time out; recording does not secretly wake the device.

## Contract

- `--duration`: integer 1..60 seconds, default 10. Initialization, validation and transfer add wall time.
- `--view headset` (default): continuous SteamVR `system.HeadsetView`, fixed left eye on Frame, 1920×1080. `--fps` is integer 1..60, default 30. These are requested encoding/capture settings, not proof of new source frames at that rate.
- `--view stereo` / `--view preview`: legacy sampled OpenVR screenshots, **requiring `--fps 1..10` explicitly** because the new global default is 30. No hardware encoder is used. The old pipeline preserves screenshot-completion gaps and limits width to 1920 with even dimensions.
- `--output`: new capture-host directory. With SSH, this is a headset path.
- `--fetch`: new local directory containing `recording.mp4`, `timeline.json`, and local `result.json`; requires `--host`.
- All modes are silent. No microphone, game-audio capture, replay buffer, privileged panel capture, or background service is enabled.
- At least two encoded frames are required. Existing output directories are never reused. Errors retain diagnostics without reporting success.

The `headset` view is a composited VR view, not a desktop capture, but it is **not full stereo panel scanout**. The live validation established Frametop spatial-overlay coverage. Camera-layer inclusion and other overlays must be checked in the application being tested. A virtual head pose cannot move physical cameras or certify passthrough alignment.

## MCP

```json
{"name":"record","arguments":{"duration_seconds":10,"fps":30,"view":"headset"}}
```

The tool returns structured metadata and local video/timeline resource links, not base64 video. It fetches remote output into the configured MCP artifacts directory. A `file:` URI is on the MCP server's host; the calling agent needs access to deliver the attachment. Generic clients may not render these links inline.

Recording has a separate lock from input tools, so concurrent pose/button/status calls remain available. Recordings in one MCP server serialize. This is not a global lock across CLI processes or other MCP servers; coordinate one recorder per headset.

MCP cancellation does not abandon the worker in mid-recording. It completes within backend deadlines and retains files. Use a client timeout long enough for capture, validation and SSH transfer, such as 420 seconds. Child stdin is disconnected from the MCP protocol.

## Timing and evidence

For `headset`, Framecorder uses KMS vblank pacing and its encoder/container timeline. The wrapper reports probed video dimensions, codec, encoded duration and frame count. Container presentation times are not photon-exposure measurements. Encoded FPS and distinct compressed/decoded frames do not alone prove independent fresh source frames. Inspect a changing scene, not just a nonempty MP4.

For legacy sampled views, `timeline.json` retains monotonic screenshot request/completion offsets. These bracket receipt, not photon time. Each image is held until the next completion; the final sample is held one requested interval, with a duplicate terminator frame. `measured_sample_fps` is distinct from requested cadence and encoded frame count.

See [continuous-capture research](continuous-capture-research.md), [integrated CLI/MCP hardware validation](continuous-recording-validation.md), and [original screenshot-backend validation](recording-validation.md). The hardware result is a light test scene, not a latency or GPU-bound gameplay benchmark. The renderer and recorder both consume resources; this is not a zero-cost capture claim.

## Bounds and failures

The continuous backend launches one bounded recorder process with no inherited protocol stdin and no hidden fallback. Its wrapper independently bounds startup/capture/teardown, since Framecorder's own duration does not bound an inactive display's vblank wait. Output is checked with ffprobe and constrained to a regular MP4 of at most 512 MiB. This is an output-validation bound, not a filesystem quota. A missing dependency, stalled runtime, wrong codec, empty/truncated output or process error must fail rather than silently reverting to screenshot sampling.

The legacy backend uses one Background OpenVR session with sequential screenshots. It permits at most 600 samples and 512 MiB of completed PNG pairs. The byte check occurs after a completed pair, not as a hard asynchronous disk quota. Its native watchdog, encoding, probing and transfer deadlines remain separate.

Only video and metadata are fetched; detailed logs and any legacy PNGs stay on the capture host. All can contain private screens. Keep artifacts out of Git.
