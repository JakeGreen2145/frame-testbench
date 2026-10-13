"""Optional external UGEVR test orchestration, separate from the headset's CLI."""
import argparse
import json
import re
from pathlib import Path
import signal
import subprocess
import sys
import threading

from . import transport
from .streamed_session import Orchestrator
from .ugevr import Client, validate_steam_app_id, write_scalars


def steam_id_argument(text):
    if not re.fullmatch(r'[0-9]+', text):
        raise argparse.ArgumentTypeError('Steam application ID must be positive decimal digits')
    try:
        return validate_steam_app_id(int(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def run_headset_cli(arguments):
    """Run existing CLI operations with a bound, without a shell or new remote commands."""
    # Renewal must finish before Orchestrator's 25-second cleanup join.
    command = arguments[4] if arguments[:1] == ['--host'] else arguments[0]
    timeout = 20 if command == 'session' else 120
    completed = subprocess.run(
        [sys.executable, '-c', 'from frame_testbench.cli import main; raise SystemExit(main())', *arguments],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
    if len(completed.stdout.encode('utf-8')) > 1024 * 1024:
        raise RuntimeError('headset CLI reply exceeds 1 MiB')
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        raise RuntimeError('headset CLI returned invalid JSON') from None
    if completed.returncode or not isinstance(result, dict) or result.get('ok') is not True:
        raise RuntimeError('headset CLI failed')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controller-dll', type=Path, required=True)
    parser.add_argument('--dotnet', default='dotnet')
    parser.add_argument('--session-dir', type=Path, required=True,
                        help='unique empty controller session directory owned by this test')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('stop')
    run = commands.add_parser('run', help='launch and retain bounded evidence; stream association remains unknown')
    run.add_argument('--exe', type=Path, required=True)
    run.add_argument('--game-root', type=Path)
    run.add_argument('--distribution-root', type=Path)
    run.add_argument('--deployment-script', type=Path)
    run.add_argument('--prepared', action='store_true', help='explicit development-only predeployed launch')
    run.add_argument('--game-arg', action='append', default=[])
    run.add_argument('--steam-app-id', type=steam_id_argument,
                     help='set SteamAppId and SteamGameId only in the controller-owned child')
    run.add_argument('--host', required=True, help='fixed headset SSH alias')
    run.add_argument('--remote-root', default='dev/frame-testbench')
    run.add_argument('--expected-peer', required=True)
    run.add_argument('--artifacts-dir', type=Path, default=Path('artifacts/streamed'))
    run.add_argument('--duration-seconds', type=int, default=180)
    run.add_argument('--chunk-seconds', type=int, default=30)
    args = parser.parse_args(argv)
    try:
        client = Client([args.dotnet, str(args.controller_dll.absolute())], args.session_dir)
        if args.command in ('status', 'stop'):
            status = getattr(client, args.command)()
            print(write_scalars(status.values), end='')
            return (0 if args.command == 'status' or status.cleanup_outcome == 'observed'
                    else 3 if status.cleanup_outcome == 'failed' else 4)
        deployment = (args.game_root, args.distribution_root, args.deployment_script)
        if args.prepared and any(deployment):
            raise ValueError('prepared launch cannot include deployment options')
        if not args.prepared and not all(deployment):
            raise ValueError('run requires game root, distribution root and deployment script')
        transport.remote_argv(args.host, args.remote_root, [])  # Validate before host launch.
        prefix = ['--host', args.host, '--remote-root', args.remote_root]
        cancellation = threading.Event()
        previous = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda signum, frame: cancellation.set())
            orchestrator = Orchestrator(client, lambda arguments: run_headset_cli(prefix + arguments),
                                        args.artifacts_dir, args.expected_peer)
            result = orchestrator.run(
                args.exe, duration_seconds=args.duration_seconds, chunk_seconds=args.chunk_seconds,
                cancelled=cancellation, launch_options={
                    'game_root': args.game_root, 'distribution_root': args.distribution_root,
                    'deployment_script': args.deployment_script, 'prepared': args.prepared,
                    'game_arguments': args.game_arg, 'steam_app_id': args.steam_app_id})
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        print(result.metadata(), end='')
        return 130 if result.outcome == 'cancelled' else 3 if result.outcome == 'failed' else 4
    except (ValueError, OSError, RuntimeError) as error:
        print(write_scalars({'error': str(error)}), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
