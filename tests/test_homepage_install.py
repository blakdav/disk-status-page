import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'homepage_install', Path(__file__).parents[1] / 'examples/homepage/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class HomepageInstallerTests(unittest.TestCase):
    def test_existing_customizations_are_preserved_and_updates_are_idempotent(self):
        original = '/* existing settings */\nbody { margin: 0; }\n'
        result = installer.merge(original, '/* app styles */')
        self.assertIn(original.rstrip(), result)
        self.assertEqual(installer.merge(result, '/* app styles */'), result)
        changed = installer.merge(result, '/* updated app styles */')
        self.assertIn(original.rstrip(), changed)
        self.assertNotIn('/* app styles */', changed)
        self.assertEqual(changed.count('/* BEGIN DISK STATUS PAGE */'), 1)

    def test_broken_markers_are_refused(self):
        with self.assertRaises(ValueError):
            installer.merge('/* BEGIN DISK STATUS PAGE */', 'new')

    def test_failed_download_leaves_both_existing_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['custom.js', 'custom.css']:
                (root / name).write_text('existing')
            def fetch(name):
                if name == 'custom.css':
                    raise OSError('Network unavailable')
                return 'new code'
            with self.assertRaises(OSError):
                installer.install(root, fetch)
            for name in ['custom.js', 'custom.css']:
                self.assertEqual((root / name).read_text(), 'existing')
            self.assertEqual(len(list(root.iterdir())), 2)

    def test_install_creates_backups_and_preserves_other_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'custom.js').write_text('// Existing code\n')
            installer.install(root, lambda name: '// addon' if name.endswith('.js') else '/* addon */')
            backups = list(root.glob('custom.js.before-disk-status-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), '// Existing code\n')
            self.assertIn('// Existing code', (root / 'custom.js').read_text())
            self.assertTrue((root / 'custom.css').exists())
