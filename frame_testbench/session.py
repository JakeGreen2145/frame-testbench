"""Headset-side leases and recovery for temporary testbench input/power overrides."""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 300
UNIT = 'frame-testbench-guard.service'


class Session:
    def __init__(self, bench, directory=None, clock=time.monotonic):
        self.bench = bench
        key = hashlib.sha256(str(bench.socket).encode()).hexdigest()[:16]
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
        self.directory = Path(directory) if directory else runtime / 'frame-testbench-sessions'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / (key + '.json')
        self.guard_path = self.directory / (key + '.guard')
        self.lock_path = self.directory / (key + '.lock')
        self.clock = clock

    @contextlib.contextmanager
    def locked(self):
        import fcntl  # Linux headset only; remote CLI clients may run on Windows.
        with self.lock_path.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def read(self):
        try:
            state = json.loads(self.path.read_text())
            if (not isinstance(state, dict) or
                    not math.isfinite(state['expires_at']) or
                    not 1 <= state['timeout_seconds'] <= MAX_TIMEOUT):
                return None
            return state
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def status(self):
        state = self.read()
        remaining = max(0, state['expires_at'] - self.clock()) if state else 0
        return {'ok': True, 'active': remaining > 0,
                'remaining_seconds': round(remaining, 2),
                'timeout_seconds': state['timeout_seconds'] if state else DEFAULT_TIMEOUT}

    def require_guard(self):
        result = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', UNIT],
                                timeout=5, check=False)
        if result.returncode:
            raise RuntimeError('testbench guard is unavailable; refusing persistent overrides')
        try:
            guard = json.loads(self.guard_path.read_text())
            if guard['socket'] != str(self.bench.socket) or guard['pid'] <= 0:
                raise ValueError('guard target mismatch')
            os.kill(guard['pid'], 0)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError('no live guard for this input socket; refusing persistent overrides') from error

    def renew(self, timeout=None, create=False, grace=0):
        state = self.read()
        active = state and state['expires_at'] > self.clock()
        if not active and not create:
            return self.status()
        seconds = timeout if timeout is not None else (state['timeout_seconds'] if active else DEFAULT_TIMEOUT)
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= MAX_TIMEOUT:
            raise ValueError('session timeout must be an integer from 1 to 300 seconds')
        self.require_guard()
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'timeout_seconds': seconds, 'expires_at': self.clock() + max(seconds, grace)}))
        temporary.chmod(0o600)
        temporary.replace(self.path)
        return self.status()

    def end(self):
        # Try both recoveries even if the proxy or runtime is unavailable. Remove
        # the lease first so the guard retries any failed recovery independently.
        self.path.unlink(missing_ok=True)
        errors = []
        state = None
        try:
            state = self.bench.control('release')
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(str(error))
        try:
            self.bench.native('setting', 'true')
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(str(error))
        if errors:
            raise RuntimeError('session cleanup incomplete: ' + '; '.join(errors))
        return dict(state or {}, ok=True, session=self.status(), compositor_auto=True)

    def sweep(self, check_power=False):
        with self.locked():
            state = self.read()
            if state and state['expires_at'] > self.clock():
                return False
            # Catch clients which bypass the CLI and write the native socket.
            try:
                current = self.bench.control('status')
                overridden = current.get('pose_override') or current.get('worn_override') is not None
                overridden = overridden or any(c.get('pose_override') or any(c.get('inputs', {}).values())
                                                for c in current.get('controllers', {}).values())
            except (OSError, RuntimeError):
                overridden = False
            power_override = False
            if check_power and not (state or overridden):
                power_override = self.bench.native('status')['settings']['pauseCompositorOnStandby'] is False
            if state or overridden or power_override:
                self.end()
                return True
            return False


def dispatch(args, bench, operation):
    """Arm recovery before mutations; observations renew only an existing session."""
    if args.command == 'controller-trace':
        return operation(args, bench)
    session = Session(bench)
    if args.command == 'session':
        with session.locked():
            if args.session_command == 'end':
                return session.end()
            if args.session_command == 'status':
                return session.status()
            if args.session_command == 'begin':
                # Starting an empty session does not force worn state or wake displays.
                if session.read() and not session.status()['active']:
                    session.end()
                return session.renew(args.timeout, create=True)
            if not session.status()['active']:
                raise RuntimeError('no active session to renew; use session begin')
            return session.renew()
    if args.command == 'release':
        with session.locked():
            return session.end()
    if args.command in ('install', 'uninstall'):
        return operation(args, bench)
    mutation = args.command in ('worn', 'pose', 'controller', 'compositor')
    with session.locked():
        if session.read() and not session.status()['active']:
            session.end()
        session.renew(create=mutation and not (args.command == 'compositor' and args.mode == 'auto'),
                      grace=120 if args.command in ('capture', 'record') else 0)
        if mutation:
            try:
                return operation(args, bench)
            except Exception:
                try:
                    session.end()
                except Exception:
                    pass  # guard retains responsibility for retrying recovery
                raise
    # Slow captures/recordings must not block the independent expiry watchdog.
    try:
        result = operation(args, bench)
    except Exception:
        with session.locked():
            if session.status()['active']:
                session.end()
        raise
    if args.command in ('capture', 'record'):
        with session.locked():
            session.renew()
    if args.command == 'status':
        result['session'] = session.status()
    return result


def main():
    from .cli import Bench
    session = Session(Bench())
    if '--cleanup' in __import__('sys').argv:
        with session.locked():
            session.end()
        return
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Leases never survive a guard restart, logout, or headset reboot.
    with session.locked():
        try:
            session.end()
        except Exception as error:
            print(str(error), flush=True)
        session.guard_path.write_text(json.dumps({'pid': os.getpid(), 'socket': str(session.bench.socket)}))
    if os.environ.get('NOTIFY_SOCKET'):
        address = os.environ['NOTIFY_SOCKET']
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
            notification.connect('\0' + address[1:] if address.startswith('@') else address)
            notification.sendall(b'READY=1')
    last_power_check = -30.0
    try:
        while not stopping:
            now = time.monotonic()
            power_check = now - last_power_check >= 10
            if power_check:
                last_power_check = now
            try:
                if session.sweep(check_power=power_check):
                    print('Restored physical input and automatic compositor standby', flush=True)
            except Exception as error:
                print(str(error), flush=True)
            time.sleep(1)
    finally:
        with session.locked():
            session.guard_path.unlink(missing_ok=True)
            session.end()


if __name__ == '__main__':
    main()
