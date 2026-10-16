# Persistent recorder sessions

This is an additive, test-only Linux CLI. It does not change the existing `record`
command's 1..60-second contract, install anything, start SteamVR, or own input or
power overrides. Coordinate one recorder per headset externally. There is no new
service, MCP tool, game action, observer cadence, or implicit gameplay gate.

```
./frame-testbench record-session --duration 330 --segment-seconds 50 --fps 30 --output /fresh/private/session
./frame-testbench verify-record-session --output /fresh/private/session
```

Both commands also accept the existing global `--host` / `--remote-root` SSH
transport. A remote call's local exit or timeout does not prove remote closure.
Read the remote durable receipt. These commands do not implicitly end or renew a
testbench lease. The caller must maintain its independently bounded lease and
must not end the native session until **all** its current clients have closed.
No automatic fetch is added. Files can contain private screens.

## Runtime bounds

`duration` is a whole-operation monotonic budget, integer 1..330, not a promise
of that many seconds of recorded media. `segment-seconds` is an integer 1..50,
default 50. Requested FPS is 1..60. At most seven MP4 paths are generated with a
session nonce inside a newly created mode-0700 directory. Existing directories,
symlink ancestors, line/control characters and oversized command paths refuse.
Caller-supplied filenames or native control commands are not accepted.

One `frame-observe status` precheck runs in its own process group. Its total
budget is at most three seconds, with up to two seconds for normal completion
and a final bounded SIGKILL/reap if needed. A successful typed JSON response,
actual exit zero, drained pipes and a fresh complete group census are required
before the one Framecorder process is dispatched. No other SDK query occurs.

Framecorder is launched with `--control`, a generated initial output, explicit
left-eye HeadsetView, H.264, no audio, and its native duration backup. The wrapper
rotates with `stop-record` followed by `record GENERATED_PATH`. Commands are
bounded nonblocking pipe writes. The initial recording acknowledgment and first
five-second progress message must arrive within 20 seconds; later recording and
saved-file acknowledgments must arrive within four seconds. A progress gap over
12 seconds without a strictly advancing positive cumulative frame count refuses.
Zero/repeated counts cannot establish readiness or refresh progress. Regressing
counts, malformed messages and nonfinite/negative FPS refuse. These are admission
deadlines, not measured performance.
Warnings/errors, unexpected/duplicate/out-of-order acknowledgments, malformed
output, partial files and output bounds retain a sticky first failure.

Rotation time is measured from the request, not acknowledgment receipt, so a slow
acknowledgment cannot silently extend the segment. The wrapper reserves six
seconds before the aggregate deadline: normal `quit` plus EOF has a three-second
grace, followed by the unchanged SIGINT / SIGTERM / SIGKILL one-second stages.
Normal quit and EOF **do** set Framecorder's capture STOP flag. They are stop
requests, not closure evidence. Interrupted or failed sessions use the same
bounded cleanup and never earn media success. A native stall or uninterruptible
filesystem operation can still leave closure unknown; it is never reported as
absence merely because a local SSH child exited.

Both process attempts have a durable write-ahead entry before `Popen`. The
attached entry records executable hash, exact argv, PID, `/proc` start ticks,
PGID and SID. Normal completion requires the source-pinned `done:` footer,
actual exit zero, drained stdout/stderr, and a fresh complete `/proc` group
census. An abnormal exit can still prove runtime closure through actual exit
and full exact owned-group absence, while keeping the capture failed. Permission
errors, PID reuse, unexpected group identity, ambiguous dispatch and incomplete
census refuse. Each census is limited to 16,384 entries and half a second.
This relies on the pinned recorder's process model, not protection against a
privileged process escaping its session or rewriting private receipts.

## Receipts and verification

`session.json` is an atomically replaced, fsynced ledger. `events.jsonl` contains
fsynced typed precheck, ready, progress, command intent/write, segment, stop and
completion events with a nonce, UTC and monotonic receive timestamps. Native
stdout/stderr are retained separately, each bounded to 8 MiB and 64 KiB per
line. A bound violation fails and records that the raw output was truncated.
There is no claim of complete raw retention after that bound. Segment hashes
are computed after native closure, with the remaining aggregate deadline checked
between 64-KiB reads. Exhausting that budget fails rather than omitting the hash.

A successful capture returns `runtime_closed: true`, `ok: true`, and
`media_verified: false`. CPU decoding is deliberately deferred until the runtime
clients have closed. `verify-record-session` refuses incomplete closure, changed
ledger or file identities/hashes, foreign or partial paths, and more than seven
segments or 512 MiB total. It checks each actual H.264/no-audio file with ffprobe,
including decoded frame counts, strictly increasing finite packet PTS and
container/packet duration agreement. Each encoded segment must be **at most
60 seconds**, without tolerance above 60. An overlong file is preserved but
never qualifies. Verification records diagnostics in fresh subdirectories and
does not rewrite the capture manifest or media. It produces a distinct
`verification-UUID.json` only after all files pass.

Packet PTS describe the encoded left-eye HeadsetView. They do not establish final
stereo panel pixels, exposure time, fresh compositor frames, continuous coverage
across rotations, original game response, or purposeful gameplay time. Media
gaps, input-lifetime admission and the 180-second gameplay requirement remain
external decisions. There is no fixed controller-observer cadence in this API.

## Source and remaining runtime check

The control contract was read from Framecorder v0.1.1:
[control parser](https://github.com/coah80/framecorder/blob/v0.1.1/src/control.rs),
[capture/control shutdown](https://github.com/coah80/framecorder/blob/v0.1.1/src/main.rs),
and [mux acknowledgments/writer join](https://github.com/coah80/framecorder/blob/v0.1.1/src/mux.rs).
Reviewed main/control/mux SHA256 values are respectively
`459fc2f324a96afba97f395fce36ae71358d935b2d48db222d17f42409258f0e`,
`ad3f578f58a04936477cfbb888d4e6c1265b0d4861512053a74c04738eb4575b`,
and `9f4395f8385624eceb446ef8f520973c04f00c5a9cf0ae2b387dfd8d650ed87b`.
The tested release executable is SHA256
`631e20aff5a91d124fd2780aaa8096898d9ecf8c385d068c8853e54cf1c1bb38`.
The existing explicit executable override remains available for controlled CPU
fixtures; the ledger records the actual executable hash, not an inferred match.

CPU tests exercise fake subprocesses and synthetic encoded video only. Actual
Framecorder rotation/readiness cost, source/host coexistence, segment gaps,
long-session resource use and normal shutdown latency still require a separately
authorized physical run. No gameplay integration or hardware qualification is
implied by this implementation.
