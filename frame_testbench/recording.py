"""Bounded recordings: continuous HeadsetView or explicit sampled screenshots."""
import json
import math
from pathlib import Path
import shutil
import subprocess
from . import transport

MAX_BYTES = 512 * 1024 * 1024
FILES = ('recording.mp4', 'timeline.json')


def validate(duration, fps, view):
    if type(duration) is not int or not 1 <= duration <= 60:
        raise ValueError('record duration must be an integer from 1 to 60 seconds')
    if view not in ('headset', 'preview', 'stereo'):
        raise ValueError('record view must be headset, preview or stereo')
    maximum_fps = 60 if view == 'headset' else 10
    if type(fps) is not int or not 1 <= fps <= maximum_fps:
        raise ValueError(f'record fps must be an integer from 1 to {maximum_fps}')


def media_file(path):
    if path.is_symlink() or not path.is_file() or not 12 <= path.stat().st_size <= MAX_BYTES:
        raise ValueError('recording must be a regular MP4 of at most 512 MiB')
    with path.open('rb') as stream:
        if stream.read(12)[4:8] != b'ftyp':
            raise ValueError('recording is not MP4')


def samples(data, directory, view):
    if not isinstance(data, dict) or data.get('ok') is not True:
        raise RuntimeError('native recording failed')
    frames = data.get('frames', [])
    if not isinstance(frames, list) or not 2 <= len(frames) <= 600:
        raise ValueError('recording needs 2..600 complete samples')
    previous = -1.0
    for index, frame in enumerate(frames):
        start, end = frame['request_seconds'], frame['completed_seconds']
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)):
            raise ValueError('invalid sample timestamp')
        if not 0 <= start <= end <= 120 or end <= previous:
            raise ValueError('unordered sample timestamps')
        previous = end
        expected = directory / f'frame-{index:06d}' / (view + '.png')
        if (frame['index'] != index or frame[view] != str(expected) or expected.is_symlink()
                or not expected.is_file() or expected.resolve() != expected):
            raise ValueError('unsafe or missing native sample path')
    return frames


def record(output, duration, fps, view, native):
    validate(duration, fps, view)
    directory = Path(output).absolute()
    if directory.exists() or directory.is_symlink():
        raise FileExistsError('record output already exists; use a fresh directory')
    if view == 'headset':
        from . import headset_recording
        return headset_recording.record(directory, duration, fps, native)
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise RuntimeError('record requires ffmpeg and ffprobe on the capture host')
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = directory.resolve()
    data = native('record', str(directory), duration, fps)
    frames = samples(data, directory, view)
    # No arbitrary backend paths enter the concat demuxer. Its paths are relative,
    # generated names, so even an output directory containing quotes is safe.
    lines = ['ffconcat version 1.0']
    first = frames[0]['completed_seconds']
    final_hold = 1 / fps
    video_duration = frames[-1]['completed_seconds'] - first + final_hold
    for index, frame in enumerate(frames):
        hold = frames[index+1]['completed_seconds'] - frame['completed_seconds'] if index+1 < len(frames) else final_hold
        frame['video_pts_seconds'] = frame['completed_seconds'] - first
        lines += [f"file frame-{index:06d}/{view}.png", 'option framerate 1000', f'duration {hold:.9f}']
    # The duplicate terminator makes the final duration observable to the muxer.
    lines += [f'file frame-{len(frames)-1:06d}/{view}.png', 'option framerate 1000']
    (directory / 'frames.ffconcat').write_text('\n'.join(lines) + '\n')
    metadata = dict(data, view=view, sample_count=len(frames),
                    timing_basis='screenshot_completion_monotonic',
                    video_duration_seconds=video_duration, final_hold_seconds=final_hold,
                    measured_sample_fps=(len(frames)-1)/(frames[-1]['completed_seconds']-first),
                    timing_note='Completion timestamps bound receipt, not photon time. Gaps are held, not interpolated; last sample held one requested interval. No audio.')
    (directory / 'timeline.json').write_text(json.dumps(metadata, indent=2, allow_nan=False) + '\n')
    partial = directory / 'recording.partial.mp4'
    with (directory / 'encode.log').open('wb') as log:
        subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
                        '-f', 'concat', '-safe', '0', '-i', 'frames.ffconcat',
                        '-vf', 'scale=trunc(min(1920\\,iw)/2)*2:-2',
                        '-fps_mode', 'vfr', '-enc_time_base', '1:1000',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23', '-threads', '2',
                        '-pix_fmt', 'yuv420p', '-an', '-movflags', '+faststart',
                        '-video_track_timescale', '1000', '-fs', str(MAX_BYTES), str(partial)],
                       cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=log, check=True, timeout=90)
    media_file(partial)
    probe = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(partial)],
                           capture_output=True, text=True, check=True, timeout=15)
    info = json.loads(probe.stdout)
    streams = info.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_name') != 'h264':
        raise ValueError('encoded recording does not contain one H.264 video stream')
    stream = streams[0]
    encoded_duration = float(info['format']['duration'])
    if not math.isfinite(encoded_duration) or abs(encoded_duration-video_duration) > .1:
        raise ValueError('encoded recording duration does not match measured timeline')
    if min(stream['width'],stream['height']) <= 0 or int(stream.get('nb_frames', 0)) < len(frames):
        raise ValueError('encoded recording is missing samples')
    metadata.update(encoded_duration_seconds=encoded_duration,
                    width=stream['width'], height=stream['height'],
                    encoded_frame_count=int(stream['nb_frames']), codec='h264')
    (directory / 'timeline.json').write_text(json.dumps(metadata, indent=2, allow_nan=False) + '\n')
    partial.rename(directory / 'recording.mp4')
    result = dict(metadata, output=str(directory), files={name: str(directory / name) for name in FILES})
    (directory / 'result.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    return result


def fetch(host, result, output):
    transport.remote_argv(host, '.', [])
    if result.get('ok') is not True:
        raise RuntimeError('cannot fetch unsuccessful recording')
    paths = result.get('files', {})
    for name in FILES:
        remote = paths.get(name, '')
        if not isinstance(remote, str) or not remote.startswith('/') or Path(remote).name != name or any(ord(c) < 32 for c in remote):
            raise ValueError('invalid remote recording path')
    directory = Path(output).absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    directory = directory.resolve()
    local = {}
    for name in FILES:
        dest = directory / name
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                        '--', f'{host}:{paths[name]}', str(dest)], check=True, timeout=60)
        if name.endswith('.mp4'):
            media_file(dest)
        else:
            if dest.is_symlink() or not dest.is_file() or dest.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('invalid recording timeline')
            timeline = json.loads(dest.read_text())
            if not isinstance(timeline,dict) or timeline.get('ok') is not True:
                raise ValueError('unsuccessful recording timeline')
        local[name] = str(dest)
    fetched = dict(result, local_files=local)
    (directory / 'result.json').write_text(json.dumps(fetched, indent=2, allow_nan=False) + '\n')
    return fetched
