# Input proxy protocol and build

## Build and offline tests

```sh
bash scripts/build-input.sh                # build/libframe_input.so + libframe_loader.so
bash scripts/build-input.sh /private/out   # optional isolated output directory
python3 -m unittest discover -s tests -v
CXXFLAGS='-fsanitize=undefined -fno-sanitize-recover=all' python3 -m unittest discover -s tests -p test_input_proxy.py -v
```

Requires Linux, a C++17 g++ toolchain, libc/libdl/pthreads, and Python 3 for tests.
The script honors CXX and CXXFLAGS. Build natively for the target architecture.
It statically links the C++ runtime and hides its symbols, avoiding SteamVR's
potentially different libstdc++. Public exports are only HmdDriverFactory for
the proxy, and dlopen/frame_real_dlopen for the loader.

The tests build a fake cv DSO and an executable named vrserver. They dynamically
load the production proxy through the production loader, exercise every public
host/input forwarding method, preserve component identity, and make real UNIX
socket requests. They do not load SteamVR, access a headset, or restart services.
Test artifacts go under build/input-tests. These are CPU/ABI tests, not evidence
of live Frame tracking, compositor rendering, or XRService presence behavior.

## Loader environment

Only the vrserver executable, identified by the basename of /proc/self/exe,
redirects dlopen. Other processes inheriting LD_PRELOAD are unchanged.

* LD_PRELOAD: absolute path to libframe_loader.so.
* FRAME_TESTBENCH_PROXY: absolute path to libframe_input.so, required to redirect.
* FRAME_TESTBENCH_SOCKET: optional absolute UNIX socket path. Default:
  /run/user/<uid>/frame-testbench.sock.
* FRAME_TESTBENCH_REAL_DRIVER: optional exact absolute cv module path. Default:
  /opt/steamvr/drivers/cv/bin/linuxarm64/driver_cv.so. Tests set this to the fake
  module. Do not point it at the proxy.

The loader matches only that exact absolute path, not substrings, relative paths,
other modules, dlopen(NULL), or alternate spellings. The proxy loads cv through
frame_real_dlopen, which calls the un-interposed RTLD_NEXT dlopen. This keeps the
real cv module at its original path and preserves its $ORIGIN dependencies. No
/opt modifications, vtable patches, device registrations, or settings writes are
performed. Provider004, host006, and input004 use the pinned public Valve header.
All other requested interface versions pass through unchanged.

Installation, service environment, initial restart, live acceptance, and recovery
are the caller's responsibility. A missing proxy/cv DSO or a control socket that
cannot be created causes cv initialization to fail rather than pretending control
is available. Removing the service environment and restarting restores ordinary
loading. The source makes no service changes itself.

## Socket security and lifetime

The socket is chmod 0600 before listen. The parent directory must belong to the
runtime UID and must not be group/other writable. SO_PEERCRED restricts clients
to the runtime UID. Live sockets, symlinks, and ordinary files are never replaced.
An owned stale socket is reclaimed only after ECONNREFUSED and an inode check.
Cleanup removes only the same inode created by this instance.

Each connection sends one ASCII text command followed by newline and receives one
JSON line, then closes. Requests are bounded to 1024 bytes. A partial request has
a two-second connection deadline. This is only a transport timeout: overrides
have no lease or disconnect timeout. They remain until release or provider restart.
Up to 32 concurrent clients are serviced without blocking the nominal 90Hz tick.

## Commands

```text
status
pose x y z qw qx qy qz
worn 0
worn 1
release
pose-release
worn-release
```

Pose coordinates are RAW tracking coordinates in meters. Quaternion order is
w,x,y,z. Every value must be finite, and the quaternion must be nonzero. The proxy
normalizes it with overflow-safe scaling. Synthetic poses have identity
world-from-driver and driver-from-head rotations, zero translations for those
transforms, zero velocity/acceleration, poseTimeOffset=0, connected/valid=true,
and TrackingResult_Running_OK. This is a stationary pose, not a trajectory.

Only the activated cv HMD index and /proximity components belonging to its exact
property container are intercepted. Every matching proximity handle is supported.
Controllers and other components are forwarded unchanged. Real HMD pose and
proximity updates are cached but suppressed independently while their respective
override is enabled. Commands publish immediately; the worker republishes at a
nominal 90Hz even without physical updates or provider RunFrame calls.

release clears both overrides. The selective releases clear only their named
channel. Release immediately forwards its latest cached physical sample, including
original driver transforms and validity. If no physical sample has arrived yet,
that channel resumes forwarding at the next physical update. No physical state is
invented. Cached samples can be stale when cv is in standby.

Deactivate clears readiness and stops publication before forwarding the real
Deactivate. Cleanup joins the worker before calling real provider Cleanup.

## Response fields

Every response, including rejected commands, contains:

* ok: boolean, true when a command was accepted or status was read. This is not a
  client-side or compositor readback guarantee.
* sequence: monotonically increasing override/capture mutation counter, reset by
  provider Init. Status reads and malformed requests do not change it. Physical
  samples and periodic republications do not change it.
* pose_override: boolean.
* worn_override: null when released, otherwise the requested boolean.
* pose: selected DriverPose data, either requested override or latest physical;
  null if neither exists. Contains position:[x,y,z], quaternion:[w,x,y,z], valid,
  connected, world_from_driver, and driver_from_head.
* physical_pose: latest unmodified real DriverPose in the same JSON shape or null.
  Its position/quaternion are driver-space values, not a silently assumed raw or
  Standing pose. The two transform objects preserve the conversion inputs.
* requested_pose: null or [x,y,z,qw,qx,qy,qz], normalized.
* physical_worn: null if unknown, otherwise the latest physical value for the first
  matching proximity handle.
* hmd_index: captured Activate index, or null when inactive.
* hmd_container: captured property container, zero when inactive.
* proximity_ready: boolean, true when a matching /proximity handle is captured.
* proximity_handles: all captured matching handles.
* error: fixed explanatory string on rejected requests.

Pose and worn commands reject missing HMD/proximity readiness. On Deactivate the
requested override fields persist but hmd_index is null and publication stops.
The header ABI and offline tests do not establish that low-level sensor presence,
headset suspend, compositor reprojection, or application poses respond correctly
on a particular SteamVR build. Verify those separately before claiming live use.
