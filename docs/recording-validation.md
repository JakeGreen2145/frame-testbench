# Recording verification

Verified on a Steam Frame development headset on 2026-10-07. Actual recordings and decoded images remain in ignored artifacts rather than in this public repository.

## Real compositor pixels

- CLI request: six-second stereo capture at requested five samples/sec. Result: 20 completed screenshot samples, measured 3.277 samples/sec, 5.999-second H.264 MP4, 1920x1080. Decoded pixels show the Frametop settings overlay in both eyes against SteamVR's environment.
- MCP request: eight-second stereo capture at requested five samples/sec. Result: 24 completed samples, measured 3.025 samples/sec, 7.804-second H.264 MP4, 1920x1080, 3,279,172 bytes. The official Python SDK client received local video/timeline resource links after SSH fetch.
- While that MCP recording was active, the same MCP session set synthetic HMD yaw to +15, -15, then 0 degrees. Each input call finished in approximately 0.35 seconds with the recording still pending. Decoded samples at roughly 0, 2, 4, and 6 seconds show the overlay shifting across the stereo view and returning, rather than a repeated still image.
- FFmpeg decoded the entire MP4 successfully. Its 25 decoded frames include the final duration terminator. Video duration and source sample count are separate fields; no headset-rate or exposure-time claim follows from them.

The physical headset was unworn before synthetic pose changes. The recorder itself does not activate overrides. Final readback confirmed HMD pose/proximity overrides released and automatic compositor standby restored. SteamVR and SSH remained active. The recorder update did not require a driver install or runtime restart.

## Transport regression found during acceptance

The first concurrent MCP trial ended with a closed protocol connection even though the remote recording completed. The CLI subprocess inherited the server's stdin, allowing SSH to consume incoming MCP messages. A real subprocess regression reproduced this by passing a sentinel through server stdin and observing it in the CLI's child. Connecting the child stdin to DEVNULL made that regression pass and allowed the live concurrent motion recording above to complete. Sequential SDK calls and a mocked CLI runner had not exposed this condition.

## CPU coverage

Native tests use a public-header-derived fake OpenVR runtime to exercise a single SDK session, complete PNGs, delays, pacing without catch-up bursts, argument/output validation, partial failure, byte/sample limits, and process deadlines. Python tests encode and decode generated color PNG fixtures with real FFmpeg and verify variable timing, source changes, directory quoting, failure retention and strict SSH fetch. MCP tests cover typed bounded arguments, owned resource paths, bounded file reads, concurrent input, cancellation, and actual SDK stdio exchanges.

The public GL mirror probe returned InvalidTexture with a current EGL/OpenGL context on the tested Frame. This implementation therefore makes no continuous-mirror claim. Screenshot sampling has measurable overhead and is unsuitable for certifying application frame pacing or motion-to-photon latency.
