#!/usr/bin/env python3
"""Install the session guard for this checkout, without restarting SteamVR."""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from frame_testbench.install import atomic

MARKER = '# Managed by frame-testbench session guard.\n'


def unit(root):
    root = str(Path(root).resolve())
    if any(ord(c) < 32 for c in root):
        raise ValueError('checkout path must not contain control characters')
    # WorkingDirectory is a scalar path, not an ExecStart argument. Quotes
    # would become part of the path. Only systemd specifiers need escaping.
    escaped = root.replace('%', '%%')
    return MARKER + f'''[Unit]
Description=Restore testbench input and display standby after session expiry
After=steamvr.service

[Service]
Type=notify
WorkingDirectory={escaped}
ExecStart=/usr/bin/python3 -m frame_testbench.session
ExecStopPost=-/usr/bin/python3 -m frame_testbench.session --cleanup
Restart=always
RestartSec=2
TimeoutStartSec=45
TimeoutStopSec=30

[Install]
WantedBy=default.target
'''


def main():
    path = Path.home() / '.config/systemd/user/frame-testbench-guard.service'
    if path.exists() and not path.read_text().startswith(MARKER):
        raise ValueError(f'refusing unowned service: {path}')
    text = unit(ROOT)
    with tempfile.TemporaryDirectory(prefix='frame-testbench-unit-') as directory:
        candidate = Path(directory) / path.name
        candidate.write_text(text)
        subprocess.run(['systemd-analyze', '--user', 'verify', str(candidate)], check=True, timeout=20)
    atomic(path, text.encode())
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, timeout=20)
    subprocess.run(['systemctl', '--user', 'enable', 'frame-testbench-guard.service'], check=True, timeout=20)
    subprocess.run(['systemctl', '--user', 'restart', 'frame-testbench-guard.service'], check=True, timeout=45)
    print(f'Session guard installed and enabled: {path}')


if __name__ == '__main__':
    main()
