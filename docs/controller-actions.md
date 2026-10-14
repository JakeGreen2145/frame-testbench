# Fixed-window passive controller action observer

`frame-controller-actions` keeps one background OpenVR client and one Testbench action manifest open while it samples right A and thumbstick state. It is a separate executable; it does not replace either observer or the input proxy. It produces no controller input and never requests focus, changes priority, assigns hands, submits frames or hides the dashboard.

Build and run on Linux:

```sh
sh scripts/build-controller-actions.sh
./build/frame-controller-actions "$(pwd)/resources/input-actions.json" > PRIVATE_FRESH_CAPTURE.jsonl
```

The only argument is an absolute file named `input-actions.json`. Its bytes and the sibling `frame-controller-bindings.json` must match the reviewed repository resources embedded at build time. CRLF/LF differences are normalized. Missing, nonregular, symlinked, oversized or edited resources fail before SDK initialization. The generator also verifies SHA256 pins before compiling. The build needs Python3, C++17 and the repository's public OpenVR header; it has no SteamVR link-time dependency. Stage the native source, SDK header, generator, build script, both resources and vendor header in their existing relative layout for a one-off aarch64 build. Never deploy a host x86-64 binary to the headset.

The sampling window is fixed15 seconds, at most20Hz and300 samples, with no catch-up bursts. A fixed20-second signal deadline covers file validation, SDK loading, manifest setup, all reads and shutdown. Samples are complete JSONL records written immediately without C++ stream buffering. The final `end` record appears only after successful SDK shutdown. A timeout exits124 and reports an unknown result; already flushed samples remain available. SDK/output errors exit1. Drain stdout continuously or redirect to a private regular file; an undrained pipe can cause an explicit output failure.

Each sample includes observer PID, sequence, UTC and monotonic elapsed time, action-update error, application input availability before/after, dashboard visibility before/after, local scene focus PID before/after and bounded scene app key/error. Right A reports individual error/active/value/changed/update-time and origin; axis0 reports individual error/active/vector/origin. Origin handles remain decimal strings, with actual device index and a bounded serial/property error. Inactive or failed actions keep values/origins null. Nonfinite numbers remain null. `input_available` is the after-read value; `input_available_before` is the earlier value. These separate reads cannot prove uninterrupted context between them.

The known Testbench synthetic-right producer is independently identified by exact serial among at most64 controller-class devices each sample. Its current actual role and standing connected/valid pose are recorded. Missing or duplicate identities produce no selected producer. Each serial/app-key read uses one256-byte buffer without retries; malformed/error strings remain unknown. Serial identity is an experimental test correlation and does not identify a remote Windows receiver or game consumer.

`IsInputAvailable` applies to this observer application's input context. It does not report raw component validity. A hand-restricted action can select a physical controller or change origins; the action origin must match the recorded synthetic producer in the same window. `input_receipt` and `controller_forwarding` always stay unknown. The helper emits observations, never a readiness marker, producer action, gameplay pass or physical gate.

For one explicitly selected root-owned probe, first establish the fresh stream receipt, observed hidden dashboard, current local scene consumer, live lease and distinct connected synthetic-right pose. Keep the observer process open. An external coordinator may select one500ms A down/up only after at least three consecutive fresh samples show available input, hidden dashboard, unchanged nonzero scene PID and matching app key, active neutral A from the exact unique synthetic producer, and its valid connected pose. If any precondition is unavailable or origin changes, stay passive or release immediately. Avoid another property/status/input SDK process during the window. The existing proxy-only button and lease-renew commands do not create an SDK client. Producer release/right-only cleanup remains the coordinator's responsibility.

Controlled checks compile this executable against a public-header fake SDK whose unused methods abort. They verify one Background/manifest lifetime, default action-set priority, scene/dashboard/origin changes, inactive/null states, malformed bounded metadata, duplicate identities, pre-initialization resource rejection and preservation of flushed samples when SDK shutdown hangs:

```sh
python3 -m unittest discover -s tests -p test_controller_actions.py -v
```

These checks load no SteamVR. A successful observation window is not input delivery. Runtime observations of same-origin neutral/pressed/released transitions, Windows controller receipt and original game response remain separate results.
