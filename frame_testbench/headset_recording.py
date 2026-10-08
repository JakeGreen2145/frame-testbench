"""Continuous rootless SteamVR HeadsetView recording through Framecorder.

This is a separately rendered left-eye view, not final panel scanout. The wrapper
never installs software, starts SteamVR, changes inputs, or falls back to PNGs.
"""
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from .recording import FILES, media_file, validate

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_OVERHEAD_SECONDS = 20
STOP_GRACE_SECONDS = 1
MAX_PROBE_BYTES = 8 * 1024 * 1024


def resolve_framecorder():
    """An explicit override is authoritative, including when it is broken."""
    override = os.environ.get('FRAME_TESTBENCH_FRAMECORDER')
    pinned = ROOT / 'tools/framecorder-v0.1.1/framecorder'
    if override is not None:
        candidate = Path(override).absolute() if override else None
    elif pinned.is_file() and os.access(pinned, os.X_OK):
        candidate = pinned
    else:
        found = shutil.which('framecorder')
        candidate = Path(found).absolute() if found else None
    if candidate is None or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError('Framecorder unavailable; explicitly run python3 scripts/setup-framecorder.py '
                           'on the capture host, or set FRAME_TESTBENCH_FRAMECORDER to an executable')
    return str(candidate)


def _signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _stop(process):
    # Signal the group, not just the recorder: encoder/muxer children must not
    # survive a failed launch. Reap our direct child even after forced shutdown.
    for signum in (signal.SIGINT, signal.SIGTERM):
        _signal_group(process, signum)
        try:
            process.wait(timeout=STOP_GRACE_SECONDS)
            break
        except subprocess.TimeoutExpired:
            pass
    _signal_group(process, signal.SIGKILL)
    process.wait(timeout=STOP_GRACE_SECONDS)


def _run(argv, directory, stdout, stderr, timeout):
    process = subprocess.Popen(argv, cwd=directory, stdin=subprocess.DEVNULL,
                               stdout=stdout, stderr=stderr, start_new_session=True)
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise RuntimeError(f'{Path(argv[0]).name} exited with status {code}; see local logs')
    except subprocess.TimeoutExpired as exc:
        _stop(process)
        raise RuntimeError(f'{Path(argv[0]).name} timed out; see local logs') from exc
    except BaseException:
        _stop(process)
        raise
    finally:
        # Also dispose of unexpected children left behind by a clean launcher.
        _signal_group(process, signal.SIGKILL)


def _verify(ffprobe, partial, duration, fps, timeout):
    media_file(partial)
    directory = partial.parent
    probe_path, error_path = directory / 'ffprobe.json', directory / 'ffprobe.log'
    with probe_path.open('xb') as output, error_path.open('xb') as errors:
        _run([ffprobe, '-v', 'error', '-threads', '1', '-count_frames', '-show_packets',
              '-show_entries', 'packet=pts_time,duration_time:stream=codec_type,codec_name,width,height,nb_frames,nb_read_frames,duration,time_base,avg_frame_rate:format=duration,format_name',
              '-of', 'json', str(partial)], directory, output, errors, timeout)
    # ffprobe can exit zero while reporting truncated or corrupt packet errors.
    if error_path.stat().st_size:
        raise ValueError('recording contains decoding errors; see ffprobe.log')
    if not 0 < probe_path.stat().st_size <= MAX_PROBE_BYTES:
        raise ValueError('invalid or oversized recording probe')
    info = json.loads(probe_path.read_text())
    streams = info.get('streams', [])
    if (len(streams) != 1 or streams[0].get('codec_type') != 'video'
            or streams[0].get('codec_name') != 'h264'):
        raise ValueError('recording must contain exactly one H.264 video stream and no audio')
    stream = streams[0]
    try:
        width, height = int(stream['width']), int(stream['height'])
        count = int(stream['nb_read_frames'])
        declared_count = int(stream['nb_frames'])
        encoded_duration = float(stream['duration'])
        container_duration = float(info['format']['duration'])
        packets = info['packets']
        pts = [float(packet['pts_time']) for packet in packets]
        packet_durations = [float(packet['duration_time']) for packet in packets]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('recording lacks verified frame counts or encoded timestamps') from exc
    if min(width, height) <= 0 or not 2 <= count <= 3660 or declared_count != count or len(pts) != count:
        raise ValueError('recording is empty, truncated, or missing encoded frames')
    if (any(not math.isfinite(t) for t in pts + packet_durations)
            or any(t <= 0 for t in packet_durations)
            or any(b <= a for a, b in zip(pts, pts[1:]))):
        raise ValueError('invalid encoded presentation timestamps')
    # The capture loop may include its endpoint frame. This is a cadence-sized
    # tolerance, not permission to accept an early-stop or substantially long clip.
    tolerance = max(.15, 1 / fps + .05)
    if (not math.isfinite(encoded_duration) or not math.isfinite(container_duration)
            or encoded_duration < duration - .15
            or encoded_duration > duration + tolerance
            or abs(container_duration - encoded_duration) > .05
            or abs(pts[-1] + packet_durations[-1] - pts[0] - encoded_duration) > .05):
        raise ValueError('encoded recording duration does not match requested capture')
    return dict(codec='h264', width=width, height=height, encoded_frame_count=count,
                encoded_duration_seconds=encoded_duration, video_pts_seconds=pts,
                packet_duration_seconds=packet_durations, video_time_base=stream.get('time_base'),
                encoded_average_frame_rate=stream.get('avg_frame_rate'))


def record(output, duration, fps, native):
    validate(duration, fps, 'headset')
    directory = Path(output).absolute()
    if directory.exists() or directory.is_symlink():
        raise FileExistsError('record output already exists; use a fresh directory')
    binary = resolve_framecorder()
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError('headset record requires ffprobe on the capture host')
    # Native status uses a Background connection. An Overlay connection alone
    # can launch SteamVR; do not let Framecorder attempt it without a live read.
    before = native('status')
    if not isinstance(before, dict) or before.get('ok') is not True:
        raise RuntimeError('headset recording requires a successful live runtime status read')
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    directory = directory.resolve()
    partial = directory / 'recording.partial.mp4'
    argv = [binary, '--source', 'headset', '--view', 'eye', '--eye', 'left',
            '--aspect', '16:9', '--codec', 'h264', '--fps', str(fps),
            '--duration', str(duration), '--bitrate', '12', '--no-audio',
            '--gpu-priority', 'low', str(partial)]
    deadline = time.monotonic() + duration + 30
    with (directory / 'framecorder.log').open('xb') as log:
        _run(argv, directory, log, log, duration + CAPTURE_OVERHEAD_SECONDS)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('recording verification timed out')
    verified = _verify(ffprobe, partial, duration, fps, min(15, remaining))
    metadata = dict(ok=True, source='openvr_headset_view', backend='framecorder', view='headset',
                    requested_duration_seconds=duration, requested_fps=fps,
                    timing_basis='encoded_video_pts',
                    timing_note='Packet presentation timestamps describe the encoded video, not exposure time or distinct compositor updates. No audio.',
                    capture_scope='SteamVR separately rendered left-eye HeadsetView; not final panel scanout or a guarantee of all overlays/passthrough.',
                    runtime_before=before, **verified)
    partial.rename(directory / 'recording.mp4')
    (directory / 'timeline.json').write_text(json.dumps(metadata, indent=2, allow_nan=False) + '\n')
    result = dict(metadata, output=str(directory), files={name: str(directory / name) for name in FILES})
    (directory / 'result.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    return result
