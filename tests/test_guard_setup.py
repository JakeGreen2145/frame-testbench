import importlib.util
from pathlib import Path
import tempfile
import unittest
import shutil
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/setup-session-guard.py'
spec = importlib.util.spec_from_file_location('guard_setup', SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class GuardSetupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd parser not installed')
    def test_generated_unit_passes_real_systemd_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'frame-testbench-guard.service'
            path.write_text(setup.unit('/tmp/bench space%"quote'))
            result = setup.subprocess.run(['systemd-analyze', '--user', 'verify', str(path)],
                                          capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unit_uses_checkout_and_escapes_systemd_path(self):
        text = setup.unit('/tmp/bench space%"quote')
        self.assertIn('WorkingDirectory=/tmp/bench space%%"quote\n', text)
        self.assertIn('Type=notify', text)
        self.assertIn('--cleanup', text)
        with self.assertRaises(ValueError):
            setup.unit('/tmp/bench\nnewline')

    def test_setup_enables_only_guard_and_refuses_unowned_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(setup.Path, 'home', return_value=Path(directory)), \
                    patch.object(setup.subprocess, 'run') as run:
                setup.main()
                self.assertEqual(run.call_args_list[0].args[0][:3], ['systemd-analyze', '--user', 'verify'])
                self.assertEqual([call.args[0] for call in run.call_args_list[1:]], [
                    ['systemctl', '--user', 'daemon-reload'],
                    ['systemctl', '--user', 'enable', 'frame-testbench-guard.service'],
                    ['systemctl', '--user', 'restart', 'frame-testbench-guard.service']])
                path = Path(directory) / '.config/systemd/user/frame-testbench-guard.service'
                self.assertTrue(path.read_text().startswith(setup.MARKER))
                path.write_text('unowned service')
                run.reset_mock()
                with self.assertRaisesRegex(ValueError, 'unowned'):
                    setup.main()
                run.assert_not_called()
