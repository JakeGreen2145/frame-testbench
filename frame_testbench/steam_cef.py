"""Explicit experimental Frame stream control through its existing loopback Steam CEF."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import subprocess
import threading
import tempfile
import os
import time

from . import transport
from .steam_cef_remote import DIAGNOSTIC_CODES


class ProviderError(RuntimeError):
    pass


def client_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value)
            or int(value) > 18446744073709551615):
        raise ProviderError('invalid Steam client identity')
    return value


@dataclass(frozen=True)
class StreamState:
    client_id: str
    can_stream_vr: bool
    stream_associated: bool
    other_stream_active: bool
    request_issued: bool
    timed_out: bool
    dashboard_visible: bool | None

    @classmethod
    def parse(cls, data):
        if (not isinstance(data, dict) or data.get('ok') is not True
                or type(data.get('schema_version')) is not int or data['schema_version'] != 1):
            raise ProviderError('invalid Steam CEF reply')
        flags = ['can_stream_vr', 'stream_associated', 'other_stream_active', 'request_issued', 'timed_out']
        if any(type(data.get(key)) is not bool for key in flags):
            raise ProviderError('invalid Steam CEF state flags')
        visible = data.get('dashboard_visible')
        if visible is not None and type(visible) is not bool:
            raise ProviderError('invalid Steam CEF dashboard state')
        return cls(client_id(data.get('client_id')), *(data[key] for key in flags), visible)


def ssh_worker(argv, source, timeout, cancelled):
    """Bounded SSH child only. Remote worker has fixed operations and scoped replies."""
    if cancelled.is_set():
        raise InterruptedError('Steam CEF operation cancelled before transport')
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors, subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=output, stderr=errors, text=True, encoding='utf-8') as child:
        deadline = time.monotonic() + timeout
        first = True
        try:
            while True:
                if cancelled.is_set():
                    raise InterruptedError('Steam CEF operation cancelled; request outcome may be unknown')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Steam CEF SSH operation timed out; request outcome may be unknown')
                if os.fstat(output.fileno()).st_size > 65536 or os.fstat(errors.fileno()).st_size > 65536:
                    raise ProviderError('Steam CEF transport output exceeds limit')
                try:
                    child.communicate(input=source if first else None, timeout=.1)
                    output.seek(0)
                    out = output.read(65537)
                    if len(out) > 65536 or os.fstat(errors.fileno()).st_size > 65536:
                        raise ProviderError('Steam CEF transport output exceeds limit')
                    return child.returncode, out.decode('utf-8-sig')
                except subprocess.TimeoutExpired:
                    first = False
        except BaseException:
            child.kill()
            child.communicate()
            raise


class SteamCEF:
    mode = 'experimental_steam_cef'

    def __init__(self, host, expected_peer, *, runner=ssh_worker, deadline_seconds=30):
        transport.remote_argv(host, '.', [])  # Existing startup-host validation, no I/O.
        if (not isinstance(expected_peer, str) or not 1 <= len(expected_peer) <= 128
                or any(ord(c) < 32 for c in expected_peer)):
            raise ValueError('expected peer must be one bounded display name')
        if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 30:
            raise ValueError('Steam CEF deadline must be 1..30 seconds')
        self.host, self.expected_peer = host, expected_peer
        self.runner, self.deadline_seconds = runner, deadline_seconds
        self.identity = None
        self.owns_stream = False
        self.last_state = None
        self.last_request = 'unavailable'

    def _call(self, operation, cancelled=None):
        if operation not in ('observe', 'connect', 'hide-dashboard'):
            raise ValueError('unsupported Steam CEF operation')
        arguments = ['python3', '-', '--operation', operation, '--expected-peer', self.expected_peer,
                     '--deadline-seconds', str(self.deadline_seconds)]
        if self.identity is not None:
            arguments += ['--client-id', self.identity]
        argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o',
                'ConnectTimeout=10', self.host, shlex.join(arguments)]
        source = Path(__file__).with_name('steam_cef_remote.py').read_text(encoding='utf-8')
        code, output = self.runner(argv, source, self.deadline_seconds + 30,
                                   cancelled or threading.Event())
        if len(output.encode('utf-8')) > 65536:
            raise ProviderError('Steam CEF reply exceeds limit')
        try:
            data = json.loads(output)
        except ValueError:
            raise ProviderError('Steam CEF returned invalid JSON') from None
        if code or not isinstance(data, dict) or data.get('ok') is not True:
            # Whitelist the diagnostic code; never include arbitrary remote text or stderr.
            reason = data.get('error_code') if isinstance(data, dict) else None
            if not isinstance(reason, str) or reason not in DIAGNOSTIC_CODES:
                reason = 'transport_failed'
            raise ProviderError('Steam CEF operation failed: ' + reason)
        if data.get('operation') != operation:
            raise ProviderError('Steam CEF operation reply does not match request')
        state = StreamState.parse(data)
        if self.identity is not None and state.client_id != self.identity:
            raise ProviderError('Steam CEF peer identity changed')
        self.identity = state.client_id
        self.last_state = state
        return state

    def observe(self, cancelled=None):
        return self._call('observe', cancelled)

    def connect(self, cancelled=None):
        before = self.observe(cancelled)
        if not before.can_stream_vr:
            raise ProviderError('intended peer cannot stream VR')
        if before.other_stream_active:
            raise ProviderError('another stream is active; connection refused')
        if before.stream_associated:
            self.last_request = 'reused'
            return before
        self.last_request = 'unknown'
        state = self._call('connect', cancelled)
        self.last_request = 'requested' if state.request_issued else 'unavailable'
        # Association after an initially empty state is evidence of our new request.
        self.owns_stream = state.request_issued and state.stream_associated
        return state

    def hide_dashboard(self, cancelled=None):
        state = self.observe(cancelled)
        if not state.can_stream_vr or not state.stream_associated or state.other_stream_active:
            raise ProviderError('dashboard control requires only the intended SteamVR stream')
        return self._call('hide-dashboard', cancelled)

    def disconnect(self):
        # The source candidate is not validated yet. Never mutate an unowned/reused stream.
        return 'unavailable'


def main(argv=None):
    import argparse
    from .ugevr import write_scalars
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='fixed SSH headset alias')
    parser.add_argument('--expected-peer', required=True)
    parser.add_argument('--deadline-seconds', type=int, default=30)
    parser.add_argument('--experimental', action='store_true', help='explicitly activate the experimental mutating control')
    parser.add_argument('operation', choices=['observe', 'connect', 'hide-dashboard', 'disconnect'])
    args = parser.parse_args(argv)
    try:
        if args.operation != 'observe' and not args.experimental:
            raise ValueError('mutating Steam CEF operations require --experimental')
        provider = SteamCEF(args.host, args.expected_peer, deadline_seconds=args.deadline_seconds)
        if args.operation == 'disconnect':
            print(write_scalars({'mode': provider.mode, 'disconnect': provider.disconnect()}), end='')
            return 4
        state = getattr(provider, args.operation.replace('-', '_'))()
        values = dict(vars(state), mode=provider.mode, request=provider.last_request,
                      owns_stream=provider.owns_stream, both_eye_evidence='unknown', input_evidence='unknown')
        if values['dashboard_visible'] is None:
            values['dashboard_visible'] = 'unknown'
        print(write_scalars(values), end='')
        return 4 if state.timed_out else 0
    except (ValueError, OSError, RuntimeError) as error:
        print(write_scalars({'error': str(error)}), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
