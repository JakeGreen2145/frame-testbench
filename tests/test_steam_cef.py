"""Source-only provider checks. All JS, processes and CDP frames are controlled fixtures."""
import base64
import hashlib
import io
import json
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from frame_testbench.steam_cef import ProviderError, SteamCEF, StreamState, ssh_worker, main
from frame_testbench import steam_cef_remote as remote

CLIENT = '18446744073709551615'
TARGET = {'id': 'AA11', 'title': 'SharedJSContext', 'type': 'page',
          'webSocketDebuggerUrl': 'ws://127.0.0.1:8080/devtools/page/AA11'}


def reply(operation='observe', **changes):
    data = dict(ok=True, schema_version=1, operation=operation, client_id=CLIENT,
                can_stream_vr=True, stream_associated=False, other_stream_active=False,
                request_issued=False, timed_out=False, dashboard_visible=None)
    data.update(changes)
    return data


class ProviderTests(unittest.TestCase):
    def test_typed_uint64_reply_and_invalid_flags(self):
        self.assertEqual(StreamState.parse(reply()).client_id, CLIENT)
        for data in [reply(client_id=18446744073709551615), reply(client_id='18446744073709551616'),
                     reply(client_id='0'), reply(client_id='001'), reply(stream_associated='true')]:
            with self.subTest(data=data), self.assertRaises(ProviderError):
                StreamState.parse(data)

    def test_requested_is_separate_from_association_and_connection_is_owned_only_after_observation(self):
        responses = [reply(), reply('connect', request_issued=True, timed_out=True)]
        calls = []
        def runner(args, source, timeout, cancelled):
            calls.append((args, source, timeout))
            return 0, json.dumps(responses.pop(0))
        provider = SteamCEF('frame', 'my pc;$(false)', runner=runner)
        state = provider.connect()
        self.assertEqual(provider.last_request, 'requested')
        self.assertFalse(state.stream_associated)
        self.assertFalse(provider.owns_stream)
        self.assertIn("'my pc;$(false)'", calls[0][0][-1])
        self.assertIn('--client-id ' + CLIENT, calls[1][0][-1])
        self.assertEqual(calls[0][0][:3], ['ssh', '-o', 'BatchMode=yes'])
        self.assertIn('StrictHostKeyChecking=yes', calls[0][0])
        self.assertIn('class CDP', calls[0][1])

    def test_new_associated_stream_is_owned_but_existing_stream_is_reused(self):
        responses = [reply(), reply('connect', request_issued=True, stream_associated=True)]
        provider = SteamCEF('frame', 'pc', runner=lambda *_: (0, json.dumps(responses.pop(0))))
        self.assertTrue(provider.connect().stream_associated)
        self.assertTrue(provider.owns_stream)
        reused_calls = []
        provider = SteamCEF('frame', 'pc', runner=lambda *args: (
            reused_calls.append(args) or 0, json.dumps(reply(stream_associated=True))))
        provider.connect()
        self.assertEqual(provider.last_request, 'reused')
        self.assertFalse(provider.owns_stream)
        self.assertEqual(len(reused_calls), 1)
        self.assertEqual(provider.disconnect(), 'unavailable')
        self.assertEqual(len(reused_calls), 1)

    def test_other_stream_or_missing_capability_blocks_before_mutation(self):
        for fields in [dict(other_stream_active=True), dict(can_stream_vr=False)]:
            calls = []
            provider = SteamCEF('frame', 'pc', runner=lambda *args: (
                calls.append(args) or 0, json.dumps(reply(**fields))))
            with self.assertRaises(ProviderError):
                provider.connect()
            self.assertEqual(len(calls), 1)

    def test_peer_change_bad_operation_and_remote_errors_are_redacted(self):
        responses = [reply(), reply(client_id='42')]
        provider = SteamCEF('frame', 'pc', runner=lambda *_: (0, json.dumps(responses.pop(0))))
        provider.observe()
        with self.assertRaises(ProviderError):
            provider.observe()
        for code, output in [(2, '{"ok":false,"error_code":"secret TOKEN=value"}'),
                             (2, '{"ok":false,"error_code":"secrettoken"}'),
                             (0, 'secret invalid output'), (0, json.dumps(reply('connect')))]:
            provider = SteamCEF('frame', 'pc', runner=lambda *_: (code, output))
            with self.assertRaises(ProviderError) as caught:
                provider.observe()
            self.assertNotIn('TOKEN', str(caught.exception))
            self.assertNotIn('secret', str(caught.exception))

    def test_explicit_cli_activation_and_disconnect_do_no_transport(self):
        with patch('frame_testbench.steam_cef.ssh_worker') as runner, patch('builtins.print'):
            self.assertEqual(main(['--host', 'frame', '--expected-peer', 'pc', 'connect']), 2)
            self.assertEqual(main(['--host', 'frame', '--expected-peer', 'pc', '--experimental', 'disconnect']), 4)
        runner.assert_not_called()

    def test_real_controlled_ssh_transport_stdin_bounds_timeout_and_cancel(self):
        command = [sys.executable, '-c', 'import sys; print(len(sys.stdin.read()))']
        code, output = ssh_worker(command, 'fixture source', 3, threading.Event())
        self.assertEqual((code, output.strip()), (0, '14'))
        with self.assertRaises(TimeoutError):
            ssh_worker([sys.executable, '-c', 'import time; time.sleep(10)'], 'x', .1, threading.Event())
        cancelled = threading.Event()
        cancelled.set()
        with patch('subprocess.Popen') as process, self.assertRaises(InterruptedError):
            ssh_worker(command, 'x', 3, cancelled)
        process.assert_not_called()
        with self.assertRaises(ProviderError):
            ssh_worker([sys.executable, '-c', 'print("x" * 65537)'], 'x', 3, threading.Event())


@unittest.skipUnless(shutil.which('node'), 'Node is needed only for controlled guard execution')
class GuardTests(unittest.TestCase):
    def execute(self, expression, *, devices=None, streams=None, peer='pc'):
        devices = devices if devices is not None else [dict(clientName=peer, clientId=CLIENT, bCanStreamVR=True)]
        streams = streams or []
        program = ('const calls=[];const window={RemotePlayStore_SteamUI:'
                   + json.dumps(dict(devices=devices, remoteClientStreams=streams, settings={'unStreamingSessionID': 0}))
                   + '};const SteamClient={Apps:{StreamGame:(...args)=>calls.push(args)},'
                   + 'OpenVR:{VROverlay:{HideDashboard:()=>calls.push("hide")}}};'
                   + 'try{const result=' + expression + ';console.log(JSON.stringify({result,calls}));}'
                   + 'catch(e){console.log(JSON.stringify({error:e.message,calls}));}')
        completed = subprocess.run(['node', '-e', program], capture_output=True, text=True, timeout=5, check=True)
        return json.loads(completed.stdout)

    def test_connect_preserves_uint64_and_fixed_app_id(self):
        result = self.execute(remote.connect_expression('pc', CLIENT))
        self.assertEqual(result['calls'], [[250820, CLIENT, -1]])
        self.assertEqual(result['result'], {'request_issued': True})

    def test_fresh_same_expression_guards_missing_duplicate_wrong_peer_capability_or_stream(self):
        cases = [([], [], 'FTB:peer_changed'),
                 ([dict(clientName='pc', clientId=CLIENT, bCanStreamVR=True)] * 2, [], 'FTB:peer_changed'),
                 ([dict(clientName='pc', clientId='42', bCanStreamVR=True)], [], 'FTB:peer_changed'),
                 ([dict(clientName='pc', clientId=CLIENT, bCanStreamVR=False)], [], 'FTB:peer_not_vr'),
                 ([dict(clientName='pc', clientId=CLIENT, bCanStreamVR=True)],
                  [dict(m_clientid=CLIENT, m_gameid='250820')], 'FTB:other_stream_active')]
        for devices, streams, error in cases:
            result = self.execute(remote.connect_expression('pc', CLIENT), devices=devices, streams=streams)
            self.assertEqual(result.get('error'), error)
            self.assertEqual(result['calls'], [])

    def test_snapshot_accepts_zero_generic_session_and_rejects_lossy_numeric_ids(self):
        stream = dict(m_clientid=CLIENT, m_gameid='250820')
        result = self.execute(remote.snapshot_expression('pc'), streams=[stream])['result']
        self.assertTrue(result['stream_associated'])
        self.assertFalse(result['generic_session_active'])
        self.assertEqual(result['client_id'], CLIENT)
        invalid = self.execute(remote.snapshot_expression('pc'), devices=[
            dict(clientName='pc', clientId=18446744073709551615, bCanStreamVR=True)])
        self.assertEqual(invalid['error'], 'FTB:unsafe_client_id')

    def test_peer_name_is_data_and_hide_requires_only_matching_stream(self):
        peer = 'pc";throw Error("injected")//'
        result = self.execute(remote.connect_expression(peer, CLIENT), peer=peer)
        self.assertEqual(result['calls'], [[250820, CLIENT, -1]])
        result = self.execute(remote.hide_expression('pc', CLIENT), streams=[dict(m_clientid=CLIENT, m_gameid='42')])
        self.assertEqual(result['error'], 'FTB:other_stream_active')
        self.assertEqual(result['calls'], [])
        result = self.execute(remote.hide_expression('pc', CLIENT), streams=[dict(m_clientid=CLIENT, m_gameid='250820')])
        self.assertEqual(result['calls'], ['hide'])


class FakeCDP:
    def __init__(self, values):
        self.values = list(values)
        self.expressions = []
    def evaluate(self, expression, await_promise=False):
        self.expressions.append((expression, await_promise))
        return self.values.pop(0)


class OperationTests(unittest.TestCase):
    def test_single_request_polls_association_and_timeout_keeps_unknown(self):
        empty = reply()
        cdp = FakeCDP([empty, {'request_issued': True}, empty, reply(stream_associated=True)])
        now = [0.0]
        data = remote.operate(cdp, 'connect', 'pc', CLIENT, 1,
                              clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
        self.assertTrue(data['stream_associated'])
        self.assertTrue(data['request_issued'])
        self.assertEqual(sum('SteamClient.Apps.StreamGame(' in expr for expr, _ in cdp.expressions), 1)
        now[0] = 0
        cdp = FakeCDP([empty, {'request_issued': True}, empty])
        data = remote.operate(cdp, 'connect', 'pc', CLIENT, .1,
                              clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
        self.assertTrue(data['timed_out'])
        self.assertFalse(data['stream_associated'])

    def test_dashboard_awaits_and_polls_after_one_mutation(self):
        cdp = FakeCDP([reply(stream_associated=True, dashboard_call_available=True), True, None, True, False])
        now = [0.0]
        data = remote.operate(cdp, 'hide-dashboard', 'pc', CLIENT, 1,
                              clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
        self.assertIs(data['dashboard_visible'], False)
        self.assertEqual(sum('return SteamClient.OpenVR.VROverlay.HideDashboard();' in expr for expr, _ in cdp.expressions), 1)
        self.assertTrue(all(awaited for expr, awaited in cdp.expressions if 'IsDashboardVisible' in expr))


class FakeSocket:
    def __init__(self, replies=(), bad_accept=False):
        self.buffer = bytearray()
        self.replies = list(replies)
        self.bad_accept = bad_accept
        self.closed = False
        self.sent = []
    def settimeout(self, seconds):
        self.timeout = seconds
    def close(self):
        self.closed = True
    def recv(self, size):
        value = self.buffer[:size]
        del self.buffer[:size]
        return bytes(value)
    def sendall(self, data):
        self.sent.append(data)
        if data.startswith(b'GET '):
            key = re.search(b'Sec-WebSocket-Key: ([^\r]+)', data)[1]
            accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
            self.buffer.extend(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: '
                               + (b'wrong' if self.bad_accept else accept) + b'\r\n\r\n')
        else:
            length = data[1] & 127
            offset = 2
            if length == 126:
                length = struct.unpack('!H', data[2:4])[0]
                offset = 4
            mask = data[offset:offset+4]
            request = json.loads(bytes(value ^ mask[index % 4] for index, value in enumerate(data[offset+4:offset+4+length])))
            reply_value = self.replies.pop(0)
            response = {'id': request['id'], 'result': reply_value}
            encoded = json.dumps(response).encode()
            header = bytes([129, len(encoded)]) if len(encoded) < 126 else bytes([129, 126]) + struct.pack('!H', len(encoded))
            self.buffer.extend(header + encoded)


class CDPTests(unittest.TestCase):
    def test_real_frame_handshake_scoped_target_and_reply(self):
        socket = FakeSocket([{'result': {'value': {'fixture': True}}}])
        with patch.object(remote.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps([TARGET]).encode())),\
             patch.object(remote.socket, 'create_connection', return_value=socket) as connect:
            cdp = remote.CDP()
            self.assertEqual(cdp.evaluate('fixture()'), {'fixture': True})
            connect.assert_called_once_with(('127.0.0.1', 8080), timeout=5)
            cdp.close()
        self.assertTrue(socket.closed)

    def test_wrong_target_external_url_duplicate_and_bad_accept_reject(self):
        for inventory in [[dict(TARGET, webSocketDebuggerUrl='ws://remote:8080/devtools/page/AA11')],
                          [TARGET, TARGET], [], [dict(TARGET, id='../escape')]]:
            with patch.object(remote.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(inventory).encode())),\
                 patch.object(remote.socket, 'create_connection') as connect, self.assertRaises(remote.ProbeError):
                remote.CDP()
            connect.assert_not_called()
        socket = FakeSocket(bad_accept=True)
        with patch.object(remote.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps([TARGET]).encode())),\
             patch.object(remote.socket, 'create_connection', return_value=socket), self.assertRaises(remote.ProbeError):
            remote.CDP()
        self.assertTrue(socket.closed)

    def test_evaluation_errors_are_codes_without_native_secrets(self):
        socket = FakeSocket([{'exceptionDetails': {'exception': {'description': 'native secret TOKEN=value'}}}])
        with patch.object(remote.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps([TARGET]).encode())),\
             patch.object(remote.socket, 'create_connection', return_value=socket):
            cdp = remote.CDP()
            with self.assertRaises(remote.ProbeError) as caught:
                cdp.evaluate('fixture()')
            self.assertEqual(str(caught.exception), 'evaluation_failed')
            cdp.close()

    def test_frame_size_and_wall_clock_deadline_are_bounded(self):
        cdp = remote.CDP.__new__(remote.CDP)
        cdp.timeout = 5
        cdp.client = FakeSocket()
        cdp.deadline = time.monotonic() - 1
        with self.assertRaises(remote.ProbeError) as caught:
            cdp._read(1)
        self.assertEqual(str(caught.exception), 'evaluation_timeout')
        cdp.deadline = time.monotonic() + 5
        cdp.client.buffer.extend(bytes([129, 127]) + struct.pack('!Q', 65537))
        with self.assertRaises(remote.ProbeError) as caught:
            cdp._receive()
        self.assertEqual(str(caught.exception), 'response_too_large')
        cdp.close()


if __name__ == '__main__':
    unittest.main()
