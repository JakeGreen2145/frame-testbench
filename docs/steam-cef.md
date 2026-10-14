# Experimental Steam CEF stream provider

This optional Testbench provider uses the Steam Frame's already exposed local Steam debug context to request a stream from one selected paired Windows PC. It is experimental automation of an observed internal Steam UI control. Normal UGEVR use remains independent of Testbench.

The default streamed-run mode stays assisted. Select `--experimental-steam-cef` explicitly to activate the provider. The separate `--hide-steam-dashboard` option dismisses the dashboard after game readiness. No debug listener, service, pairing or setting is changed by these flags.

## Preconditions and controls

The headset SSH target is fixed by configuration. Steam must already expose its debug inventory on headset loopback `127.0.0.1:8080`. The provider requires exactly one page titled `SharedJSContext`, verifies its loopback WebSocket URL and handshake, and checks the expected Steam globals. It does not enable debugging or connect to arbitrary endpoints.

The expected PC name must match exactly one device. That device must advertise VR capability. The provider preserves its uint64 client ID as a decimal string, rejects unsafe JavaScript numeric identities, and binds later replies to that ID. It neither enumerates nor exports account identities, tokens, page URLs or full Steam state.

The source-backed request is `SteamClient.Apps.StreamGame(250820, device.clientId, -1)`. App250820 is fixed as SteamVR. The provider rechecks the unique selected peer, client ID, VR capability and absence of any active stream in the same synchronous expression immediately before one call. It never overwrites another stream. An already observed stream for the intended peer and SteamVR is reused without a connection call and remains unowned.

After a new request, the provider polls `RemotePlayStore_SteamUI.remoteClientStreams` for that exact client ID and SteamVR app. Request completion and association remain separate fields. The observed direct Frame path had generic session IDs of zero even when the Windows HMD was connected, so those generic IDs are not a readiness requirement.

Dashboard dismissal uses the observed no-argument `SteamClient.OpenVR.VROverlay.HideDashboard()` control. It requires only the selected SteamVR stream, issues at most one hide call, awaits its result and polls the awaited `IsDashboardVisible` query. The observed native request propagated after its initial response, so a completed request cannot itself mean the dashboard is hidden.

Owned disconnect requires a fresh private ownership receipt. After a new connection request produces one specific stream row, the provider records that exact row object and its raw native argument tuple in a private, bounded receipt map inside SharedJSContext. Only the opaque nonce leaves that context. A fresh stop expression checks the expected peer, sole matching stream, the exact row object and unchanged raw arguments before one `StopRemoteClientStream` call. Replaced rows, changed arguments, another stream, missing receipts and reused streams are refused. A repeated pending cleanup polls without issuing another stop call.

The result reports `association_removed` or `pending`; its Windows runtime state remains `unknown`. A coordinated private probe removed the specific row through twenty seconds. Windows initially still exposed a connected HMD, then independently reported its runtime closed after propagation. Association removal and host runtime closure must therefore remain separate observations. Ending a Testbench lease or stopping the game proves neither.

The provider does not kill or restart a waiting Windows SteamVR runtime. A manual probe showed that an already waiting directly launched runtime could prevent the request from connecting. That condition needs an explicit ownership-aware remedy in the host workflow. No automatic process or service cleanup is inferred from it.

## CLI and integration

Read-only observation:

```text
frame-testbench-steam --host frame --expected-peer my-paired-pc observe
```

Explicit experimental connection and dashboard dismissal:

```text
frame-testbench-steam --host frame --expected-peer my-paired-pc --experimental connect
frame-testbench-steam --host frame --expected-peer my-paired-pc --experimental hide-dashboard
```

To retain ownership across standalone CLI processes, select a new private `--receipt-file` during connection and use it for explicit disconnect:

```text
frame-testbench-steam --host frame --expected-peer my-paired-pc --experimental --receipt-file artifacts/owned-stream.yaml connect
frame-testbench-steam --host frame --expected-peer my-paired-pc --experimental --receipt-file artifacts/owned-stream.yaml disconnect
```

The receipt is an opaque control handle, stays local and is not printed or included in ordinary result metadata. It contains no account IDs. Loading it requires a fresh context receipt match; a local file alone grants no ownership. Reuse creates no receipt, and an unrelated existing receipt file is not replaced. A missing receipt produces an unowned result without a stop call.

The checkout equivalent is `python -m frame_testbench.steam_cef`. Its output is a bounded flat YAML mapping. A request flag and specific stream association cannot establish game image or input evidence. They leave both-eye and Windows input results unknown. Exit4 reports a timed-out observation, pending cleanup or unowned disconnect, and exit2 reports validation or transport failure.

`SteamCEF` is the typed Python provider. `Orchestrator(stream_provider=provider)` begins its lease, requests or reuses the stream, then launches the external UGEVR controller. A timed-out stream association stops this path before game launch. The orchestrator records the experimental mode, request, uint64 client ID, specific association, ownership and dashboard visibility separately. Independent observers still own both-eye run markers and Windows action receipt. Capture chunks recheck stream association.

SSH runs a fixed Python worker supplied by this package on stdin. The worker has only `observe`, `connect`, `hide-dashboard` and receipt-guarded `disconnect` operations and uses the existing loopback context. Arguments use explicit SSH host validation and shell quoting. Replies contain selected capability/state flags and opaque client identity only. Diagnostic codes exclude native exception text and arbitrary page output. Socket reads have wall-clock deadlines and size limits; the SSH child has a deadline and cancellation. Cancellation can leave a request outcome unknown. It never manufactures a stopped stream or kills SteamVR to compensate.

## Evidence and limits

The implementation uses newly authored expressions and contains no copied shipped JavaScript. The installed source inspected for the host card and SteamVR constant had these hashes:

- Steam UI `sp.js`: `59c97d98b0b3eaa28e2e677209738a6d075ecaefc13f3f6e13d7371332163956`.
- Steam UI `library.js`: `a2228603cc8a993f2340cb80f89e39b4748473207ec11709a62e0e51fb1d83e1`.

The source host card's advanced stream-VR-without-game control invokes the fixed request above. Its return-to-running-app and cancel handlers invoke the dashboard hide operation. These internal calls are not documented public Valve automation APIs. Steam updates can change their context or semantics. Missing or changed fields are reported as failures.

Coordinated runtime probes executed one request with a waiting Windows runtime and one after explicit host cleanup. The second produced a matching SteamVR stream entry through twenty seconds. Separate Windows observation confirmed a valid VRLink HMD and causal forwarding of a distinctive six-axis pose. One coordinated dashboard hide request later changed awaited visibility from true to false. These observations justify this experimental attempt. They do not prove both-eye gameplay, optical behavior, controller action forwarding, all startup states or automatic disconnect.

Those mutating probes used a bounded private research adapter. The packaged provider's read-only observation subsequently passed against the existing live stream, including exact uint64 preservation and separate unknown game/input evidence. Its complete connection/dashboard orchestration still needs hardware validation. Source-only checks cannot replace that validation.

Valve's [Frame setup documentation](https://partner.steamgames.com/doc/steamhardware/steamframe/setup#Streaming) describes normal headset connection and PC reconnect behavior. The required remaining evidence is tracked in [S1: extend separate Frame Testbench orchestration for streamed game sessions](https://github.com/JakeGreen2145/UGEVR/issues/29). The physical gate remains separate from source and controlled tests.

Run the source-only checks with `python -m unittest discover -s tests -p test_steam_cef.py -v` and the orchestration checks with `python -m unittest discover -s tests -p test_streamed_session.py -v`. They execute authored guards against controlled Node fixtures, verify CDP handshakes/frames and deadlines, exercise controlled subprocess cancellation, and test zero-session association, unsafe IDs, peer changes, active streams, redaction, reuse and phase ordering. They contact no SSH host or VR runtime.
