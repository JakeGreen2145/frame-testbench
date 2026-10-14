# Fixed-window passive controller action observer

`frame-controller-actions` keeps one background OpenVR client and one Testbench action manifest open while it samples right A and thumbstick state. It is a separate executable; it does not replace either observer or the input proxy. It produces no controller input and never requests focus, changes priority, assigns hands, submits frames or hides the dashboard.

Build and run on Linux:

```sh
sh scripts/build-controller-actions.sh
./build/frame-controller-actions "$(pwd)/resources/input-actions.json" > PRIVATE_FRESH_CAPTURE.jsonl
```

The required argument is an absolute file named `input-actions.json`. Its bytes and the sibling `frame-controller-bindings.json` must match the reviewed repository resources embedded at build time. CRLF/LF differences are normalized. Missing, nonregular, symlinked, oversized or edited resources fail before SDK initialization. The generator also verifies SHA256 pins before compiling. The build needs Python3, C++17 and the repository's public OpenVR header; it has no SteamVR link-time dependency. Stage the native source, SDK header, generator, build script, both resources and vendor header in their existing relative layout for a one-off aarch64 build. Never deploy a host x86-64 binary to the headset.

The sampling window is fixed15 seconds, at most20Hz and300 samples, with no catch-up bursts. A fixed20-second signal deadline covers file validation, SDK loading, manifest setup, all reads and shutdown. Samples are complete JSONL records written immediately without C++ stream buffering. The final `end` record appears only after successful SDK shutdown. A timeout exits124 and reports an unknown result; already flushed samples remain available. SDK/output errors exit1. Drain stdout continuously or redirect to a private regular file; an undrained pipe can cause an explicit output failure.

Each sample includes observer PID, sequence, UTC and monotonic elapsed time, action-update error, application input availability before/after, dashboard visibility before/after, local scene focus PID before/after and bounded scene app key/error. Right A reports individual error/active/value/changed/update-time and origin; axis0 reports individual error/active/vector/origin. Origin handles remain decimal strings, with actual device index and a bounded serial/property error. Inactive or failed actions keep values/origins null. Nonfinite numbers remain null. `input_available` is the after-read value; `input_available_before` is the earlier value. These separate reads cannot prove uninterrupted context between them.

An optional `--bindings` adds passive effective-binding queries in the same SDK client and manifest lifetime:

```sh
./build/frame-controller-actions "$(pwd)/resources/input-actions.json" --bindings > PRIVATE_FRESH_CAPTURE.jsonl
```

At sample sequences 0, 100 and 200, each action receives one `GetActionBindingInfo` call with 16 structs and one `GetActionOrigins` call with 64 handles. There are no retries or displays. The fixed sampling and process deadlines still apply. Default mode does not make these calls. This diagnostic mode produces extra event types and is intended for passive capture; mutation coordinators expecting only start/sample/end must use default mode.

`binding_query` records observer PID, associated sample sequence, action, native errors, capacities and returned binding count. Counts outside the fixed array are not consumed; reaching capacity does not prove completeness. `binding_entry` records each successfully returned device/input path, mode, slot and source type. The SDK's fixed 128/32-byte fields must terminate within their buffers; malformed fields stay null with a validation error. `binding_origin` records each nonzero handle returned on success, its device index, bounded serial and bounded component name with errors. Handles remain decimal strings. Origin-list completeness stays unknown because the API supplies no returned count. An origin on this list is a candidate mapping, not an active action origin or receipt.

Metadata events precede their associated sample. That sample's before/after context readings enclose the added queries; they do not prove uninterrupted context. No extra SDK process, producer input, focus request, priority change, binding display or manifest update occurs. Use the action state and producer correlation in the associated sample independently of mappings.

## Optional trigger observation

`--trigger` resolves the existing `/actions/observe/in/right_trigger_value` from the same pinned manifest and reads its analog data through the same right-hand source and SDK client. Resources and their byte pins remain unchanged. The scalar uses the SDK's X value. Default mode resolves no trigger handle, performs no trigger query and emits no trigger field.

```sh
./build/frame-controller-actions "$(pwd)/resources/input-actions.json" --trigger > PRIVATE_FRESH_CAPTURE.jsonl
./build/frame-controller-actions "$(pwd)/resources/input-actions.json" --trigger --bindings > PRIVATE_FRESH_BINDINGS.jsonl
```

Trigger mode adds `trigger_diagnostic=true` to the start record and `right_trigger` to each sample, with native error, active state, scalar value and exact origin information. Inactive/error/nonfinite values stay null. Existing before/after app, scene, dashboard and input availability context encloses this read. When `--bindings` is also selected, the same three sample-sequence snapshots include the trigger action with the existing 16-binding/64-origin capacities. Options may appear in either order; unknown or duplicate options fail before SDK initialization. No trigger components or bindings are created or changed.

The trigger scalar's local action identity does not establish a Windows legacy axis index, its type or original game input. Those remain independent receiver observations. Existing mutation coordinators keep their unchanged default sampler. Any explicitly selected trigger coordinator must require a fully active exact-origin neutral trigger baseline and keep receipt unknown until a separately verified receiver observes the bounded scalar and its release. The observer produces no input.

## Optional dual-stick observation

`--dual-stick` resolves the existing `/actions/observe/in/left_thumbstick_position` and `/user/hand/left` in the same SDK lifetime. It retains right A and right thumbstick reads through the existing right source. It changes no resources, pins, bindings, roles or producer input. Default mode and standalone `--trigger` mode make no left action/source query and emit no left fields.

```sh
./build/frame-controller-actions "$(pwd)/resources/input-actions.json" --dual-stick > PRIVATE_FRESH_DUAL_STICK.jsonl
./build/frame-controller-actions "$(pwd)/resources/input-actions.json" --dual-stick --trigger --bindings > PRIVATE_FRESH_BINDINGS.jsonl
```

The start record adds `dual_stick_diagnostic=true`; samples add `left_axis0` and `left_producer`. The left action has its own native error, active flag, XY vector and actual origin. Inactive actions have `active=false` with null vector/origin; SDK errors have null active/vector/origin. Each nonfinite vector component remains null. Left and right source handles must be nonzero and distinct; a zero or aliased left action handle fails setup before sampling.

Only dual-stick mode adds actual device class/role and index validation to action origins, including the existing right records. An out-of-range index stays null with `device_index_out_of_range`; SDK failures retain their native error and null device fields. Origin and device-path handles remain uint64 decimal strings. Foreign origins, invalid roles, generic trackers and failed serial reads remain visible as observations. A left-restricted query is not proof of a left-controller origin; compare its index, class, actual role and bounded serial with the producer separately.

`left_producer` identifies the exact synthetic-left serial among the same at-most-64 controller indices, records actual role and connected/valid standing pose, and selects no device when missing or duplicated. Producer lookup performs one bounded scan for each side; no retries or additional SDK client are introduced. Sequential producer/origin reads are not an atomic identity snapshot.

With `--bindings`, the left action is included at the existing sample sequences 0, 100 and 200. At most four actions are queried per sequence when all options are selected, using the same fixed 16-binding/64-origin buffers. The 15-second, 20Hz, 300-sample window and whole-process 20-second deadline remain unchanged. Existing mutation coordinators do not opt into this mode. Receipt and forwarding stay unknown; separate Windows and original-game observations are required.

The known Testbench synthetic-right producer is independently identified by exact serial among at most64 controller-class devices each sample. Its current actual role and standing connected/valid pose are recorded. Missing or duplicate identities produce no selected producer. Each serial/app-key read uses one256-byte buffer without retries; malformed/error strings remain unknown. Serial identity is an experimental test correlation and does not identify a remote Windows receiver or game consumer.

`IsInputAvailable` applies to this observer application's input context. It does not report raw component validity. A hand-restricted action can select a physical controller or change origins; the action origin must match the recorded synthetic producer in the same window. `input_receipt` and `controller_forwarding` always stay unknown. The helper emits observations, never a readiness marker, producer action, gameplay pass or physical gate.

For one explicitly selected root-owned probe, first establish the fresh stream receipt, observed hidden dashboard, current local scene consumer, live lease and distinct connected synthetic-right pose. Keep the observer process open. An external coordinator may select one500ms A down/up only after at least three consecutive fresh samples show available input, hidden dashboard, unchanged nonzero scene PID and matching app key, active neutral A from the exact unique synthetic producer, and its valid connected pose. If any precondition is unavailable or origin changes, stay passive or release immediately. Avoid another property/status/input SDK process during the window. The existing proxy-only button and lease-renew commands do not create an SDK client. Producer release/right-only cleanup remains the coordinator's responsibility.

Controlled checks compile this executable against a public-header fake SDK whose unused methods abort. They verify one Background/manifest lifetime, default action-set priority, scene/dashboard/origin changes, inactive/null states, malformed bounded metadata, duplicate identities, pre-initialization resource rejection and preservation of flushed samples when SDK shutdown hangs. Opt-in checks verify fixed binding/origin capacities and call counts, invalid returned counts, unterminated fields, native errors and uint64 handle preservation. Default-mode checks prohibit binding queries:

```sh
python3 -m unittest discover -s tests -p test_controller_actions.py -v
```

These checks load no SteamVR. A successful observation window is not input delivery. Runtime observations of same-origin neutral/pressed/released transitions, Windows controller receipt and original game response remain separate results.
