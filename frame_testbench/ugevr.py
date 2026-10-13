"""Bounded external UGEVR version 1 CLI client. No injector code lives here."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import tempfile
import time
import uuid


class ProtocolError(RuntimeError):
    pass


def read_scalars(text):
    """Read the protocol's flat YAML subset, never arbitrary YAML objects."""
    if len(text.encode('utf-8')) > 65536:
        raise ProtocolError('controller reply exceeds 64 KiB')
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_]+):\s*(.+)', line)
        if not match or match[1] in values:
            raise ProtocolError('invalid or duplicate flat YAML key')
        key, scalar = match.groups()
        try:
            if scalar.startswith('"'):
                value = json.loads(scalar)
                if not isinstance(value, str):
                    raise ValueError('expected quoted string')
            elif scalar.startswith("'"):
                if len(scalar) < 2 or not scalar.endswith("'"):
                    raise ValueError('unterminated string')
                value = scalar[1:-1].replace("''", "'")
            else:
                if any(c in scalar for c in '{}[]#&*!|>'):
                    raise ValueError('unsupported YAML scalar')
                value = scalar
        except ValueError as error:
            raise ProtocolError('invalid protocol scalar') from error
        values[key] = value
    return values


def write_scalars(values):
    return ''.join(f'{key}: {json.dumps(str(value).lower() if isinstance(value, bool) else str(value), ensure_ascii=True)}\n'
                   for key, value in values.items())


def boolean(values, key):
    if values.get(key) not in ('true', 'false'):
        raise ProtocolError(f'invalid {key}')
    return values[key] == 'true'


@dataclass(frozen=True)
class HostStatus:
    run_id: str
    process_id: int
    process_start_utc_ticks: int
    executable: str
    state: str
    process_alive: bool
    engine_ready: bool
    runtime_ready: bool
    rendering_active: bool
    injector_status: str
    values: dict[str, str]

    @classmethod
    def parse(cls, values):
        try:
            if values.get('schema_version') != '1':
                raise ValueError('unsupported schema version')
            run = str(uuid.UUID(values['run_id']))
            pid, ticks = int(values['process_id']), int(values['process_start_utc_ticks'])
            alive = boolean(values, 'process_alive')
            if pid < 0 or ticks < 0 or (pid == 0) != (ticks == 0) or (alive and pid == 0):
                raise ValueError('invalid process identity')
            state = values['state']
            if state not in {'preparing', 'waiting_for_injector', 'active', 'stopping', 'stopped', 'failed'}:
                raise ValueError('unknown session state')
            return cls(run, pid, ticks, values['executable'], state,
                       alive, boolean(values, 'engine_ready'),
                       boolean(values, 'runtime_ready'), boolean(values, 'rendering_active'),
                       values['injector_status'], dict(values))
        except (ValueError, KeyError) as error:
            raise ProtocolError(f'invalid controller status: {error}') from error

    @property
    def ready(self):
        return (self.state == 'active' and self.process_alive and self.engine_ready
                and self.runtime_ready and self.rendering_active and self.injector_status == 'fresh')


def run_process(argv, timeout, cancelled):
    """Cancel only the CLI child. The orchestrator separately stops its game session."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors, subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=output, stderr=errors) as child:
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancelled.is_set():
                    raise InterruptedError('controller call cancelled')
                if time.monotonic() >= deadline:
                    raise TimeoutError('controller call timed out')
                if os.fstat(output.fileno()).st_size > 65536 or os.fstat(errors.fileno()).st_size > 65536:
                    raise ProtocolError('controller output exceeds 64 KiB')
                try:
                    child.wait(timeout=min(.1, max(.001, deadline - time.monotonic())))
                    output.seek(0)
                    errors.seek(0)
                    stdout, stderr = output.read(65537), errors.read(65537)
                    if len(stdout) > 65536 or len(stderr) > 65536:
                        raise ProtocolError('controller output exceeds 64 KiB')
                    return child.returncode, stdout.decode('utf-8-sig'), stderr.decode('utf-8', errors='replace')
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            child.kill()
            child.wait()
            raise


class Client:
    def __init__(self, command, session_dir, *, runner=run_process, call_timeout=45):
        if not command or any(not isinstance(arg, str) or not arg for arg in command):
            raise ValueError('controller command must be a nonempty argument list')
        if not 1 <= call_timeout <= 120:
            raise ValueError('controller call timeout must be 1..120 seconds')
        self.command = list(command)
        self.session_dir = Path(session_dir).absolute()
        self.runner, self.call_timeout = runner, call_timeout
        self.identity = None

    def _call(self, operation, options=(), cancelled=None):
        argv = self.command + [operation, '--session-dir', str(self.session_dir), *options]
        code, output, _ = self.runner(argv, self.call_timeout, cancelled or threading.Event())
        values = read_scalars(output)
        if 'error' in values:
            raise ProtocolError(values['error'])
        if code not in (0, 3, 4):
            raise ProtocolError(f'controller failed with exit code {code}')
        status = HostStatus.parse(values)
        if code == 3 and status.state not in ('failed', 'stopped'):
            raise ProtocolError('controller failure code conflicts with session state')
        identity = (status.run_id, status.process_id, status.process_start_utc_ticks, status.executable)
        if self.identity is not None:
            same_run = identity[0] == self.identity[0] and identity[3] == self.identity[3]
            same_process = self.identity[1] == 0 or identity[1:3] == self.identity[1:3]
            if not same_run or not same_process:
                raise ProtocolError('controller returned a different run or process identity')
        self.identity = identity
        return status

    def launch(self, executable, *, game_root=None, distribution_root=None,
               deployment_script=None, prepared=False, game_arguments=(), cancelled=None):
        options = ['--exe', str(Path(executable).absolute()), '--timeout-seconds', '30']
        if prepared:
            if any((game_root, distribution_root, deployment_script)):
                raise ValueError('prepared launch cannot include deployment options')
            options += ['--prepared', 'true']
        else:
            if not all((game_root, distribution_root, deployment_script)):
                raise ValueError('launch requires game root, distribution root and deployment script')
            for name, value in [('game-root', game_root), ('distribution-root', distribution_root),
                                ('deployment-script', deployment_script)]:
                options += ['--' + name, str(Path(value).absolute())]
        for argument in game_arguments:
            options += ['--game-arg', argument]
        return self._call('launch', options, cancelled)

    def status(self, cancelled=None):
        return self._call('status', cancelled=cancelled)

    def stop(self):
        # Check correlation before asking the controller to clean up. No forced kill.
        self.status()
        return self._call('stop', ['--timeout-seconds', '30'])
