"""Offline installer fixtures are synthetic tar files, not upstream binaries."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def archive(binary=b'test executable', symlink=False):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w') as t:
        m = tarfile.TarInfo('bin/framecorder')
        if symlink:
            m.type = tarfile.SYMTYPE
            m.linkname = '/outside'
            t.addfile(m)
        else:
            m.size = len(binary)
            t.addfile(m, io.BytesIO(binary))
        # This must never be extracted or executed.
        m = tarfile.TarInfo('../../unexpected')
        m.size = 3
        t.addfile(m, io.BytesIO(b'bad'))
    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode='w:gz') as t:
        m = tarfile.TarInfo('payload.tar')
        m.size = len(payload.getvalue())
        t.addfile(m, io.BytesIO(payload.getvalue()))
    return outer.getvalue()

class SetupTests(unittest.TestCase):
    def module(self):
        source = ROOT / 'scripts/setup-framecorder.py'
        self.assertTrue(source.exists(), 'pinned recorder setup script is missing')
        spec = importlib.util.spec_from_file_location('framecorder_setup', source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_installs_only_verified_recorder_without_executing_archive_content(self):
        m = self.module(); binary = b'test executable'; data = archive(binary)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(m, 'ARCHIVE_SHA256', hashlib.sha256(data).hexdigest()), \
             patch.object(m, 'BINARY_SHA256', hashlib.sha256(binary).hexdigest()):
            dest = Path(tmp) / 'tools' / 'version'
            result = m.install_archive(data, dest)
            self.assertEqual(result, dest / 'framecorder')
            self.assertEqual(result.read_bytes(), binary)
            self.assertEqual(result.stat().st_mode & 0o7777, 0o755)
            self.assertEqual(list(dest.iterdir()), [result])
            self.assertFalse((Path(tmp) / 'unexpected').exists())
            self.assertEqual(m.install_archive(data, dest), result)

    def test_hash_mismatch_and_link_members_fail_without_install(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'tools'
            with self.assertRaisesRegex(ValueError, 'checksum'):
                m.install_archive(b'bad archive', dest)
            self.assertFalse(dest.exists())
            data = archive(symlink=True)
            with patch.object(m, 'ARCHIVE_SHA256', hashlib.sha256(data).hexdigest()):
                with self.assertRaises(ValueError):
                    m.install_archive(data, dest)
            self.assertFalse(dest.exists())

    def test_refuses_wrong_executable_hash_and_existing_file(self):
        m = self.module(); data = archive()
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'ARCHIVE_SHA256', hashlib.sha256(data).hexdigest()):
            dest = Path(tmp) / 'tools'
            with self.assertRaisesRegex(ValueError, 'checksum'):
                m.install_archive(data, dest)
            self.assertFalse(dest.exists())
            dest.mkdir(); target = dest / 'framecorder'; target.write_bytes(b'keep me')
            with patch.object(m, 'BINARY_SHA256', hashlib.sha256(b'test executable').hexdigest()):
                with self.assertRaises(FileExistsError):
                    m.install_archive(data, dest)
            self.assertEqual(target.read_bytes(), b'keep me')

    def test_refuses_non_arm_before_download(self):
        m = self.module()
        with patch.object(m.platform, 'machine', return_value='x86_64'), patch.object(m, 'download') as download:
            with self.assertRaisesRegex(RuntimeError, 'aarch64'):
                m.setup()
            download.assert_not_called()

if __name__ == '__main__':
    unittest.main()
