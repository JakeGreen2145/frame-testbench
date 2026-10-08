# Stdio MCP server

`frame-testbench-mcp` exposes the toolkit through the official Python MCP SDK's
FastMCP server. It uses stdin/stdout only. It opens no listening port and requires
no separate authentication service. The launching process and, for remote use,
your existing SSH configuration determine access.

## Install from a checkout

Requires Python 3.11 or newer. The ordinary CLI has no Python dependencies; MCP
is an optional extra. Use an isolated environment:

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -e '.[mcp]'
make all
.venv/bin/frame-testbench-mcp --help
```

Without uv, use `python3 -m venv .venv` and
`.venv/bin/python -m pip install -e '.[mcp]'`. Some Debian installations need the
`python3-venv` OS package first. The MCP extra requires `mcp>=1.30,<2`.

An editable install preserves the CLI's checkout-relative `build/` path. Run
`make all` on the machine that actually runs the native observer. Remote-only
MCP clients do not need a local native build. The target checkout and its native
build must support the same CLI commands, including `controller`.

The Python wheel contains only `frame_testbench` and package metadata, not the
native binaries, build scripts, or OpenVR headers. A wheel alone is **not a
supported local native toolkit installation**. It can act as a remote SSH MCP
client. For local use, keep the checkout and editable installation above.

## Start and connect

Local SteamVR, under the same user/session as the toolkit:

```sh
/path/to/frame-testbench/.venv/bin/frame-testbench-mcp \
  --artifacts-dir /absolute/path/to/captures
```

Remote Steam Frame through an already configured SSH alias:

```sh
/path/to/frame-testbench/.venv/bin/frame-testbench-mcp \
  --host frame \
  --remote-root dev/frame-testbench \
  --artifacts-dir /absolute/path/to/captures
```

The remote root may be relative to the SSH user's home or absolute. SSH uses the
CLI's batch mode, host-key checking, and connection timeout. Configure keys and
known hosts yourself before starting the server. The server does not install,
uninstall, restart SteamVR, or change SSH configuration.

Generic MCP client configuration:

```json
{
  "mcpServers": {
    "frame-testbench": {
      "command": "/absolute/path/to/frame-testbench/.venv/bin/frame-testbench-mcp",
      "args": [
        "--host", "frame",
        "--remote-root", "dev/frame-testbench",
        "--artifacts-dir", "/absolute/path/to/captures"
      ]
    }
  }
}
```

Omit `--host` and `--remote-root` for local use. For clients that provide a tool
call timeout, allow at least 450 seconds for capture or recording and leave
additional time when queueing calls. The server's CLI subprocess timeout is
420 seconds, covering the CLI's bounded backend and fetch operations.

You can also launch `.venv/bin/python -m frame_testbench.mcp_server`, or use the
checkout launcher `.venv/bin/python /path/to/frame-testbench/frame-testbench-mcp`.
An editable install makes the module available regardless of working directory.

## Tools

| Tool | Arguments | CLI operation |
| --- | --- | --- |
| `status` | None | `status` |
| `worn` | `state`: `on`, `off`, or `physical` | `worn STATE` |
| `hmd_pose_set` | Required `position`; optional `rotation`, `space` | `pose set` |
| `hmd_pose_move` | Optional `translation`, `rotation`, `space` | `pose move` |
| `hmd_pose_reset` | None | `pose reset` |
| `controller_pose_set` | Required `side`, `position`; optional `rotation`, `space` | `controller SIDE set` |
| `controller_pose_move` | Required `side`; optional `translation`, `rotation`, `space` | `controller SIDE move` |
| `controller_pose_reset` | Required `side` | `controller SIDE reset`, clears inputs and disconnects |
| `controller_button` | `side`, `button`, `pressed`; optional `touched` | `controller SIDE button NAME on/off [--touch on/off]` |
| `controller_trigger` | `side`, `value`; optional `clicked`, `touched` | `controller SIDE trigger VALUE [--click on/off] [--touch on/off]` |
| `controller_grip` | `side`, `value`; optional `clicked`, `touched` | `controller SIDE grip VALUE [--click on/off] [--touch on/off]` |
| `controller_thumbstick` | `side`, `x`, `y`; optional `clicked`, `touched` | `controller SIDE thumbstick X Y [--click on/off] [--touch on/off]` |
| `controller_touch` | `side`, `control`, `touched` | `controller SIDE touch NAME on/off` |
| `controller_inputs_reset` | `side` | `controller SIDE inputs-reset`, retains pose |
| `controller_input_status` | None | `inputs`, independent SteamVR action readback |
| `compositor` | `mode`: `awake` or `auto` | `compositor MODE` |
| `capture` | None | `capture` with a server-owned output/fetch directory |
| `record` | Optional `duration_seconds`, `fps`, `view` | `record --duration N --fps N --view VIEW` with a server-owned output/fetch directory |
| `release_all` | None | `release` |

`side` is `left` or `right`. Position and translation are three-element XYZ
arrays in meters. Rotation is `[yaw, pitch, roll]` in degrees. All numbers must
be finite JSON numbers, not strings or booleans. Default translation and rotation
are `[0, 0, 0]`. Space is `standing` by default, or `raw`. Moves require a prior
pose set for that device and follow the existing CLI's coordinate conventions.

### Controller inputs

Set the selected controller's pose before sending buttons, analog values or touches.
The native proxy enforces this prerequisite. Each input tool sends a single batch;
invalid fields reject the whole batch before it changes controller state. CLI and
MCP also reject wrong-sided names and invalid values before any backend I/O,
including SSH or a CLI subprocess.

Button names match the Frame profile:

- Both sides: `system`, `bumper`, `trigger`, `grip`, `thumbstick`.
- Right only: `menu`, `a`, `b`, `x`, `y`.
- Left only: `view`, `dpad_up`, `dpad_right`, `dpad_down`, `dpad_left`.

`controller_touch` accepts those names plus `thumbrest` on either side. Thumbrest
has touch only, no click. Wrong-sided names are errors, not aliases. Skeletons and
haptics are not supported.

Trigger and grip `value` must be in `[0, 1]`. Thumbstick `x` and `y` must each be
in `[-1, 1]`. MCP `pressed`, `clicked` and `touched` are JSON booleans. Numeric
strings, boolean analog values, nonfinite numbers and out-of-range values are errors.

Values, clicks and touches are independent. No value threshold infers a click or
touch. Omitted optional fields remain unchanged; optional JSON `null` also leaves
them unchanged. Setting trigger value to zero does not release a previously set
trigger click. Use `controller_inputs_reset` to neutralize all buttons, analog axes
and touches on that side without disconnecting or moving its controller.
`controller_pose_reset` clears that side's inputs and disconnects it. `release_all`
clears both controllers' inputs and disconnects them as well as releasing HMD overrides.

`status` includes the proxy's per-side `inputs_ready` and commanded `inputs` map.
That acknowledgement is not downstream verification. Use `controller_input_status`
for the observer's independent SteamVR action readback. The CLI equivalent is
`frame-testbench inputs`; it uses the checkout's `resources/input-actions.json`
manifest and does not query the control socket. The native observer and action
resources must be installed in the target checkout. Readback establishes the
observer's action values, not another application's bindings or behavior.

Example calls to `controller_trigger`, `controller_button`, then `controller_touch`:

```json
{"side": "right", "value": 0.75, "clicked": true, "touched": true}
```

```json
{"side": "right", "button": "a", "pressed": true}
```

```json
{"side": "left", "control": "thumbrest", "touched": false}
```

### Pose examples

Examples of tool arguments:

```json
{"position": [0, 1.6, 0], "rotation": [30, 0, 0], "space": "standing"}
```

```json
{"side": "left", "translation": [0.1, 0, 0], "rotation": [0, 15, 0]}
```

Input overrides persist until explicitly reset or released. Disconnecting the
MCP client does not release them. `release_all` restores physical HMD tracking
and proximity, clears controller inputs and disconnects the synthetic controllers.
It leaves the compositor policy unchanged. Physical controller tracking is never overridden. Use
`compositor` with `{"mode":"auto"}` separately to restore automatic standby.
Do not apply virtual tracking overrides while someone relies on normal tracking.

### Recording

Call `record` with no arguments for 10 seconds at a requested 5 fps in stereo,
or supply strict integer `duration_seconds` from 1 to 60 and `fps` from 1 to 10.
Booleans, strings, fractional numbers, and integer-valued floats are rejected.
`view` is `stereo` by default or `preview`. For example:

```json
{"duration_seconds": 10, "fps": 5, "view": "stereo"}
```

The recording samples compositor screenshots and encodes an MP4. This is not a
real-time compositor mirror or headset-refresh-rate video. The public GL mirror
is unavailable on Steam Frame. Requested sampling rates do not guarantee actual
capture rates; inspect the returned recording metadata and `timeline.json`.

Pose and controller input calls can run while a recording is active. The client
must issue those calls concurrently rather than waiting for `record` to return.

## Results and artifact files

Successful tools return the actual CLI JSON as both a text content block and
`structuredContent`. Normal CLI failures and input validation errors return MCP
`isError: true`; they are not successful responses containing an error string.

`capture` also returns two `image/png` image blocks, in preview then stereo order,
with base64-encoded PNG bytes for vision-capable clients. The text and structured
metadata retain the CLI's runtime snapshots, image paths, and capture metadata.
The server does not substitute a path for image content or generate placeholder
screenshots.

Each capture gets a new private directory under `--artifacts-dir`, defaulting to
`artifacts` relative to the server working directory. Its `images/` child is left
absent for the CLI to create. Local captures use `--output`; SSH captures use
`--fetch` and read the downloaded `local_files`, never remote filesystem paths.
The server checks expected filenames, rejects symlinks and outside paths, checks
PNG signatures, and limits each returned image to 32 MiB. The native observer is
responsible for validating complete capture files. Oversized images fail instead
of being silently resized or omitted.

`record` returns the CLI JSON plus official MCP `resource_link` blocks for
`recording.mp4` with MIME type `video/mp4` and `timeline.json` with MIME type
`application/json`. Videos are not embedded as base64. Each recording gets a new
private `record-*/recording/` destination under `--artifacts-dir`; the final child
is absent until the CLI creates it. Local calls use `--output`, and remote calls
use `--fetch` and validate only the fetched `local_files`.

Links use local `file://` URIs on the MCP server machine. The client needs access
to that filesystem and support for resource links to open them. These files are
not exposed through an HTTP server or an MCP resource-read endpoint.

The server checks backend success before returning links, requires exact owned
paths, refuses symlinks and nonregular files, limits MP4 files to 512 MiB, and
checks a bounded 16-byte header for the MP4 `ftyp` signature without reading the
whole video. It parses the timeline as JSON with an 8 MiB limit. These checks do
not establish complete video decodability; the CLI and encoder produce the video.

Artifacts, including partial failed captures and recordings, remain on disk for
diagnosis. There is no automatic retention policy. Delete old artifacts when no
call is using them, and treat screenshots and recordings as potentially sensitive.

## Execution boundaries

- Host, remote checkout, and artifact directory are fixed at startup. Tool calls
  cannot set a host, socket, output path, executable, or arbitrary command.
- Schemas reject unknown arguments, wrong types, unsupported enums, and nonfinite
  numbers before the CLI subprocess starts.
- Commands use literal argv, never a local shell. The existing CLI handles SSH
  quoting and backend validation.
- One server serializes controls and captures through one lock. Recordings use a
  separate lock, so only one recording runs per server while controls can proceed.
  A cancelled MCP request retains its shielded worker and lock until the already
  started, bounded CLI operation finishes. Cancellation is not rollback. These
  locks do not coordinate separate servers or direct CLI processes; use one
  control writer for a target.
- Stdout carries MCP JSON-RPC only. Diagnostics go to stderr. Backend stdout is
  captured and parsed as JSON, never forwarded directly to the client stream.
- Existing CLI environment overrides such as `FRAME_TESTBENCH_SOCKET` and
  `FRAME_TESTBENCH_OBSERVER` remain trusted launch-time configuration, not tool
  parameters. Give the server only the environment and SSH access it needs.

## Reproduce the tests

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -e '.[mcp]'
make all
.venv/bin/python -m unittest discover -s tests -p 'test_mcp*.py' -v
.venv/bin/python -m unittest discover -s tests -v
```

Without the extra, MCP-dependent tests skip; packaging metadata tests still run.
Do not count a skipped MCP suite as MCP verification. No hardware or SSH connection
is needed. Tests use an explicitly identified CPU-only observer fixture and a
real SDK client/server subprocess to exercise initialize, list-tools, tool calls,
backend errors, validation, PNG byte content, metadata, and stderr separation.
Recording tests use explicitly synthetic MP4 headers, not playable videos or
hardware evidence. A real SDK stdio round trip verifies resource links and
metadata with a synthetic CLI runner. Unit tests check local/remote argv, unsafe
paths, size limits, bounded header reads, concurrent controls, and cancellation.
Existing native tests build and exercise their own CPU-only OpenVR
boundary. Live headset verification is a separate step.

License: MIT, matching the repository's `LICENSE`.
