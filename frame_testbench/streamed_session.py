"""Test-only streamed session orchestration across independent PC and headset clients."""
from dataclasses import dataclass, field
import math
from pathlib import Path
import threading
import time
import uuid

from .ugevr import HostStatus, ProtocolError, write_scalars


@dataclass(frozen=True)
class Observation:
    """An independent observer must associate fresh evidence with this exact run."""
    run_id: str
    peer: str
    observed_at: float
    marker_left: str | None = None
    marker_right: str | None = None
    input_receipt: str | None = None
    evidence_reference: str = ''


@dataclass
class RunResult:
    test_id: str
    host_run_id: str = ''
    host_ready: bool = False
    stream_request: str = 'unavailable'
    stream_association: str = 'unknown'
    stream_client_id: str = ''
    stream_owned: bool = False
    dashboard_visibility: str = 'unknown'
    peer_evidence: str = 'unknown'
    both_eye_evidence: str = 'unknown'
    input_forwarding_evidence: str = 'unknown'
    mode: str = 'assisted'
    outcome: str = 'incomplete'
    host_cleanup: str = 'not_started'
    host_cleanup_complete: bool = False
    host_deployment_restored: str = 'unknown'
    host_cleanup_reason: str = ''
    headset_cleanup: str = 'not_started'
    stream_cleanup: str = 'unavailable'
    stream_association_removed: bool = False
    stream_windows_runtime: str = 'unknown'
    requested_seconds: int = 0
    recorded_seconds: int = 0
    error: str = ''
    artifacts: list[str] = field(default_factory=list)
    evidence_references: list[str] = field(default_factory=list)

    @property
    def association_verified(self):
        return (self.outcome not in ('failed', 'cancelled') and self.host_ready and self.peer_evidence == 'observed'
                and self.both_eye_evidence == 'observed'
                and self.input_forwarding_evidence == 'observed')

    def metadata(self):
        # This verifies association only, never gameplay checkpoints, optics or comfort.
        values = {key: value for key, value in vars(self).items()
                  if key not in ('artifacts', 'evidence_references')}
        values['schema_version'] = '1'
        values['association_verified'] = str(self.association_verified).lower()
        for index, artifact in enumerate(self.artifacts):
            values[f'artifact_{index}'] = artifact
        for index, reference in enumerate(self.evidence_references):
            values[f'evidence_{index}'] = reference
        return write_scalars(values)


class Orchestrator:
    def __init__(self, host, bench, artifacts_dir, expected_peer, *,
                 stream_request=None, observe=None, clock=time.monotonic,
                 wait=None, heartbeat_seconds=30, stream_provider=None, hide_dashboard=False):
        """bench calls the existing CLI; no ownership of Steam pairing or runtime settings."""
        if not expected_peer:
            raise ValueError('expected peer identity is required')
        if not 1 <= heartbeat_seconds <= 30:
            raise ValueError('lease heartbeat must be 1..30 seconds')
        self.host, self.bench = host, bench
        self.artifacts_dir = Path(artifacts_dir).absolute()
        self.expected_peer = expected_peer
        self.stream_request, self.observe = stream_request, observe
        if stream_provider is not None and stream_request is not None:
            raise ValueError('choose one stream provider or request callback')
        if hide_dashboard and stream_provider is None:
            raise ValueError('dashboard control requires an explicit stream provider')
        self.stream_provider, self.hide_dashboard = stream_provider, hide_dashboard
        self.clock = clock
        self.wait = wait or (lambda event, seconds: event.wait(seconds))
        self.heartbeat_seconds = heartbeat_seconds

    def _bench(self, arguments):
        reply = self.bench(arguments)
        if not isinstance(reply, dict) or reply.get('ok') is not True:
            raise RuntimeError('headset CLI operation failed')
        return reply

    def _observe(self, status, result):
        if self.observe is None:
            return
        observation = self.observe(status, result.test_id)
        if observation is None:
            return
        if not isinstance(observation, Observation) or observation.run_id != status.run_id:
            raise ProtocolError('evidence belongs to a different run')
        if observation.peer != self.expected_peer:
            raise ProtocolError('observed stream peer differs from requested peer')
        if not observation.evidence_reference:
            raise ProtocolError('observation requires an independent evidence reference')
        age = self.clock() - observation.observed_at
        if not math.isfinite(age) or not 0 <= age <= 15:
            raise ProtocolError('observation is stale or uses an unassociated clock')
        result.peer_evidence = 'observed'
        # Require both views to carry the same run ID, not just nonempty pixels.
        if observation.marker_left == status.run_id and observation.marker_right == status.run_id:
            result.both_eye_evidence = 'observed'
        elif observation.marker_left is not None or observation.marker_right is not None:
            raise ProtocolError('both-eye game marker association failed')
        # Receipt comes from the Windows game observer, not headset-local readback.
        if observation.input_receipt == result.test_id:
            result.input_forwarding_evidence = 'observed'
        elif observation.input_receipt is not None:
            raise ProtocolError('Windows game input receipt association failed')
        result.evidence_references.append(observation.evidence_reference)

    def run(self, executable, *, duration_seconds=180, readiness_seconds=60,
            chunk_seconds=30, cancelled=None, launch_options=None):
        if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int)
                or not 180 <= duration_seconds <= 300):
            raise ValueError('gameplay observation duration must be 180..300 seconds')
        if (isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, int)
                or not 1 <= chunk_seconds <= 60):
            raise ValueError('recording chunk must be 1..60 seconds')
        if not math.isfinite(readiness_seconds) or not 1 <= readiness_seconds <= 300:
            raise ValueError('readiness timeout must be 1..300 seconds')
        cancelled = cancelled or threading.Event()
        result = RunResult(str(uuid.uuid4()))
        result.requested_seconds = duration_seconds
        directory = self.artifacts_dir / result.test_id
        directory.mkdir(parents=True, exist_ok=False)
        done = threading.Event()
        heartbeat_errors = []
        heartbeat = None
        headset_attempted = host_attempted = False

        def renew():
            while not done.wait(self.heartbeat_seconds):
                try:
                    if self._bench(['session', 'renew']).get('active') is not True:
                        raise RuntimeError('headset lease expired instead of renewing')
                except Exception as error:
                    heartbeat_errors.append(str(error))
                    cancelled.set()
                    return

        def check():
            if heartbeat_errors:
                raise RuntimeError('headset lease renewal failed: ' + heartbeat_errors[0])
            if cancelled.is_set():
                raise InterruptedError('test run cancelled')

        try:
            check()
            # Refuse an existing shared lease rather than taking another client's overrides.
            if self._bench(['session', 'status']).get('active') is not False:
                raise RuntimeError('headset already has an active test lease')
            headset_attempted = True
            lease = self._bench(['session', 'begin', '--timeout', '120'])
            if lease.get('active') is not True:
                raise RuntimeError('headset test lease did not become active')
            result.headset_cleanup = 'pending'
            heartbeat = threading.Thread(target=renew, name='frame-lease-renewal', daemon=True)
            heartbeat.start()
            if self.stream_provider is not None:
                # The wireless runtime is a prerequisite. An unconnected host can return OpenVR216.
                result.mode = self.stream_provider.mode
                stream = self.stream_provider.connect(cancelled)
                result.stream_request = self.stream_provider.last_request
                result.stream_client_id = stream.client_id
                result.stream_owned = self.stream_provider.owns_stream
                result.stream_association = 'observed' if stream.stream_associated else 'unknown'
                if not stream.stream_associated or stream.other_stream_active:
                    raise TimeoutError('intended SteamVR stream association was not observed')
                check()
            host_attempted = True
            result.host_cleanup = 'pending'
            status = self.host.launch(executable, cancelled=cancelled, **(launch_options or {}))
            result.host_run_id = status.run_id
            deadline = self.clock() + readiness_seconds
            while not status.ready:
                check()
                if status.state in ('failed', 'stopped') or not status.process_alive:
                    raise RuntimeError('host game failed before readiness: ' + status.values.get('last_error', ''))
                if self.clock() >= deadline:
                    raise TimeoutError('host injector/runtime/render readiness timed out')
                self.wait(cancelled, .25)
                status = self.host.status(cancelled)
            result.host_ready = True
            if self.stream_provider is not None and self.hide_dashboard:
                dashboard = self.stream_provider.hide_dashboard(cancelled)
                result.dashboard_visibility = ('hidden' if dashboard.dashboard_visible is False
                                               else 'visible' if dashboard.dashboard_visible is True else 'unknown')
            if self.stream_request is not None:
                # Request must use a separately verified control. It cannot assert stream readiness.
                request = self.stream_request(status, self.expected_peer)
                if request not in ('requested', 'assisted', 'unavailable'):
                    raise ProtocolError('stream control returned an invalid request outcome')
                result.stream_request = request
            self._observe(status, result)
            deadline = self.clock() + duration_seconds
            index = 0
            while self.clock() < deadline:
                check()
                status = self.host.status(cancelled)
                if not status.ready:
                    raise RuntimeError('host lost injector/runtime/render readiness')
                if self.stream_provider is not None:
                    stream = self.stream_provider.observe(cancelled)
                    result.stream_association = 'observed' if stream.stream_associated else 'unknown'
                    if not stream.stream_associated or stream.other_stream_active:
                        raise RuntimeError('intended SteamVR stream association was lost')
                self._observe(status, result)
                check()
                # Independent stereo captures are retained alongside left-eye continuous video.
                capture_dir = directory / f'stereo-{index:03d}'
                self._bench(['capture', '--fetch', str(capture_dir)])
                result.artifacts.append(str(capture_dir))
                check()
                remaining = max(0, deadline - self.clock())
                if remaining < 1:
                    break
                duration = min(chunk_seconds, int(remaining))
                record_dir = directory / f'record-{index:03d}'
                self._bench(['record', '--duration', str(duration), '--fps', '30',
                             '--view', 'headset', '--fetch', str(record_dir)])
                result.recorded_seconds += duration
                result.artifacts.append(str(record_dir))
                index += 1
                check()
            result.outcome = 'association_observed' if result.association_verified else 'incomplete'
        except InterruptedError as error:
            result.outcome, result.error = 'cancelled', str(error)
        except Exception as error:
            result.outcome, result.error = 'failed', str(error)
        finally:
            done.set()
            if heartbeat is not None:
                # Existing transport calls are bounded; join before ending its shared lease.
                heartbeat.join(timeout=25)
            if headset_attempted:
                try:
                    if heartbeat is not None and heartbeat.is_alive():
                        raise RuntimeError('lease renewal still running; guard expiry remains responsible')
                    self._bench(['session', 'end'])
                    result.headset_cleanup = 'requested'
                    if self._bench(['session', 'status']).get('active') is False:
                        result.headset_cleanup = 'observed'
                except Exception as error:
                    result.headset_cleanup = 'failed'
                    result.error += '; headset cleanup: ' + str(error)
            if host_attempted:
                try:
                    stopped = self.host.stop()
                    result.host_run_id = result.host_run_id or stopped.run_id
                    result.host_cleanup = stopped.cleanup_outcome
                    result.host_cleanup_complete = stopped.cleanup_complete
                    result.host_deployment_restored = stopped.deployment_restored
                    result.host_cleanup_reason = stopped.cleanup_reason
                    if result.host_cleanup == 'failed':
                        result.error += '; host cleanup: ' + result.host_cleanup_reason
                        if result.outcome != 'cancelled':
                            result.outcome = 'failed'
                except Exception as error:
                    result.host_cleanup = 'failed'
                    result.host_cleanup_reason = str(error)
                    result.error += '; host cleanup: ' + str(error)
                    if result.outcome != 'cancelled':
                        result.outcome = 'failed'
            if self.stream_provider is not None:
                result.stream_request = self.stream_provider.last_request
                # Only a newly owned receipt may stop its stream. Host runtime has a separate observer.
                if self.stream_provider.owns_stream:
                    try:
                        cleanup = self.stream_provider.disconnect()
                        if isinstance(cleanup, str):
                            result.stream_cleanup = cleanup
                        else:
                            result.stream_cleanup = cleanup.outcome
                            result.stream_association_removed = cleanup.association_removed
                            result.stream_windows_runtime = cleanup.windows_runtime_state
                    except Exception as error:
                        result.stream_cleanup = 'failed'
                        result.error += '; stream cleanup: ' + str(error)
                        if result.outcome != 'cancelled':
                            result.outcome = 'failed'
                elif result.stream_request == 'reused':
                    result.stream_cleanup = 'unowned'
            (directory / 'session.yaml').write_text(result.metadata(), encoding='utf-8')
        return result
