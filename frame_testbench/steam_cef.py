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
import uuid

from . import transport
from .steam_cef_remote import DIAGNOSTIC_CODES
from .ugevr import read_scalars, write_scalars


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
    receipt_claimed: bool = False
    association_removed: bool = False
    windows_runtime_state: str = 'unknown'

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
        receipt, removed = data.get('receipt_claimed', False), data.get('association_removed', False)
        if type(receipt) is not bool or type(removed) is not bool or data.get('windows_runtime_state', 'unknown') != 'unknown':
            raise ProviderError('invalid Steam CEF ownership or cleanup fields')
        return cls(client_id(data.get('client_id')), *(data[key] for key in flags), visible, receipt, removed)


@dataclass(frozen=True)
class DisconnectResult:
    outcome: str
    association_removed: bool
    request_issued: bool = False
    windows_runtime_state: str = 'unknown'


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
        self._ownership_nonce = uuid.uuid4().hex

    def _call(self, operation, cancelled=None):
        if operation not in ('observe', 'connect', 'hide-dashboard', 'disconnect'):
            raise ValueError('unsupported Steam CEF operation')
        arguments = ['python3', '-', '--operation', operation, '--expected-peer', self.expected_peer,
                     '--deadline-seconds', str(self.deadline_seconds)]
        if self.identity is not None:
            arguments += ['--client-id', self.identity]
        arguments += ['--ownership-nonce', self._ownership_nonce]
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
        self.owns_stream = state.request_issued and state.stream_associated and state.receipt_claimed
        return state

    def hide_dashboard(self, cancelled=None):
        state = self.observe(cancelled)
        if not state.can_stream_vr or not state.stream_associated or state.other_stream_active:
            raise ProviderError('dashboard control requires only the intended SteamVR stream')
        return self._call('hide-dashboard', cancelled)

    def disconnect(self, cancelled=None):
        if not self.owns_stream:
            return DisconnectResult('unowned', False)
        # A fresh worker verifies the context's exact row object and private raw tuple receipt.
        state = self._call('disconnect', cancelled)
        if state.association_removed:
            self.owns_stream = False
        return DisconnectResult('association_removed' if state.association_removed else 'pending',
                                state.association_removed, state.request_issued)

    def save_receipt(self, path):
        if not self.owns_stream or self.identity is None:
            raise ProviderError('no newly owned observed stream receipt to save')
        path = Path(path).absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 65536:
                raise ProviderError('refusing to replace an unrelated receipt path')
            prior = read_scalars(path.read_text(encoding='utf-8'))
            if prior.get('ownership_nonce') != self._ownership_nonce:
                raise ProviderError('receipt path contains a different ownership record')
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(write_scalars({'schema_version': '1', 'host': self.host,
                                                'expected_peer': self.expected_peer, 'client_id': self.identity,
                                                'ownership_nonce': self._ownership_nonce}), encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def load_receipt(self, path):
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ProviderError('invalid ownership receipt file')
        data = read_scalars(path.read_text(encoding='utf-8'))
        if (data.get('schema_version') != '1' or data.get('host') != self.host
                or data.get('expected_peer') != self.expected_peer
                or not re.fullmatch(r'[a-f0-9]{32}', data.get('ownership_nonce', ''))):
            raise ProviderError('ownership receipt does not match this provider')
        self.identity = client_id(data.get('client_id'))
        self._ownership_nonce = data['ownership_nonce']
        # Local file data is a hint. A fresh remote context receipt must confirm ownership.
        self.owns_stream = self.observe().receipt_claimed
        if not self.owns_stream:
            raise ProviderError('remote stream ownership receipt is no longer valid')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='fixed SSH headset alias')
    parser.add_argument('--expected-peer', required=True)
    parser.add_argument('--deadline-seconds', type=int, default=30)
    parser.add_argument('--experimental', action='store_true', help='explicitly activate the experimental mutating control')
    parser.add_argument('--receipt-file', type=Path, help='private local ownership receipt for a later explicit disconnect')
    parser.add_argument('operation', choices=['observe', 'connect', 'hide-dashboard', 'disconnect'])
    args = parser.parse_args(argv)
    try:
        if args.operation != 'observe' and not args.experimental:
            raise ValueError('mutating Steam CEF operations require --experimental')
        provider = SteamCEF(args.host, args.expected_peer, deadline_seconds=args.deadline_seconds)
        if args.operation == 'disconnect':
            if args.receipt_file is not None:
                provider.load_receipt(args.receipt_file)
            cleanup = provider.disconnect()
            print(write_scalars(dict(vars(cleanup), mode=provider.mode)), end='')
            return 0 if cleanup.association_removed else 4
        state = getattr(provider, args.operation.replace('-', '_'))()
        if args.receipt_file is not None and args.operation == 'connect' and provider.owns_stream:
            provider.save_receipt(args.receipt_file)
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
