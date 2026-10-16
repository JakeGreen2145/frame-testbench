"""Bounded, test-only Framecorder control sessions (Linux /proc required).

Runtime closure and decoded media verification are deliberately separate receipts.
The caller owns the lease and any game/input lifetime; this module changes none.
"""
import hashlib
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time
import uuid

from . import headset_recording as legacy
from .recording import MAX_BYTES, media_file

MAX_SECONDS = 330
MAX_SEGMENTS = 7
NORMAL_QUIT = 3.0  # mux STOP_WAIT=1.5s, tick=.25s, plus bounded writer drain
ESCALATION = 1.0  # unchanged INT / TERM / KILL stage bounds
START_TIMEOUT = 20.0
ACK_TIMEOUT = 4.0
PROGRESS_TIMEOUT = 12.0  # source reports every five seconds
MAX_LOG = 8 * 1024 * 1024
MAX_LINE = 65536
ROOT = Path(__file__).resolve().parents[1]


def validate(duration, segment_seconds, fps):
    for value, low, high, label in ((duration, 1, MAX_SECONDS, 'duration'),
                                    (segment_seconds, 1, 50, 'segment-seconds'),
                                    (fps, 1, 60, 'fps')):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{label} must be an integer in {low}..{high}')


def digest(path, deadline=None):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError('hash_budget_exhausted')
            value.update(chunk)
    return value.hexdigest()


def file_identity(path):
    info = path.stat()
    return dict(device=info.st_dev, inode=info.st_ino, size=info.st_size,
                mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)


def atomic_json(path, value, *, replace=True):
    temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    owned = False
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            owned = True
            json.dump(value, stream, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # exclusive publication, never overwrite a receipt
            temporary.unlink()
        owned = False
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if owned:
            temporary.unlink(missing_ok=True)


def proc_identity(pid):
    """Do not hide permissions, malformed proc data, or other census failures."""
    text = Path(f'/proc/{pid}/stat').read_text()
    fields = text[text.rindex(')') + 2:].split()
    return dict(pid=pid, state=fields[0], ppid=int(fields[1]), pgid=int(fields[2]),
                sid=int(fields[3]), start_ticks=int(fields[19]))


def group_members(identity, deadline=None):
    members = []
    started = time.monotonic()
    until = min(started + .5, deadline) if deadline is not None else started + .5
    if started >= until:
        raise RuntimeError('process census budget exhausted')
    count = 0
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        count += 1
        if count > 16384 or time.monotonic() >= until:
            raise RuntimeError('process census budget exceeded')
        try:
            current = proc_identity(int(path.name))
        except FileNotFoundError:
            continue  # only a disappearance race is absence
        if current['pgid'] == identity['pgid']:
            if current['sid'] != identity['sid'] or current['start_ticks'] < identity['start_ticks']:
                raise RuntimeError('process group identity conflict')
            if current['pid'] == identity['pid'] and current['start_ticks'] != identity['start_ticks']:
                raise RuntimeError('process leader identity changed')
            members.append(current)
    return members


class OwnedProcess:
    """One locally launched session leader, with bounded nonblocking pipe drains."""
    def __init__(self, argv, directory, name, ledger, save, event, deadline):
        self.entry = dict(name=name, dispatch='pending', argv=argv,
                          executable_sha256=digest(argv[0], deadline), identity=None, actual_exit=None,
                          group_absent=False)
        ledger['clients'].append(self.entry)
        save()  # write ahead of *any* attempted Popen
        if time.monotonic() >= deadline:
            raise RuntimeError('dispatch_budget_exhausted')
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.logs = {}
        self.pending = {}
        self.total = {}
        self.lines = []
        self.save, self.event = save, event
        self.failure = None
        self.identity = None
        try:
            for kind in ('stdout', 'stderr'):
                self.logs[kind] = (directory / f'{name}.{kind}').open('xb')
                self.pending[kind] = bytearray()
                self.total[kind] = 0
            self.process = subprocess.Popen(argv, cwd=directory, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            start_new_session=True, bufsize=0)
            # Keep the actual Popen owned even if identity/persistence fails next.
            self.identity = proc_identity(self.process.pid)
            if self.identity['pid'] != self.identity['pgid'] or self.identity['pid'] != self.identity['sid']:
                raise RuntimeError('child is not an isolated session leader')
            self.entry.update(dispatch='started', identity=self.identity)
            save()
            os.set_blocking(self.process.stdin.fileno(), False)
            for kind in ('stdout', 'stderr'):
                stream = getattr(self.process, kind)
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, kind)
        except BaseException:
            # A caller must finish this owned child before proceeding. Persist the
            # ambiguity; never turn a failed attach into an empty-client success.
            if self.process is not None:
                try:
                    self.process.kill()
                    self.process.wait(timeout=ESCALATION)
                    self.entry['actual_exit'] = self.process.returncode
                    if self.identity:
                        self.entry['group_absent'] = not group_members(self.identity)
                except (OSError, subprocess.SubprocessError):
                    pass
            for stream in self.logs.values():
                stream.close()
            if self.process is not None:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream and not stream.closed:
                        stream.close()
            self.selector.close()
            raise

    def pump(self, timeout=0):
        for key, _ in self.selector.select(max(0, timeout)):
            kind = key.data
            data = os.read(key.fileobj.fileno(), 65536)
            if not data:
                self.selector.unregister(key.fileobj)
                if self.pending[kind]:
                    self.failure = self.failure or 'incomplete_output_line'
                key.fileobj.close()
                continue
            self.total[kind] += len(data)
            if self.total[kind] > MAX_LOG:
                self.failure = self.failure or 'output_limit_exceeded'
                continue
            self.logs[kind].write(data)
            self.logs[kind].flush()
            self.pending[kind].extend(data)
            if len(self.pending[kind]) > MAX_LINE and b'\n' not in self.pending[kind]:
                self.failure = self.failure or 'output_line_limit_exceeded'
                self.pending[kind].clear()
            while b'\n' in self.pending[kind]:
                raw, _, tail = self.pending[kind].partition(b'\n')
                self.pending[kind] = bytearray(tail)
                if len(raw) > MAX_LINE:
                    self.failure = self.failure or 'output_line_limit_exceeded'
                    continue
                try:
                    line = raw.decode('utf-8').rstrip('\r')
                except UnicodeError:
                    self.failure = self.failure or 'invalid_output_encoding'
                    continue
                self.lines.append((kind, line, time.monotonic()))
        code = self.process.poll()
        if code is not None and self.entry['actual_exit'] is None:
            self.entry['actual_exit'] = code
            self.save()

    def send(self, command):
        # Commands are generated internally, never supplied by the CLI caller.
        self.event('command_intent', client=self.entry['name'], command=command)
        payload = (command + '\n').encode('utf-8')
        if len(payload) > 2048:
            raise ValueError('generated control command too long')
        if os.write(self.process.stdin.fileno(), payload) != len(payload):
            raise RuntimeError('partial_control_write')
        self.event('command_written', client=self.entry['name'], command=command)

    def stop(self, deadline, normal=False, stages=None):
        try:
            return self._stop(deadline, normal, stages)
        finally:
            # Even a census or durable-write failure must close local handles and
            # reap the directly owned child. It cannot prove group/SDK absence.
            if self.process.poll() is None and time.monotonic() < deadline:
                self.process.kill()
                try:
                    self.process.wait(timeout=max(.001, min(ESCALATION, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            for key in list(self.selector.get_map().values()):
                key.fileobj.close()
            self.selector.close()
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            for stream in self.logs.values():
                stream.close()

    def _stop(self, deadline, normal=False, stages=None):
        """Normal quit/EOF really requests STOP. Signals are bounded fallbacks."""
        if normal and self.process.poll() is None:
            try:
                self.send('quit')
                self.process.stdin.close()  # EOF also requests capture-loop STOP
            except (OSError, ValueError):
                self.failure = self.failure or 'quit_pipe_failure'
            until = min(deadline, time.monotonic() + NORMAL_QUIT)
            while self.process.poll() is None and time.monotonic() < until:
                self.pump(min(.05, max(0, until - time.monotonic())))
        for sig in (stages if stages is not None else (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)):
            self.pump()
            members = group_members(self.identity, deadline)
            if not members and self.process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                break
            self.event('signal', client=self.entry['name'], signal=int(sig), members=members)
            try:
                os.killpg(self.identity['pgid'], sig)
            except ProcessLookupError:
                pass
            until = min(deadline, time.monotonic() + ESCALATION)
            while time.monotonic() < until:
                self.pump(min(.05, max(0, until - time.monotonic())))
                if self.process.poll() is not None and not group_members(self.identity, deadline):
                    break
        self.pump()
        # Drain pipes only within this same deadline, including inherited pipes.
        while self.selector.get_map() and time.monotonic() < deadline:
            self.pump(min(.05, max(0, deadline - time.monotonic())))
        self.entry['group_absent'] = not group_members(self.identity, deadline)
        self.entry['pipes_drained'] = not bool(self.selector.get_map())
        self.entry['actual_exit'] = self.process.poll()
        self.entry['closure_checked_monotonic'] = time.monotonic()
        self.save()
        return (self.entry['actual_exit'] is not None and self.entry['group_absent']
                and self.entry['pipes_drained'])


class Protocol:
    def __init__(self, paths, event):
        self.paths, self.event = paths, event
        self.segments = []
        self.done = False
        self.progress_at = None
        self.first_progress = None
        self.captured_frames = 0

    def requested(self, path, now):
        if len(self.segments) >= MAX_SEGMENTS or path not in self.paths:
            raise ValueError('unowned segment request')
        if path.exists() or path.is_symlink() or path.with_suffix('.mp4.part').exists():
            raise FileExistsError('segment path collision')
        segment = dict(index=len(self.segments) + 1, file=path.name,
                       requested_monotonic=now, recording_monotonic=None,
                       stopped_monotonic=None, saved_monotonic=None)
        self.segments.append(segment)
        return segment

    def line(self, kind, line, now):
        if kind == 'stderr':
            if re.search(r'\bdone: \d+ frames, [0-9.]+ MB\s*$', line):
                if self.done:
                    raise ValueError('duplicate normal-stop footer')
                self.done = True
                self.event('native_done', raw=line)
                return  # the footer also contains "frames," but is not progress
            if ' fps,' in line or ' frames,' in line:
                progress = re.search(r'(\S+) fps, (\d+) frames, (\d+) dropped, (\S+) ms gpu per frame\s*$', line)
                if not progress:
                    raise ValueError('malformed_native_progress')
                fps, frames, dropped, gpu = progress.groups()
                fps, frames = float(fps), int(frames)
                if not math.isfinite(fps) or fps < 0:
                    raise ValueError('invalid_native_progress_fps')
                if frames < self.captured_frames:
                    raise ValueError('native_progress_regressed')
                advanced = frames > self.captured_frames
                if advanced:
                    self.captured_frames = frames
                    self.progress_at = now
                    self.first_progress = self.first_progress or now
                self.event('progress' if advanced else 'progress_not_advanced', raw=line,
                           captured_frames=frames, fps=fps)
            if re.search(r'\b(?:WARN|ERROR)\b', line):
                raise ValueError('native_warning_or_error')
            return
        if line.startswith(('failed ', 'saved clip ', 'clip ')):
            raise ValueError('native_control_failure')
        if line.startswith('recording '):
            path = line[len('recording '):]
            segment = next((s for s in self.segments if str(self.paths[s['index'] - 1]) == path), None)
            if segment is None or segment['recording_monotonic'] is not None or segment['saved_monotonic'] is not None:
                raise ValueError('unexpected_recording_ack')
            bound = START_TIMEOUT if segment['index'] == 1 else ACK_TIMEOUT
            if now - segment['requested_monotonic'] > bound:
                raise ValueError('late_recording_ack')
            segment['recording_monotonic'] = now
            self.event('segment_started', **segment)
            return
        match = re.fullmatch(r'saved recording (\d+) (\d+(?:\.\d+)?) (.+)', line)
        if match:
            size, rounded, path = match.groups()
            segment = next((s for s in self.segments if str(self.paths[s['index'] - 1]) == path), None)
            if (segment is None or segment['recording_monotonic'] is None
                    or segment['stopped_monotonic'] is None or segment['saved_monotonic'] is not None):
                raise ValueError('unexpected_saved_ack')
            if now - segment['stopped_monotonic'] > ACK_TIMEOUT:
                raise ValueError('late_saved_ack')
            if not 12 <= int(size) <= MAX_BYTES or not 0 < float(rounded) <= 60:
                raise ValueError('invalid_saved_ack')
            segment.update(saved_monotonic=now, announced_bytes=int(size),
                           rounded_duration_seconds=float(rounded))
            self.event('segment_saved', **segment)
            return
        if line:
            raise ValueError('unexpected_control_output')


def _fresh_directory(output):
    directory = Path(output).absolute()
    if any(ord(c) < 32 or ord(c) == 127 for c in str(directory)):
        raise ValueError('output path contains control characters')
    if len(str(directory).encode('utf-8')) > 1600:
        raise ValueError('output path exceeds control command budget')
    if directory.exists() or directory.is_symlink():
        raise FileExistsError('record-session output already exists; use a fresh directory')
    # Do not follow symlink parents or accept shared writable output directories.
    if any(p.is_symlink() for p in directory.parents):
        raise ValueError('output parent is a symlink')
    directory.mkdir(mode=0o700, parents=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('output filesystem did not preserve private directory ownership/mode')
    return directory.resolve()


def capture(output, duration=330, segment_seconds=50, fps=30, *, cancel=None):
    previous_term = signal.getsignal(signal.SIGTERM)
    try:
        with ExitStack() as resources:
            return _capture(output, duration, segment_seconds, fps, cancel=cancel, resources=resources)
    finally:
        signal.signal(signal.SIGTERM, previous_term)


def _capture(output, duration, segment_seconds, fps, *, cancel, resources):
    validate(duration, segment_seconds, fps)
    if os.name != 'posix' or not Path('/proc/self/stat').is_file():
        raise RuntimeError('record-session requires Linux /proc process identity')
    binary = legacy.resolve_framecorder()
    observer = str(Path(os.environ.get('FRAME_TESTBENCH_OBSERVER', ROOT / 'build/frame-observe')).absolute())
    if not Path(observer).is_file() or not os.access(observer, os.X_OK):
        raise RuntimeError('native status observer unavailable')
    directory = _fresh_directory(output)
    start = time.monotonic()
    deadline = start + duration
    nonce = uuid.uuid4().hex
    ledger = dict(schema_version=1, kind='framecorder_control_session', nonce=nonce,
                  output=str(directory), duration_budget_seconds=duration,
                  segment_seconds=segment_seconds, requested_fps=fps, clients=[],
                  first_failure=None, media_verified=False, runtime_closed=False,
                  started_monotonic=start, deadline_monotonic=deadline)
    events = resources.enter_context((directory / 'events.jsonl').open('x', encoding='utf-8'))
    def save():
        atomic_json(directory / 'session.json', ledger)
    def event(kind, **values):
        events.write(json.dumps(dict(schema_version=1, event=kind, monotonic=time.monotonic(),
                                     utc_unix_ns=time.time_ns(), nonce=nonce, **values), allow_nan=False) + '\n')
        events.flush()
        os.fsync(events.fileno())
    def fail(reason):
        if ledger['first_failure'] is None:
            ledger['first_failure'] = reason
            event('refused', reason=reason)
            save()
    paths = [directory / f'segment-{i:02d}-{nonce}.mp4' for i in range(1, MAX_SEGMENTS + 1)]
    protocol = Protocol(paths, event)
    active = None
    client = None
    precheck_closed = False
    previous_term = signal.getsignal(signal.SIGTERM)
    closing = False
    def interrupted(signum, frame):
        if not closing:
            raise KeyboardInterrupt('termination requested')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        save()
        precheck_deadline = min(deadline, time.monotonic() + 3)
        client = OwnedProcess([observer, 'status'], directory, 'precheck', ledger, save, event, precheck_deadline)
        # Reserve the unchanged escalation stages inside this three-second bound.
        while client.process.poll() is None and time.monotonic() < precheck_deadline - 1:
            client.pump(.02)
        precheck_closed = client.stop(precheck_deadline, stages=(signal.SIGKILL,))
        status = json.loads((directory / 'precheck.stdout').read_text())
        if (not precheck_closed or client.entry['actual_exit'] != 0 or client.failure
                or not isinstance(status, dict) or status.get('ok') is not True):
            raise RuntimeError('precheck_not_successfully_closed')
        ledger['runtime_before'] = status
        event('precheck_closed', identity=client.identity, actual_exit=0, group_absent=True)
        client = None
        if time.monotonic() + NORMAL_QUIT + 3 * ESCALATION + ACK_TIMEOUT >= deadline:
            raise RuntimeError('insufficient_aggregate_budget')
        argv = [binary, '--source', 'headset', '--view', 'eye', '--eye', 'left',
                '--aspect', '16:9', '--codec', 'h264', '--fps', str(fps), '--duration', str(duration),
                '--bitrate', '12', '--no-audio', '--gpu-priority', 'low', '--control', str(paths[0])]
        client = OwnedProcess(argv, directory, 'framecorder', ledger, save, event, deadline)
        active = protocol.requested(paths[0], time.monotonic())  # initial argv output, not a stdin command
        capture_stop = deadline - NORMAL_QUIT - 3 * ESCALATION
        ready = False
        stop_reason = 'aggregate_budget'
        while time.monotonic() < capture_stop:
            client.pump(.025)
            for kind, line, arrived in client.lines:
                protocol.line(kind, line, arrived)
            client.lines.clear()
            now = time.monotonic()
            if client.failure:
                raise RuntimeError(client.failure)
            if cancel and cancel():
                stop_reason = 'cancelled'
                fail(stop_reason)
                break
            if client.process.poll() is not None:
                raise RuntimeError('recorder_exited_before_stop')
            if protocol.done:
                raise RuntimeError('premature_native_done')
            if protocol.first_progress is None and now - protocol.segments[0]['requested_monotonic'] > START_TIMEOUT:
                raise RuntimeError('startup_progress_timeout')
            if protocol.progress_at is not None and now - protocol.progress_at > PROGRESS_TIMEOUT:
                raise RuntimeError('progress_stall')
            ack_bound = START_TIMEOUT if active['index'] == 1 else ACK_TIMEOUT
            if active['recording_monotonic'] is None and now - active['requested_monotonic'] > ack_bound:
                raise RuntimeError('recording_ack_timeout')
            for segment in protocol.segments:
                if (segment['stopped_monotonic'] is not None and segment['saved_monotonic'] is None
                        and now - segment['stopped_monotonic'] > ACK_TIMEOUT):
                    raise RuntimeError('saved_ack_timeout')
            if not ready and active['recording_monotonic'] is not None and protocol.first_progress is not None:
                ready = True
                ledger['ready_monotonic'] = now
                event('ready', identity=client.identity, first_segment=active['file'], media_verified=False)
            if active['recording_monotonic'] is not None and now - active['requested_monotonic'] >= segment_seconds:
                active['stopped_monotonic'] = now
                client.send('stop-record')
                if len(protocol.segments) == MAX_SEGMENTS or now + ACK_TIMEOUT >= capture_stop:
                    stop_reason = 'segment_limit' if len(protocol.segments) == MAX_SEGMENTS else 'aggregate_budget'
                    active = None
                    break
                active = protocol.requested(paths[len(protocol.segments)], now)
                client.send('record ' + str(paths[active['index'] - 1]))
        if active is not None:
            active['stopped_monotonic'] = time.monotonic()
            client.send('stop-record')
            active = None
        ledger['stop_reason'] = stop_reason
        event('normal_stop_requested', reason=stop_reason)
    except (Exception, KeyboardInterrupt) as error:
        fail(type(error).__name__ + ': ' + str(error))
    finally:
        closing = True
        if client is not None and not client.logs['stdout'].closed:
            if active is not None and client.entry['name'] == 'framecorder':
                active['stopped_monotonic'] = time.monotonic()
                try:
                    client.send('stop-record')
                except (OSError, ValueError) as error:
                    fail(type(error).__name__ + ': stop-record')
            try:
                closed = client.stop(deadline, normal=client.entry['name'] == 'framecorder')
                if client.entry['name'] == 'framecorder':
                    for kind, line, arrived in client.lines:
                        try:
                            protocol.line(kind, line, arrived)
                        except ValueError as error:
                            fail(str(error))
                    client.lines.clear()
                    if not closed or client.entry['actual_exit'] != 0 or not protocol.done:
                        fail('recorder_normal_shutdown_unproved')
                    client.entry['normal_footer'] = protocol.done
                    if client.failure:
                        fail(client.failure)
                    event('normal_stop', normal_footer=protocol.done, actual_exit=client.entry['actual_exit'],
                          group_absent=client.entry['group_absent'], pipes_drained=client.entry['pipes_drained'])
            except Exception as error:
                fail('closure_unknown: ' + type(error).__name__)
        ledger['segments'] = protocol.segments
        if len(ledger['clients']) == 2 and ledger.get('ready_monotonic') is None:
            fail('recorder_readiness_unproved')
        ledger['runtime_closed'] = bool(ledger['clients']) and all(
            c['dispatch'] == 'started' and type(c['actual_exit']) is int and c['group_absent'] is True
            and c.get('pipes_drained') is True for c in ledger['clients'])
        if not ledger['runtime_closed']:
            fail('runtime_closure_unknown')
        total_media_bytes = 0
        for segment in protocol.segments:
            path = directory / segment['file']
            if segment['saved_monotonic'] is None:
                fail('segment_save_unproved')
                continue
            try:
                media_file(path)
                total_media_bytes += path.stat().st_size
                if total_media_bytes > MAX_BYTES:
                    raise ValueError('aggregate_media_byte_limit')
                if path.stat().st_size != segment['announced_bytes']:
                    raise ValueError('saved byte count mismatch')
                segment['file_identity'] = file_identity(path)
                segment['sha256'] = digest(path, deadline)
                if file_identity(path) != segment['file_identity']:
                    raise ValueError('segment changed during closure hashing')
            except (OSError, ValueError, RuntimeError) as error:
                fail(type(error).__name__ + ': ' + str(error))
        expected_media = {s['file'] for s in protocol.segments}
        if any(p.name.endswith(('.mp4', '.mp4.part')) and p.name not in expected_media
               for p in directory.iterdir()):
            fail('unexpected_or_partial_media')
        ledger.update(completed_monotonic=time.monotonic(), ok=ledger['first_failure'] is None,
                      source='openvr_headset_view', capture_scope='separately rendered left-eye HeadsetView; not final panel scanout')
        save()
        event('completed', ok=ledger['ok'], runtime_closed=ledger['runtime_closed'], media_verified=False)
        events.close()
        signal.signal(signal.SIGTERM, previous_term)
    return ledger


def verify(output):
    try:
        return _verify_session(output)
    except (KeyError, TypeError, IndexError, OverflowError) as error:
        raise ValueError('invalid typed session metadata') from error


def _verify_session(output):
    """CPU only. No observer, recorder, proxy, session, or SDK dispatch."""
    directory = Path(output).absolute()
    if directory.is_symlink() or any(p.is_symlink() for p in directory.parents):
        raise ValueError('symlink verification directory')
    manifest = directory / 'session.json'
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 65536:
        raise ValueError('invalid session manifest')
    raw = manifest.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('session manifest must be an object')
    if (type(value.get('schema_version')) is not int or value.get('schema_version') != 1 or value.get('kind') != 'framecorder_control_session'
            or value.get('runtime_closed') is not True or value.get('ok') is not True
            or value.get('output') != str(directory.resolve())
            or not re.fullmatch('[a-f0-9]{32}', value.get('nonce', ''))):
        raise ValueError('session not successfully closed')
    validate(value['duration_budget_seconds'], value['segment_seconds'], value['requested_fps'])
    clients = value.get('clients')
    if (not isinstance(clients, list) or any(not isinstance(c, dict) for c in clients)
            or [c.get('name') for c in clients] != ['precheck', 'framecorder']
            or any(c.get('actual_exit') != 0 or type(c.get('actual_exit')) is not int
                   or c.get('group_absent') is not True or c.get('pipes_drained') is not True for c in clients)):
        raise ValueError('incomplete client closure ledger')
    if clients[1].get('normal_footer') is not True:
        raise ValueError('native normal-stop footer missing')
    for client in clients:
        identity = client['identity']
        if (not isinstance(identity, dict) or client.get('dispatch') != 'started' or any(type(identity.get(k)) is not int or identity[k] <= 0
                for k in ('pid', 'pgid', 'sid', 'start_ticks'))
                or identity['pid'] != identity['pgid'] or identity['pid'] != identity['sid']):
            raise ValueError('invalid owned process identity')
    segments = value.get('segments')
    if (not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS
            or any(not isinstance(s, dict) for s in segments)):
        raise ValueError('invalid segment inventory')
    expected_media = {s.get('file') for s in segments}
    if any(p.name.endswith(('.mp4', '.mp4.part')) and p.name not in expected_media
           for p in directory.iterdir()):
        raise ValueError('unexpected or partial media inventory')
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError('verification requires ffprobe')
    verified = []
    total_bytes = 0
    for index, segment in enumerate(segments, 1):
        expected = f'segment-{index:02d}-{value["nonce"]}.mp4'
        if segment.get('index') != index or type(segment.get('index')) is not int or segment.get('file') != expected:
            raise ValueError('tampered segment path/index')
        path = directory / expected
        media_file(path)
        total_bytes += path.stat().st_size
        if (path.stat().st_size != segment.get('announced_bytes') or file_identity(path) != segment.get('file_identity')
                or total_bytes > MAX_BYTES):
            raise ValueError('segment bytes changed or over budget')
        original_hash = digest(path)
        if original_hash != segment.get('sha256'):
            raise ValueError('segment content changed after closure')
        # Legacy verifier checks a fixed duration. Obtain a bounded duration first,
        # then reuse all its decoded frame/PTS/container checks unchanged in a
        # fresh CPU-only subdirectory, never overwrite prior diagnostic files.
        check_dir = directory / ('verify-' + uuid.uuid4().hex)
        check_dir.mkdir(mode=0o700)
        probe = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'json', str(path)], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=15)
        if probe.returncode or probe.stderr or len(probe.stdout) > 65536:
            raise ValueError('duration probe failed')
        duration = float(json.loads(probe.stdout)['format']['duration'])
        if not math.isfinite(duration) or not 0 < duration <= 60:
            raise ValueError('segment exceeds sixty seconds or has invalid duration')
        info = legacy._verify(ffprobe, path, duration, value['requested_fps'], 15,
                              diagnostics_directory=check_dir)
        if info['encoded_duration_seconds'] > 60:
            raise ValueError('encoded segment exceeds sixty seconds')
        if digest(path) != original_hash or file_identity(path) != segment['file_identity']:
            raise ValueError('segment changed during verification')
        verified.append(dict(index=index, file=expected, sha256=original_hash, **info))
    if manifest.read_bytes() != raw:
        raise ValueError('session manifest changed during verification')
    receipt = dict(ok=True, schema_version=1, nonce=value['nonce'], media_verified=True,
                   session_sha256=hashlib.sha256(raw).hexdigest(), segments=verified,
                   total_encoded_seconds=sum(s['encoded_duration_seconds'] for s in verified),
                   timing_basis='encoded PTS, not exposure time or gameplay credit',
                   capture_scope=value['capture_scope'])
    atomic_json(directory / ('verification-' + uuid.uuid4().hex + '.json'), receipt, replace=False)
    return receipt
