# Sampled compositor recording

```sh
build/frame-observe record /absolute/new/output 5 4
```

The arguments are a fresh absolute output directory, integer duration from 1 to 60 seconds, and integer requested FPS from 1 to 10. Validation precedes SDK initialization. The parent directory must exist. The native process creates the output root and `frame-000000`, `frame-000001`, and subsequent directories with mode 0700. Each completed sample contains `preview.png` and `stereo.png`.

One Background OpenVR session and one screenshot interface serve all sequential requests. Samples use the same signature, chunk, CRC and terminal-IEND checks as `capture`. The recorder skips missed cadence slots and never starts catch-up bursts. It stops issuing requests at the monotonic duration deadline. An already-issued screenshot may complete after that deadline. If the final scheduled sample finishes early, the recorder waits until the deadline rather than shortening the requested recording interval.

Success requires at least two completed samples, no more than 600 samples, and at most 512 MiB of completed PNG file bytes. The byte total is checked after each completed pair. A pair exceeding the budget produces an error, not a truncated success. All files remain for inspection, including excess or incomplete files from failures. This bounds successful recordings, not the asynchronous SDK's disk writes. A one-second recording at one FPS cannot produce the required two samples.

The process alarm covers SDK initialization, capture and shutdown. Its default for `record` is duration plus 20 seconds. `FRAME_OBSERVE_TIMEOUT_SECONDS` explicitly replaces that default with an integer from 1 to 120 seconds. A hung request, incomplete PNG, corrupt CRC, SDK failure or insufficient sample count returns JSON with `ok:false`; prior completed samples do not turn a partial failure into success. Existing paths are never overwritten and failed directories are never removed automatically.

Success JSON contains:

- `ok:true`, `command:"record"`, `source:"openvr_screenshots"`.
- Integer `requested_duration_seconds` and `requested_fps`.
- `elapsed_seconds`, measured from recording start through the sampling interval and final completion check, excluding SDK setup and shutdown.
- `frames`, each with sequential `index`, `request_seconds`, `completed_seconds`, and absolute `preview` and `stereo` paths.
- `capture_scope:"sampled OpenVR stereo screenshots, cropped view; not headset-rate video or full lens output"`.

Timestamps use the steady clock relative to the same recording start. Request time is sampled immediately before `RequestScreenshot`; completion time follows validation of both PNGs. Neither is a photon-exposure timestamp. The native command does not encode video or invoke ffmpeg. Consumers should preserve the irregular sample times rather than claiming the requested FPS was achieved.

CPU tests compile the actual observer against a fake OpenVR library derived from the pinned public header. They verify sequential requests, complete files before shutdown, timing, failure retention and fixed default limits. A native inclusion probe uses reduced internal limits to exercise exact-cap and cap-plus-one accounting without writing hundreds of MiB. These tests do not validate live compositor rendering or optical coverage.
