# Continuous recording integration validation

## Scope

The continuous `headset` backend was exercised through the actual official-Python-SDK MCP stdio client, frame-testbench CLI, existing SSH transport, Framecorder's released ARM64 executable, SteamVR's headset-view texture and the Frame's Qualcomm hardware encoder. This is distinct from CPU fixture tests and the [earlier screenshot recorder](recording-validation.md).

The dependency is pinned to Framecorder `v0.1.1`. Archive SHA-256:

```text
426c2c9e5c4d0489d477122995cdec950e43ec82cfe9a9f40dfb0294779b1e63
```

Extracted recorder SHA-256:

```text
631e20aff5a91d124fd2780aaa8096898d9ecf8c385d068c8853e54cf1c1bb38
```

The explicit setup script completed on the Frame and the executable's hash and `--version` were read back. No privileged helper, capabilities, services or runtime restart were needed.

## Live MCP default

The client discovered the real `record` schema and asserted `view="headset"` and `fps=30` defaults, then invoked:

```json
{"duration_seconds":8}
```

Result:

| Evidence | Observed |
|---|---|
| Metadata | `source=openvr_headset_view`, `backend=framecorder`, `view=headset` |
| Video | One silent H.264 stream, 1920×1080 |
| Decoded frames | 241 |
| Stream duration | 8.033333 seconds |
| Average encoded frame rate | 30/1 |
| File size | 11,527,603 bytes |
| Artifacts returned | MP4 and timeline JSON as actual MCP resource links |
| Capture-host PNGs | None |

While the recording was pending, the same MCP session issued synthetic yaw +15°, −15°, and 0°. Those calls completed in 0.334–0.345 seconds, each before recording completed. Physical proximity was checked before the test and before each motion. The decoded contact sheet visibly shows the spatial Frametop overlay shifting with those poses, correctly oriented. All 241 decoded hashes differed; that is supporting evidence, not by itself proof of source freshness.

Framecorder's performance summary reported average CPU 1.4% of one core (maximum 2.0%), shader conversion 0.48 ms/frame, and process RAM 55 MB. Its intermediate 151-frame report showed zero drops. These are recorder-reported values in a light scene, not independent whole-session profiling; they exclude the wrapper's later ffprobe decode validation. No heavy-game, optical timing or passthrough-camera claim follows from them.

The headset recording directory contained only video, JSON, logs and the performance CSV. There were no PNG files or screenshot sequence directories.

## Live CLI at 60 requested FPS

With the same test session still active:

```sh
./frame-testbench --host frame record --duration 3 --fps 60 \
  --fetch artifacts/FRESH_DIRECTORY
```

The actual CLI fetched a 1920×1080 H.264 MP4 containing **181 decoded frames**, duration **3.033333 seconds**, and average encoded rate **5430/91**, approximately **59.67 fps**. This is the observed short-run result, not an assertion of perfect source cadence or a sustained 60-fps gameplay benchmark.

## Tests and cleanup

- Integrated local `make test`: **174 tests, no failures or skips**, with the official MCP SDK installed.
- Native Frame `make test`: **174 tests collected, 31 optional MCP tests skipped**, no failures. Those skips do not replace the actual SDK-on-host → SSH → headset test above.
- The full suite retains legacy screenshot encoding, timeline, transport, pose, input and native adapter coverage.
- New coverage includes no implicit install/runtime launch on preflight failure, exact recorder arguments, missing dependencies, fresh-output refusal, actual synthetic-video decoding and packet timestamps, wrong codec/audio/truncation/duration failures, isolated stdin, bounded timeout and child cleanup, and explicit installer checksum/member validation.
- After testing, input overrides were released and automatic standby restored, with status readback confirming both. SSH and SteamVR services remained active. No `framecorder` or `ffmpeg` process remained at the health check.

Private recordings, runtime identifiers and logs remain in ignored artifacts. Continuous headset-view capture is not full panel scanout, and observed Frametop coverage is not a guarantee for every overlay or camera layer.
