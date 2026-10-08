# Continuous capture backend research

## Decision

Use **Framecorder's rootless SteamVR headset-view backend** for normal recording. It acquires the `system.HeadsetView` Vulkan texture, GPU-copies it to an owned image, converts/scales into NV12 on the GPU, and passes DMA-BUFs to Qualcomm Iris's V4L2 hardware encoder. Raw video pixels do not make a CPU round-trip. This avoids screenshot requests, PNG encoding/writes, later PNG decoding, and CPU H.264 encoding.[1][2]

Frame Testbench invokes a separately installed, pinned upstream executable. It does not vendor a second Vulkan/Qualcomm encoder implementation, run the upstream installer, enable services, install the privileged panel helper, or open a network listener. The dependency is MIT-licensed; its notice is preserved in [framecorder-LICENSE.txt](framecorder-LICENSE.txt).[3]

The `headset` view is **one undistorted left-eye image**, not stereo scanout or a full optical view. The hardware check below establishes Frametop overlay coverage in that test scene; it does not establish every camera layer or overlay in every application.

## Projects compared

| Project / path | Acquisition and encoding | Decision |
|---|---|---|
| Framecorder, `--source headset` | `IVROverlayView_003::AcquireOverlayView`, key `system.HeadsetView`, Vulkan conversion, direct Iris V4L2 hardware encoder | Selected. Rootless, GPU-resident raw pixels, tested on this Frame. |
| Framecorder, panel source | DRM/KMS scanout framebuffer export → DMA-BUF → Vulkan lens correction → hardware encoder | Useful future stereo option, but needs `CAP_SYS_ADMIN` on an export helper. Not installed or selected implicitly. |
| Frame Control | Continuous `/dev/video99` V4L2 RGB capture → FFmpeg/libx264 → H.264 over SSH | Much better acquisition than PNG polling, MIT, but unnecessary CPU video encoding compared with the tested GPU path. |
| FrameMate | `/dev/video99` RGB frames → conversion → direct Iris V4L2 M2M encoding → fMP4/WebSocket | Hardware-encoded alternative, but CPU raw-frame path and a separate network service; GPL-3.0-or-later. No source copied into this MIT project. |

Sources are pinned at inspection, not treated as a promise that future SteamVR releases keep the same behavior.[1][2][4][5][6]

Frame Control's field notes identify `steamvr-v4l2cam.service` and `/opt/steamvr/bin/linuxarm64/v4l2cam --output=99` as the existing headset-view exporter. Its code separately identifies X11 app-panel capture. Thus `/dev/video99` is a VR-view lead, not merely desktop capture.[4][7] That route was source-inspected, not benchmarked here; no FrameMate or Frame Control service was installed.

## Why the GL mirror probe was insufficient

The earlier EGL/Zink `GetMirrorTextureGL` probe returned `InvalidTexture (102)`. That ruled out only that particular GL mirror call/setup. It did **not** rule out Vulkan texture sharing, the headset-view overlay, the runtime's V4L2 exporter, or KMS/DMA-BUF. Using repeated compositor screenshots as the default without investigating those alternatives was premature.

Framecorder uses public OpenVR overlay-view interfaces rather than `GetMirrorTextureGL`. The source audit checked the C function-table versions and LP64 layouts against Valve's header; the parent then verified the released ARM64 recorder on the actual headset. A research host lacking Cargo/glslc prevented a separate source build there; it did not prevent the verified upstream binary test.

## Live rootless hardware check

The dependency was fetched from release `v0.1.1` (source tag `8dbcf0c4683d1955197f365eca796e531bafe6fe`); both archive and executable SHA-256 values were checked. A source diff confirmed the audited main-pin capture, Vulkan, encoder, OpenVR and KMS modules were unchanged from that tag. Only the executable was extracted into the user-owned checkout. No archive installer was run.

Command, inside an already active test session:

```sh
framecorder --source headset --view eye --codec h264 \
  --fps 30 --duration 8 --bitrate 12 --no-audio recording.mp4
```

Actual result:

- H.264, 1920×1080, one video stream, no audio.
- FFprobe decoded **241 frames**, reported `30/1` average frame rate and **8.033333 seconds** stream duration.
- File size: **11,419,445 bytes**.
- All 241 decoded frame hashes differed. This alone is not proof of unique source frames; visual inspection also confirmed the spatial Frametop overlay moved with the +15°, −15°, and 0° synthetic yaw changes.
- Recorder's own performance log reported **1.5% of one CPU core** average, 2.0% maximum; **0.44 ms/frame** GPU conversion; **97 MB** process RAM. These are recorder-reported measurements, not an independent profiler.
- This was a light Frametop test scene, **not** a GPU-bound game benchmark, latency certification, passthrough alignment test, or proof of zero impact on rendering.
- Overrides were released and automatic standby restored after the hardware test. Recording itself does not perform those state changes.

Private video, logs, probe JSON and contact sheet remain in ignored artifacts, not this repository. The earlier screenshot path achieved approximately 3 samples/s in its documented live tests; the continuous path eliminates that pipeline rather than simply raising its requested sampling rate.

## Upstream caveats

Framecorder's main documentation initially characterized low-priority panel capture as almost free. Its development branch documents capture starvation/tearing when the compositor reuses the same scanout buffer under heavy GPU load, and changes GPU priority policy. Do not repeat blanket “zero impact” or “never torn” claims.[8] We use the rootless headset-view path, with its own GPU copy, and do not enable the privileged panel path.

Framecorder uses KMS vblank for pacing even for the headset-view source. An inactive display can stall acquisition; the wrapper must enforce an independent wall-clock deadline. `--duration` alone does not bound all initialization and teardown. Rootless capture still needs access to the Frame's DRM and encoder devices. It is Frame/SteamOS-specific, not a generic Linux desktop recorder.

## Primary sources

[1] Framecorder headset-view acquisition, pinned `171a905006bb917e5a1677fa922a1d00cc1ec214`: https://github.com/coah80/framecorder/blob/171a905006bb917e5a1677fa922a1d00cc1ec214/src/headset_view.rs

[2] Framecorder encoder and GPU pipeline: https://github.com/coah80/framecorder/blob/171a905006bb917e5a1677fa922a1d00cc1ec214/src/encoder.rs and https://github.com/coah80/framecorder/blob/171a905006bb917e5a1677fa922a1d00cc1ec214/src/gpu.rs

[3] Framecorder MIT license: https://github.com/coah80/framecorder/blob/171a905006bb917e5a1677fa922a1d00cc1ec214/LICENSE

[4] Frame Control capture implementation, pinned `551cc54bbc3fe1706360b0e6ee09a72b5ed57ce0`: https://github.com/saphid/frame-control/blob/551cc54bbc3fe1706360b0e6ee09a72b5ed57ce0/ui/server.py#L549-L592

[5] FrameMate capture implementation, pinned `816aff8902c3ed787524d2672d0da068c3b28816`: https://github.com/nailuj05/framemate/blob/816aff8902c3ed787524d2672d0da068c3b28816/crates/agent/src/stream.rs#L131-L201

[6] FrameMate Iris encoder: https://github.com/nailuj05/framemate/blob/816aff8902c3ed787524d2672d0da068c3b28816/crates/agent/src/encoder.rs#L54-L155

[7] Frame Control runtime field notes: https://github.com/saphid/frame-control/blob/551cc54bbc3fe1706360b0e6ee09a72b5ed57ce0/docs/how-the-frame-works.md#L31-L33

[8] Framecorder development-branch timing caveats, pinned `134cdb5645f721b65338909a9d0c2be8f63c6f9c`: https://github.com/coah80/framecorder/blob/134cdb5645f721b65338909a9d0c2be8f63c6f9c/docs/how-it-works.md
