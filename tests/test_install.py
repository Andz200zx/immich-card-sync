import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('installer',Path(__file__).resolve().parents[1]/'scripts/install.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class InstallerTests(unittest.TestCase):
    def test_normalizes_library_urls(self):
        self.assertEqual(installer.normalize_url('https://photos.example.test/photos/'),'https://photos.example.test')
        self.assertEqual(installer.normalize_url('http://nas.example.test:2283/api'),'http://nas.example.test:2283')
    def test_rejects_credentials_or_non_http_urls(self):
        for url in ['file:///tmp','https://user:password@example.test','https://example.test?apiKey=secret','https://example.test/#key','example.test']:
            with self.subTest(url=url),self.assertRaises(ValueError):installer.normalize_url(url)
    def test_wizard_keeps_key_out_of_config_and_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest=Path(tmp)/'config.json'
            response=io.BytesIO(json.dumps({'id':'test-owner'}).encode())
            with patch('builtins.input',side_effect=['https://photos.example.test','']),patch.object(installer.getpass,'getpass',return_value='test-secret'),patch.object(installer.urllib.request,'build_opener') as opener,patch.object(installer.subprocess,'run') as run:
                opener.return_value.open.return_value=response
                installer.configure(Path('/example/App'),dest)
                self.assertNotIn('test-secret',dest.read_text())
                self.assertNotIn('test-secret',str(run.call_args.args))
                self.assertEqual(run.call_args.kwargs['input'],b'test-secret')
                self.assertEqual(json.loads(dest.read_text())['userId'],'test-owner')

class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root/'home'
        self.package = self.root/'package'
        self.app = self.home/'Applications/Immich Card Sync.app'
        self.support = self.home/'Library/Application Support/Immich Card Sync'
        self.state = self.support/'state'
        self.config = self.support/'config.json'
        self.agent = self.home/'Library/LaunchAgents'/f'{installer.LABEL}.plist'
        self.legacy = self.agent.with_name(f'{installer.LEGACY_LABEL}.plist')
        for path in [self.app, self.package/'Immich Card Sync.app']:
            for relative in ['Contents/MacOS/Immich Card Sync', 'Contents/Resources/ingest.py', 'Contents/Info.plist']:
                file = path/relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('old' if path == self.app else 'new')
        self.state.mkdir(parents=True)
        self.agent.parent.mkdir(parents=True)
        self.current = {'servers': ['http://nas.example.test:2283', 'https://nas.example.ts.net'],
                        'userId': 'owner', 'keyAccount': 'old-account', 'pythonPath': '/old/python',
                        'uploadKeyService': installer.SERVICE, 'stackKeyService': installer.SERVICE}
        self.config.write_text(json.dumps(self.current))
        self.original_config = self.config.read_bytes()
        self.agent.write_bytes(plistlib.dumps({'Label': installer.LABEL, 'old': True}))
        self.original_agent = self.agent.read_bytes()
        self.legacy.write_bytes(plistlib.dumps({'Label': installer.LEGACY_LABEL, 'old': True}))
        self.original_legacy = self.legacy.read_bytes()
        (self.state/'pending-tags.json').write_text('[]')
        (self.state/'pending-stacks.json').write_text('[]')
        (self.state/'hashes.sqlite').write_text('original-cache')
        self.loaded = {installer.LABEL, installer.LEGACY_LABEL}
        self.calls = []
        self.fail_probe = False
        self.fail_bootstrap = False

    def command(self, argv, **kwargs):
        self.calls.append(argv)
        status = 0
        if '--probe' in argv:
            # Preflight leaves the existing app/config/login services available.
            self.assertEqual((self.app/'Contents/Resources/ingest.py').read_text(), 'old')
            self.assertEqual(self.config.read_bytes(), self.original_config)
            self.assertEqual(self.loaded, {installer.LABEL, installer.LEGACY_LABEL})
            self.assertNotEqual(Path(argv[argv.index('--state') + 1]), self.state)
            status = 1 if self.fail_probe else 0
        elif argv[0] == '/bin/launchctl':
            if argv[1] == 'print':
                status = 0 if argv[2].rsplit('/', 1)[1] in self.loaded else 3
            elif argv[1] == 'bootout':
                self.loaded.discard(argv[2].rsplit('/', 1)[1])
            elif argv[1] == 'bootstrap':
                path = Path(argv[3])
                label = plistlib.loads(path.read_bytes())['Label']
                if self.fail_bootstrap:
                    self.fail_bootstrap = False
                    status = 5
                else:
                    self.loaded.add(label)
        if status and kwargs.get('check'):
            raise subprocess.CalledProcessError(status, argv)
        return subprocess.CompletedProcess(argv, status, stdout=b'', stderr=b'')

    def assert_unchanged(self):
        self.assertEqual((self.app/'Contents/Resources/ingest.py').read_text(), 'old')
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertEqual(self.agent.read_bytes(), self.original_agent)
        self.assertEqual(self.legacy.read_bytes(), self.original_legacy)
        self.assertEqual((self.state/'hashes.sqlite').read_text(), 'original-cache')
        self.assertEqual(self.loaded, {installer.LABEL, installer.LEGACY_LABEL})

    def configured(self, new_config):
        def configure(executable, destination, key_account):
            value = dict(new_config)
            value['keyAccount'] = key_account
            destination.write_text(json.dumps(value))
        return configure

    def test_failed_probe_preserves_running_installation(self):
        self.fail_probe = True
        with patch.object(installer.subprocess, 'run', side_effect=self.command):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.package, self.home)
        self.assert_unchanged()
        self.assertFalse(any('bootout' in call for call in self.calls))

    def test_successful_upgrade_preserves_pending_work_and_migrates_legacy_agent(self):
        (self.state/'pending-tags.json').write_text('[{"ids":["asset-1"]}]')
        with patch.object(installer.subprocess, 'run', side_effect=self.command):
            installer.install(self.package, self.home)
        self.assertEqual((self.app/'Contents/Resources/ingest.py').read_text(), 'new')
        self.assertEqual(json.loads(self.config.read_text())['pythonPath'], installer.sys.executable)
        self.assertEqual(json.loads((self.state/'pending-tags.json').read_text()), [{'ids': ['asset-1']}])
        self.assertEqual(self.loaded, {installer.LABEL})
        self.assertFalse(self.legacy.exists())
        backups = list((self.support/'backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0]/self.legacy.name).read_bytes(), self.original_legacy)
        self.assertEqual((backups[0]/'config.json').read_bytes(), self.original_config)

    def test_failed_bootstrap_restores_app_config_and_both_agents(self):
        self.fail_bootstrap = True
        with patch.object(installer.subprocess, 'run', side_effect=self.command):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.package, self.home)
        self.assert_unchanged()

    def test_changed_library_with_pending_work_is_rejected(self):
        (self.state/'pending-stacks.json').write_text('[{"ids":["jpeg", "raw"]}]')
        changed = {**self.current, 'userId': 'another-owner'}
        with patch.object(installer.subprocess, 'run', side_effect=self.command), \
             patch.object(installer, 'configure', side_effect=self.configured(changed)):
            with self.assertRaisesRegex(ValueError, 'Finish pending'):
                installer.install(self.package, self.home, reconfigure=True)
        self.assert_unchanged()
        self.assertFalse(any('bootout' in call for call in self.calls))
        deleted = [call for call in self.calls if 'delete-generic-password' in call]
        self.assertEqual(len(deleted), 1)
        self.assertNotIn('old-account', deleted[0])

    def test_changed_library_archives_state_before_new_app_starts(self):
        changed = {**self.current, 'servers': ['https://another.example.test']}
        with patch.object(installer.subprocess, 'run', side_effect=self.command), \
             patch.object(installer, 'configure', side_effect=self.configured(changed)):
            installer.install(self.package, self.home, reconfigure=True)
        self.assertEqual({path.name for path in self.state.iterdir()}, {'sync.lock'})
        backup = next((self.support/'backups').iterdir())
        self.assertEqual((backup/'state/hashes.sqlite').read_text(), 'original-cache')
        self.assertFalse(any('delete-generic-password' in call for call in self.calls))

    def test_changed_library_bootstrap_failure_restores_state_and_old_credential(self):
        self.fail_bootstrap = True
        changed = {**self.current, 'userId': 'another-owner'}
        with patch.object(installer.subprocess, 'run', side_effect=self.command), \
             patch.object(installer, 'configure', side_effect=self.configured(changed)):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.package, self.home, reconfigure=True)
        self.assert_unchanged()
        self.assertEqual(json.loads((self.state/'pending-tags.json').read_text()), [])
        deleted = [call for call in self.calls if 'delete-generic-password' in call]
        self.assertEqual(len(deleted), 1)
        self.assertNotIn('old-account', deleted[0])

    def test_local_and_tailscale_addresses_identify_the_same_library(self):
        self.assertTrue(installer.same_library(self.current, {**self.current, 'servers': ['https://nas.example.ts.net/photos/']}))
        self.assertFalse(installer.same_library(self.current, {**self.current, 'userId': 'different'}))
        self.assertFalse(installer.same_library(self.current, {**self.current, 'servers': ['https://unrelated.example.test']}))

    def test_active_sync_blocks_upgrade_without_changes(self):
        with (self.state/'sync.lock').open('a') as lock:
            installer.fcntl.flock(lock, installer.fcntl.LOCK_EX | installer.fcntl.LOCK_NB)
            with patch.object(installer.subprocess, 'run', side_effect=self.command):
                with self.assertRaisesRegex(ValueError, 'A sync is running'):
                    installer.install(self.package, self.home)
        self.assert_unchanged()
        self.assertEqual(self.calls, [])
