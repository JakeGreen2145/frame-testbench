# Physical controller callback trace

This opt-in diagnostic tests whether actual controller registrations and input callbacks reach the wrapped `cv` provider context. It does not borrow physical controllers, change inputs or poses, select hands, or establish Windows stream forwarding. Synthetic controller behavior remains unchanged.

Set `FRAME_TESTBENCH_TRACE_CONTROLLERS=1` in the `vrserver` startup environment before the proxy is loaded. The exact value `1` enables tracing. Other values and an absent flag leave physical controller driver pointers untouched. This is a startup choice; `controller-trace` cannot enable it at runtime.

When enabled, real controller registrations made through the wrapped host receive a forwarding `ITrackedDeviceServerDriver` wrapper. All methods call the real device with the original arguments and return its exact result. `GetComponent` returns the original component pointer. `GetPose` returns the original pose unchanged. Registration's driver pointer necessarily changes while tracing is enabled; the serial and class passed to the real host do not change. Provider-owned synthetic registrations use the real host directly and are not wrapped or identified by a serial-name heuristic.

## Readback

After root-owned cleanup and a reviewed startup change, query:

```sh
./frame-testbench controller-trace
```

The command uses the existing private native socket. It performs no input, pose, property writes or lease renewal. It does not create a session directory or change the watchdog. It refreshes only known controllers' profile, controller-type and role-hint properties using bounded read-only queries, then returns a JSON snapshot. The activated property container is recorded before calling real `Activate`, so synchronous component creation and updates can be associated while activation is still pending.

Each record includes registration acceptance, activation generation, index/container, activation/deactivation counts and error, `GetPose` and host pose-publication counts, last observed connected/valid flags, bounded profile/type/serial diagnostic strings and property read errors. Each boolean/scalar component records path, handle, type/units, creation count/error, `creates_during_activation`, update count and last update error. A role hint of zero or a property error stays visible. Neither a hint nor serial is interpreted as a reliable hand selection.

Interface request counts and `alias_passthrough` describe exact public interface requests. An unknown alias returning an original host/input pointer is forwarded unchanged; it is not guessed to have a compatible interface version. An activated controller with missing component observations may be using a bypass, but missing data alone does not prove which path or process owns it.

Storage is bounded to four registration records, 32 component records per container, eight pending unmatched containers, and 96 bytes per diagnostic text prefix. Overflow increments dropped counters and leaves real SDK calls unaffected. Text is escaped for valid JSON; non-ASCII bytes are represented as byte escapes. A reply exceeding 60,000 bytes instead returns `response_truncated: true` with an empty device list and an explicit diagnostic. Truncated data must not be treated as complete. Activation starts a new generation and clears that generation's component map. Deactivation stops index-based association; cleanup clears the trace. Serial prefixes can be truncated and are never selectors.

The HMD override's proven push-publication path is separate. Its current `GetPose` implementation forwards the real driver. This trace makes no inference that controller pose pushes, sampled poses or input actions are transported to Windows in the same way.

## Controlled checks

Run on a local Linux build machine without SteamVR. A short Unix-socket scratch path is required; use `/tmp` when the source checkout has a long path.

```sh
TMPDIR=/tmp python3 -m unittest discover -s tests -p test_controller_trace.py -v
TMPDIR=/tmp python3 -m unittest discover -s tests -p test_input_proxy.py -v
```

The tests load the production proxy and loader with a fake `cv` provider and fake runtime. They cover default-off pointer identity, synchronous creation before `Activate` returns, pending pre-activation association, native input-interface alias bypass, lifecycle generations, late role values, activation errors, all forwarding methods, unchanged physical pose/input values, component/registration/pending overflow, and readback without SDK mutations or lease access. They do not use a live OpenVR API library, SteamVR service, SSH or headset.

## Root-owned staging and rollback

No installation or restart is performed by this change. The built WSL test artifacts are Linux x64 and must not be deployed to the ARM64 headset. Root must build the reviewed source natively for the headset only after existing game, stream, lease and observer cleanup is confirmed.

Before staging, create a private receipt recording the source revision and the existing managed `90-frame-testbench.conf` bytes/hash, resolved loader and proxy paths and both SHA-256 hashes. Preserve the original owned drop-in bytes in that receipt's backup directory. Confirm the existing ownership marker. Do not dump unrelated service environment values into evidence.

Use the existing installer without `--restart` to stage a new immutable content-addressed library directory and managed `90-frame-testbench.conf`. Record the returned directory/drop-in paths, staged drop-in hash, loader hash and proxy hash in the receipt. The install directory is derived from the combined library content hash; it does not itself record the source revision. Never overwrite a loaded `.so`.

Enable the startup flag in a fresh, separately owned `91-frame-controller-trace.conf` containing:

```ini
# Managed by frame-testbench controller trace.
[Service]
Environment="FRAME_TESTBENCH_TRACE_CONTROLLERS=1"
```

Record its exact path and hash in the receipt and refuse an existing unowned file. Root can then reload the user service configuration and restart SteamVR after confirming cleanup. Collect `controller-trace` alongside the independent property observer. A valid proof requires actual registered/activated controller records and component/publication callbacks associated with their exact containers and generations. Unknown or zero records are a diagnostic result, not permission to implement borrowing.

For rollback, verify the current trace drop-in still matches its owned receipt, then remove only that file. Restore the original `90-frame-testbench.conf` bytes only when the current file matches the receipt's staged hash; preserve external edits and report a conflict otherwise. Verify the original proxy/loader hashes before selecting them. Reload user service configuration and restart SteamVR under root's same cleanup rule. Confirm `controller-trace` reports disabled when using the trace-capable build with the flag removed, or is unavailable with the original build. Finally confirm physical devices and input still work through independent readback. Keep staged library directories and receipts for inspection until rollback is verified.

These operations affect only the owned user service drop-ins and content-addressed testbench libraries. They must not edit `/opt`, vendor properties, controller identities, SteamVR machine settings, SSH or networking.
