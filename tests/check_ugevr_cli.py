"""Opt-in check against UGEVR's actual controller and CPU-only producer fixture."""
import argparse
from pathlib import Path
import time

from frame_testbench.ugevr import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-dll', type=Path, required=True)
    parser.add_argument('--check-child', type=Path, required=True)
    parser.add_argument('--session-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.check_child.stem != 'UGEVR.Controller.Checks' or not args.check_child.is_file():
        parser.error('check-child must be the built UGEVR.Controller.Checks apphost fixture')
    client = Client(['dotnet', str(args.controller_dll.absolute())], args.session_dir)
    status = None
    try:
        status = client.launch(args.check_child, prepared=True, game_arguments=['child', 'natural'])
        deadline = time.monotonic() + 2
        while not status.ready and time.monotonic() < deadline:
            time.sleep(.05)
            status = client.status()
        assert status.ready, 'actual controlled injector producer did not become ready'
        assert status.values['stream_ready'] == 'false', 'host readiness claimed stream readiness'
        print('PASS actual controller launch and correlated producer readiness')
        assert client.status().run_id == status.run_id
        print('PASS actual controller status identity')
    finally:
        # The fixture exits after three seconds. No forced process termination or VR runtime.
        stopped = client.stop()
        assert stopped.cleanup_outcome == 'observed', stopped.cleanup_reason
        if status is not None:
            assert stopped.run_id == status.run_id
        print('PASS actual controller graceful stop after controlled child natural exit')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
