# Streamed UGEVR test sessions

Frame Testbench can call the external UGEVR session controller from the Windows test host. UGEVR owns its injector, launch, deployment receipt and process cleanup. Testbench owns its headset lease and bounded evidence collection. UGEVR does not import or require this package.

This implementation starts the integration work in [S1: extend separate Frame Testbench orchestration for streamed game sessions](https://github.com/JakeGreen2145/UGEVR/issues/29). It does not complete that ticket's physical probes. The accepted target is [UGEVR: full Unity VR injector capability target with staged delivery](https://github.com/JakeGreen2145/UGEVR/issues/24).

## External client

`frame_testbench.ugevr.Client` invokes an explicitly configured controller argument list without a shell. Its version 1 transport reads a flat YAML scalar mapping. It rejects duplicate keys, unsupported schema versions and malformed scalar syntax. The client pins the run ID, process ID, process creation ticks and executable returned by the controller. Later replies must match all four. Controller exit code 4 preserves a typed pending result. Error replies and other failure codes cannot become readiness.

The initial command contract is `launch`, `status` and `stop`, each with a unique `--session-dir`. Launch passes the executable, deployment roots and deployment script, or the explicit development-only `--prepared true` option. Stop checks the known run identity before it asks the controller to stop. It never requests a forced process kill. Individual calls time out, and cancellation terminates only the CLI child. The orchestrator separately attempts session cleanup afterward.

A structured failed deployment reply can have a zero process ID before the game starts. The client preserves that failure and binds the run ID and executable. A later nonzero process identity must remain stable. Structured exit code 3 results remain failed or stopped; they do not establish readiness.

`Client.launch` accepts an optional typed integer `steam_app_id`, and the `run` CLI exposes `--steam-app-id`. Valid IDs are positive decimal values through 4294967295. The client rejects booleans, strings and numeric coercion. It forwards the ID to the controller, which sets `SteamAppId` and `SteamGameId` only in the owned game child. It does not change the test process or machine environment.

`frame-testbench-stream` is an optional installed entry point. It adds no tools to the existing MCP server and changes no existing headset CLI commands. It currently supports a fixed SSH headset target and a local Windows controller. Configure the actual paired PC and headset before using it.

```powershell
frame-testbench-stream --controller-dll C:\UGEVR\UGEVR.Controller.dll `
  --session-dir C:\TestRuns\unique-run run `
  --exe C:\Games\Example\Example.exe --game-root C:\Games\Example `
  --distribution-root C:\UGEVR\package --deployment-script C:\UGEVR\deploy.ps1 `
  --host frame-testbench --expected-peer my-paired-pc `
  --duration-seconds 180 --chunk-seconds 30
```

The example requires actual paths and a valid deployment package. It does not install or change pairing. Run `python -m frame_testbench.stream_cli --help` from a checkout to see the same interface. The `status` and `stop` subcommands query the external controller without contacting the headset.

## Test lifecycle and evidence

The orchestrator refuses an already active shared headset test lease. An idle headset receives a 120-second lease before host launch. A dedicated thread renews it every 30 seconds during captures and recordings. Renewal must report an active lease. On completion, cancellation or failure, the orchestrator joins that thread before it ends the lease. It independently attempts Windows session stop even when headset cleanup fails. Existing headset guard expiry remains responsible if transport fails.

The current headset lease protocol has no exclusive client token. The initial active-lease check cannot prevent a simultaneous client from starting a lease. Run with exclusive test access to this headset. A future lease-token contract must precede safe multi-client orchestration.

Host readiness requires a live process, matching fresh injector status, engine readiness, runtime readiness and active rendering. These fields do not identify the streaming peer or prove that a game appears on the headset.

The default observation period is 180 seconds, with a maximum of 300. Continuous headset video is split into recording chunks of at most 60 seconds. Each chunk also has a separate stereo screenshot. The video remains the existing left-eye recording path. Screenshot and video success prove only artifact collection. They cannot substitute for both-eye game association.

Each run retains `session.yaml`, the controller run ID, requested and recorded duration, artifact directories, evidence references and separate cleanup outcomes. Stream cleanup remains `unavailable`; host termination and lease expiry do not prove the streaming connection stopped. A final YAML result also appears on stdout. Exit code 4 means evidence remains incomplete, 3 means the run failed, 130 means cancellation and 2 means a CLI error. Cancellation is checked between bounded headset operations. A capture or recording can take up to its 120-second client timeout before cleanup runs.

Host cleanup becomes `observed` only when the process has exited, the state is `stopped`, `cleanup_complete` is `true` and `deployment_restored` is `true` or `not_applicable`. A stopped process with incomplete or unconfirmed restoration remains `pending`. A controller `failed` state produces failed host cleanup and preserves its `last_error` reason, even after process exit. Metadata retains restoration and completion fields separately. The standalone `stop` CLI uses these same rules for its exit code.

There is no automatic stream-start control in this change. The default stream request is `unavailable` and the mode is `assisted`. A caller may supply a source-verified `stream_request` callback, which can return `requested`, `assisted` or `unavailable`. That outcome never sets readiness. The CLI deliberately has no arbitrary command, undocumented URI or caller-supplied readiness flag.

An integration can provide an independent `observe(host_status, test_id)` callback. Its observation must carry the current run ID, intended peer, an associated monotonic timestamp no more than 15 seconds old and an evidence reference. Both eye markers must identify that run. A Windows game input receipt must identify the unique test ID. The observer is responsible for actually issuing the controlled input and checking its receipt and causal response in the game. Headset proxy readback alone is insufficient. The callback is a trusted evidence adapter, not an adversarial proof system. No physical evidence adapter is implemented yet.

Without this observer, peer, both-eye and input-forwarding evidence stay `unknown`. Even complete observations produce only `association_observed`, not a functional gameplay pass. Checkpoints, physical observations, performance, optics and comfort remain separate acceptance work.

## Checks and remaining integration

Run the source-only checks with:

```text
python -m unittest discover -s tests -p test_streamed_session.py -v
```

They exercise a real controlled subprocess transport and test malformed YAML, pending replies, process reuse, wrong peer/run/eye/input evidence, stale observations, three-minute chunk composition, lease renewal, cancellation and independent cleanup failures. They do not contact SSH, SteamVR or a headset.

`tests/check_ugevr_cli.py` provides an opt-in cross-repository check against the built UGEVR controller and its CPU-only `UGEVR.Controller.Checks` apphost. Run it as a module with `python -m tests.check_ugevr_cli`, supplying `--controller-dll`, `--check-child` and a new `--session-dir`. It launches the fixture in its three-second natural-exit mode, checks the actual linked injector-session producer and waits for normal stop. It leaves the session metadata for inspection. It does not launch a Unity game or VR runtime. The initial Windows implementation passed this check with the separately built controller and producer fixture.

Before the physical gate, integration must verify the real published controller, deployment and injector status contract; configure pairing and prove the intended peer; validate the documented reconnect request; observe both-eye game markers and controlled Windows pose/action receipt; and separately observe game, stream and headset cleanup. See [Which Windows game-launch and Steam Frame stream controls are evidenced for Frame Testbench?](https://github.com/JakeGreen2145/UGEVR/issues/23) for the source evidence and required bounded probes. Assisted connection must remain labeled assisted until an unattended control is validated.
