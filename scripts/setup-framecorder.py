#!/usr/bin/env python3
"""Explicit, rootless install of the pinned optional Framecorder executable only.

No installer code from the archive is executed. No services, capabilities,
network listeners, system files or SteamVR configuration are changed.
"""
import argparse
import hashlib
import io
import os
from pathlib import Path
import platform
import tarfile
import tempfile
import urllib.request

VERSION = 'v0.1.1'
URL = f'https://github.com/coah80/framecorder/releases/download/{VERSION}/framecorder-arm64.tar.gz'
ARCHIVE_SHA256 = '426c2c9e5c4d0489d477122995cdec950e43ec82cfe9a9f40dfb0294779b1e63'
BINARY_SHA256 = '631e20aff5a91d124fd2780aaa8096898d9ecf8c385d068c8853e54cf1c1bb38'
MAX_BYTES = 16 * 1024 * 1024
DESTINATION = Path(__file__).resolve().parents[1] / 'tools' / f'framecorder-{VERSION}'


def download():
    with urllib.request.urlopen(URL, timeout=30) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('release archive exceeds size bound')
    return data


def member_bytes(archive, name):
    member = archive.getmember(name)
    if not member.isfile() or not 0 < member.size <= MAX_BYTES:
        raise ValueError(f'unsafe or oversized archive member: {name}')
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f'missing archive member: {name}')
    with stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) != member.size:
        raise ValueError(f'incomplete archive member: {name}')
    return data


def install_archive(data, destination):
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise ValueError('Framecorder release archive checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as outer:
        payload = member_bytes(outer, 'payload.tar')
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:') as inner:
        binary = member_bytes(inner, 'bin/framecorder')
    if hashlib.sha256(binary).hexdigest() != BINARY_SHA256:
        raise ValueError('Framecorder executable checksum mismatch')
    destination = Path(destination)
    target = destination / 'framecorder'
    if target.exists() or target.is_symlink():
        if (not target.is_symlink() and target.is_file()
                and target.stat().st_size == len(binary)
                and hashlib.sha256(target.read_bytes()).hexdigest() == BINARY_SHA256
                and target.stat().st_mode & 0o7777 == 0o755):
            return target
        raise FileExistsError(f'refusing to replace existing executable: {target}')
    destination.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.framecorder-', dir=destination)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(binary)
            stream.flush()
            os.fchmod(stream.fileno(), 0o755)
        # Publish atomically, refusing an intervening target instead of replacing it.
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    return target


def setup(archive_path=None):
    if platform.machine() not in ('aarch64', 'arm64'):
        raise RuntimeError('run this on the aarch64 Steam Frame capture host')
    if archive_path is None:
        data = download()
    else:
        with Path(archive_path).open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
    return install_archive(data, DESTINATION)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='use a local release archive; the same pinned checksum is required')
    args = parser.parse_args()
    try:
        target = setup(args.archive)
    except (OSError, RuntimeError, ValueError, tarfile.TarError, KeyError) as error:
        parser.exit(1, f'Framecorder setup failed: {error}\n')
    print(f'Installed {VERSION}: {target}\nSHA256: {BINARY_SHA256}\nUpstream MIT license: docs/framecorder-LICENSE.txt')


if __name__ == '__main__':
    main()
