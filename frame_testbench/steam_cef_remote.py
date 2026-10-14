"""Fixed, experimental Steam CEF operations. Sent over SSH as inert Python stdin."""
import argparse
import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
import urllib.request

LIMIT = 65536
PORT = 8080
ERRORS = {'peer_missing', 'peer_ambiguous', 'peer_not_vr', 'unsafe_client_id',
          'store_unavailable', 'stream_call_unavailable', 'other_stream_active',
          'stream_already_active', 'peer_changed', 'dashboard_unavailable'}
DIAGNOSTIC_CODES = ERRORS | {
    'target_inventory_too_large', 'invalid_target_inventory', 'debug_target_missing_or_ambiguous',
    'invalid_debug_target', 'debug_target_not_loopback', 'handshake_too_large', 'handshake_refused',
    'evaluation_timeout', 'debug_target_disconnected', 'request_too_large', 'invalid_server_frame',
    'response_too_large', 'evaluation_failed', 'invalid_state', 'invalid_request_result',
    'stream_not_associated', 'invalid_dashboard_state', 'operation_unavailable', 'invalid_request',
    'debug_transport_failed'}


class ProbeError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def snapshot_expression(peer):
    # Written for this provider. No shipped JavaScript is copied or executed.
    return """(()=>{
 const s=window.RemotePlayStore_SteamUI;
 const fail=c=>{throw Error('FTB:'+c)};
 const id=v=>{if(typeof v==='number'){if(!Number.isSafeInteger(v)||v<0)fail('unsafe_client_id');v=String(v)}
 if(typeof v!=='string'||!/^\\d+$/.test(v)||BigInt(v)>18446744073709551615n)fail('unsafe_client_id');return v};
 if(!s||!Array.isArray(s.devices)||!Array.isArray(s.remoteClientStreams))fail('store_unavailable');
 const ds=s.devices.filter(d=>d.clientName===%s);
 if(ds.length===0)fail('peer_missing');if(ds.length!==1)fail('peer_ambiguous');
 const d=ds[0],cid=id(d.clientId);if(cid==='0')fail('unsafe_client_id');
 const streams=s.remoteClientStreams;
 const selected=streams.filter(r=>id(r.m_clientid)===cid&&id(r.m_gameid)==='250820');
 const other=streams.some(r=>id(r.m_clientid)!==cid||id(r.m_gameid)!=='250820')||
  s.devices.some(r=>r.clientName!==%s&&(r.unStreamingSessionID>0||String(r.status).includes('Streaming')));
 return {client_id:cid,can_stream_vr:d.bCanStreamVR===true,
 stream_associated:selected.length===1,other_stream_active:other||selected.length>1,
 generic_session_active:(s.settings?.unStreamingSessionID??0)>0,
 selected_session_active:(d.unStreamingSessionID??0)>0,
 stream_call_available:typeof SteamClient?.Apps?.StreamGame==='function',
 dashboard_call_available:typeof SteamClient?.OpenVR?.VROverlay?.HideDashboard==='function'};
 })()""" % (json.dumps(peer), json.dumps(peer))


def connect_expression(peer, client_id):
    # Recheck in the SAME synchronous expression immediately before the one call.
    return """(()=>{
 const s=window.RemotePlayStore_SteamUI;const fail=c=>{throw Error('FTB:'+c)};
 if(!s||!Array.isArray(s.devices)||!Array.isArray(s.remoteClientStreams))fail('store_unavailable');
 const ds=s.devices.filter(d=>d.clientName===%s);
 if(ds.length!==1||typeof ds[0].clientId!=='string'&&(!Number.isSafeInteger(ds[0].clientId)))fail('peer_changed');
 const d=ds[0];if(String(d.clientId)!==%s)fail('peer_changed');
 if(d.bCanStreamVR!==true)fail('peer_not_vr');
 if(s.remoteClientStreams.length!==0||(s.settings?.unStreamingSessionID??0)>0||
 s.devices.some(r=>r.unStreamingSessionID>0||String(r.status).includes('Streaming')))fail('other_stream_active');
 if(typeof SteamClient?.Apps?.StreamGame!=='function')fail('stream_call_unavailable');
 SteamClient.Apps.StreamGame(250820,d.clientId,-1);return {request_issued:true};
 })()""" % (json.dumps(peer), json.dumps(client_id))


def hide_expression(peer, client_id):
    return """(()=>{
 const s=window.RemotePlayStore_SteamUI;const fail=c=>{throw Error('FTB:'+c)};
 if(!s||!Array.isArray(s.devices)||!Array.isArray(s.remoteClientStreams))fail('store_unavailable');
 const ds=s.devices.filter(d=>d.clientName===%s);
 if(ds.length!==1||typeof ds[0].clientId!=='string'&&!Number.isSafeInteger(ds[0].clientId)||String(ds[0].clientId)!==%s)fail('peer_changed');
 if(ds[0].bCanStreamVR!==true)fail('peer_not_vr');
 const rows=s.remoteClientStreams;
 if(rows.length!==1||String(rows[0].m_clientid)!==%s||String(rows[0].m_gameid)!=='250820')fail('other_stream_active');
 if(typeof SteamClient?.OpenVR?.VROverlay?.HideDashboard!=='function')fail('dashboard_unavailable');
 return SteamClient.OpenVR.VROverlay.HideDashboard();
 })()""" % (json.dumps(peer), json.dumps(client_id), json.dumps(client_id))


class CDP:
    def __init__(self, timeout=5):
        self.timeout = timeout
        self.client = None
        self.identifier = 0
        self.deadline = time.monotonic() + timeout
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/json/list', timeout=timeout) as response:
                data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ProbeError('target_inventory_too_large')
            targets = json.loads(data)
            if not isinstance(targets, list):
                raise ProbeError('invalid_target_inventory')
            selected = [t for t in targets if isinstance(t, dict) and t.get('title') == 'SharedJSContext']
            if len(selected) != 1:
                raise ProbeError('debug_target_missing_or_ambiguous')
            target = selected[0]
            identifier = target.get('id')
            if (target.get('type') != 'page' or not isinstance(identifier, str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier)):
                raise ProbeError('invalid_debug_target')
            path = '/devtools/page/' + identifier
            if target.get('webSocketDebuggerUrl') != f'ws://127.0.0.1:{PORT}' + path:
                raise ProbeError('debug_target_not_loopback')
            self.client = socket.create_connection(('127.0.0.1', PORT), timeout=timeout)
            self.client.settimeout(timeout)
            self.deadline = time.monotonic() + timeout
            key = base64.b64encode(os.urandom(16)).decode()
            self.client.sendall((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\n'
                                f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            header = bytearray()
            while b'\r\n\r\n' not in header:
                header.extend(self._read(1))
                if len(header) > 8192:
                    raise ProbeError('handshake_too_large')
            lines = header.decode('ascii').split('\r\n')
            headers = {name.lower(): value.strip() for name, value in
                       (line.split(':', 1) for line in lines[1:] if ':' in line)}
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if (' 101 ' not in lines[0] or headers.get('sec-websocket-accept') != accept
                    or headers.get('upgrade', '').lower() != 'websocket'):
                raise ProbeError('handshake_refused')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None

    def _read(self, size):
        data = bytearray()
        while len(data) < size:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError('evaluation_timeout')
            self.client.settimeout(min(self.timeout, remaining))
            part = self.client.recv(size - len(data))
            if not part:
                raise ProbeError('debug_target_disconnected')
            data.extend(part)
        return bytes(data)

    def _send(self, data, opcode=1):
        if len(data) > LIMIT:
            raise ProbeError('request_too_large')
        mask = os.urandom(4)
        size = len(data)
        if size < 126:
            header = bytes([128 | opcode, 128 | size])
        elif size < 65536:
            header = bytes([128 | opcode, 254]) + struct.pack('!H', size)
        else:
            header = bytes([128 | opcode, 255]) + struct.pack('!Q', size)
        self.client.sendall(header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(data)))

    def _receive(self):
        fragments = bytearray()
        while True:
            start, length = self._read(2)
            if length & 128:
                raise ProbeError('invalid_server_frame')
            size = length & 127
            if size == 126:
                size = struct.unpack('!H', self._read(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self._read(8))[0]
            if size + len(fragments) > LIMIT:
                raise ProbeError('response_too_large')
            payload = self._read(size)
            opcode = start & 15
            if opcode == 8:
                raise ProbeError('debug_target_disconnected')
            if opcode == 9:
                self._send(payload, 10)
                continue
            if opcode not in (0, 1):
                raise ProbeError('invalid_server_frame')
            fragments.extend(payload)
            if start & 128:
                return json.loads(fragments)

    def evaluate(self, expression, await_promise=False):
        self.identifier += 1
        self.deadline = time.monotonic() + self.timeout
        self._send(json.dumps({'id': self.identifier, 'method': 'Runtime.evaluate',
                              'params': {'expression': expression, 'returnByValue': True,
                                         'awaitPromise': await_promise}}).encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            reply = self._receive()
            if not isinstance(reply, dict) or reply.get('id') != self.identifier:
                continue
            result = reply.get('result', {})
            if 'error' in reply or result.get('exceptionDetails'):
                # Do not expose native exceptions, Steam URLs, account IDs or arbitrary page output.
                text = str(result.get('exceptionDetails', {}).get('exception', {}).get('description', ''))
                code = next((code for code in ERRORS if 'FTB:' + code in text), 'evaluation_failed')
                raise ProbeError(code)
            return result.get('result', {}).get('value')
        raise ProbeError('evaluation_timeout')


def operate(cdp, operation, peer, client_id=None, deadline_seconds=30, *, clock=time.monotonic, sleep=time.sleep):
    state = cdp.evaluate(snapshot_expression(peer))
    if not isinstance(state, dict):
        raise ProbeError('invalid_state')
    if client_id is not None and state.get('client_id') != client_id:
        raise ProbeError('peer_changed')
    reply = dict(state, schema_version=1, operation=operation, request_issued=False,
                 timed_out=False, dashboard_visible=None)
    if operation == 'observe':
        return reply
    if state.get('can_stream_vr') is not True:
        raise ProbeError('peer_not_vr')
    if state.get('other_stream_active') is not False:
        raise ProbeError('other_stream_active')
    if operation == 'connect':
        if state.get('stream_associated') or state.get('generic_session_active') or state.get('selected_session_active'):
            raise ProbeError('stream_already_active')
        request = cdp.evaluate(connect_expression(peer, state['client_id']))
        if request != {'request_issued': True}:
            raise ProbeError('invalid_request_result')
        reply['request_issued'] = True
        deadline = clock() + deadline_seconds
        while clock() < deadline:
            current = cdp.evaluate(snapshot_expression(peer))
            if current.get('client_id') != state['client_id']:
                raise ProbeError('peer_changed')
            if current.get('other_stream_active'):
                raise ProbeError('other_stream_active')
            for key in ('client_id', 'can_stream_vr', 'stream_associated', 'other_stream_active'):
                reply[key] = current.get(key)
            if current.get('stream_associated') is True:
                return reply
            sleep(min(.25, max(0, deadline - clock())))
        reply['timed_out'] = True
        return reply
    if operation == 'hide-dashboard':
        if state.get('stream_associated') is not True:
            raise ProbeError('stream_not_associated')
        if state.get('dashboard_call_available') is not True:
            raise ProbeError('dashboard_unavailable')
        visible = cdp.evaluate('SteamClient.OpenVR.VROverlay.IsDashboardVisible()', True)
        if type(visible) is not bool:
            raise ProbeError('invalid_dashboard_state')
        reply['dashboard_visible'] = visible
        if not visible:
            return reply
        cdp.evaluate(hide_expression(peer, state['client_id']), True)
        reply['request_issued'] = True
        deadline = clock() + deadline_seconds
        while clock() < deadline:
            visible = cdp.evaluate('SteamClient.OpenVR.VROverlay.IsDashboardVisible()', True)
            if type(visible) is not bool:
                raise ProbeError('invalid_dashboard_state')
            reply['dashboard_visible'] = visible
            if not visible:
                return reply
            sleep(min(.25, max(0, deadline - clock())))
        reply['timed_out'] = True
        return reply
    raise ProbeError('operation_unavailable')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--operation', choices=['observe', 'connect', 'hide-dashboard'], required=True)
    parser.add_argument('--expected-peer', required=True)
    parser.add_argument('--client-id')
    parser.add_argument('--deadline-seconds', type=int, default=30)
    args = parser.parse_args(argv)
    cdp = None
    try:
        if (not 1 <= args.deadline_seconds <= 30 or not 1 <= len(args.expected_peer) <= 128
                or any(ord(c) < 32 for c in args.expected_peer)):
            raise ProbeError('invalid_request')
        cdp = CDP()
        result = operate(cdp, args.operation, args.expected_peer, args.client_id, args.deadline_seconds)
        print(json.dumps(dict(result, ok=True), allow_nan=False))
        return 0
    except ProbeError as error:
        print(json.dumps({'ok': False, 'error_code': error.code}))
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({'ok': False, 'error_code': 'debug_transport_failed'}))
        return 2
    finally:
        if cdp is not None:
            cdp.close()


if __name__ == '__main__':
    raise SystemExit(main())
