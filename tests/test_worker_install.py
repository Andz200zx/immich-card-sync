import fcntl
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

scripts=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(scripts))
spec=importlib.util.spec_from_file_location('install_worker',scripts/'install_worker.py')
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class WorkerInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.home=Path(self.temp.name)
        self.support=self.home/'Library/Application Support/Immich RAW Worker'
        (self.support/'state').mkdir(parents=True)
        (self.support/'bin').mkdir()
        (self.support/'bin/previous.txt').write_text('existing worker')
        self.config={'userId':'test-account','servers':['http://example.invalid'],'credentialsFile':'unchanged'}
        (self.support/'config.json').write_text(json.dumps(self.config))
        self.home_patch=patch.object(installer.Path,'home',return_value=self.home)
        self.home_patch.start()
        self.platform_patch=patch.object(installer.sys,'platform','darwin')
        self.platform_patch.start()
        self.loaded=False
        self.fail_bootstrap=False
        self.calls=[]
        self.run_patch=patch.object(installer.subprocess,'run',side_effect=self.fake_run)
        self.run_patch.start()

    def tearDown(self):
        self.run_patch.stop()
        self.platform_patch.stop()
        self.home_patch.stop()
        self.temp.cleanup()

    def fake_run(self,args,**kwargs):
        self.calls.append(args)
        if args[:2]==['launchctl','print']:
            return subprocess.CompletedProcess(args,0 if self.loaded else 1)
        if args[:2]==['launchctl','bootstrap'] and self.fail_bootstrap:
            self.fail_bootstrap=False
            raise subprocess.CalledProcessError(5,args)
        return subprocess.CompletedProcess(args,0)

    def test_manual_install_does_not_enable_login_startup(self):
        installer.install(False)
        self.assertTrue((self.support/'bin/raw_worker.py').exists())
        self.assertFalse((self.home/'Library/LaunchAgents'/f'{installer.LABEL}.plist').exists())
        self.assertEqual(json.loads((self.support/'config.json').read_text()),self.config)

    def test_failed_activation_restores_previous_worker_and_login_entry(self):
        self.loaded=True
        self.fail_bootstrap=True
        agent=self.home/'Library/LaunchAgents'/f'{installer.LABEL}.plist'
        agent.parent.mkdir(parents=True)
        agent.write_bytes(b'previous login entry')
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install(True)
        self.assertEqual((self.support/'bin/previous.txt').read_text(),'existing worker')
        self.assertEqual(agent.read_bytes(),b'previous login entry')
        self.assertEqual(sum(a[:2]==['launchctl','bootstrap'] for a in self.calls),2)

    def test_running_conversion_prevents_replacement(self):
        with (self.support/'state/worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                installer.install(True)
        self.assertEqual((self.support/'bin/previous.txt').read_text(),'existing worker')
        self.assertFalse(self.calls)


if __name__=='__main__':
    unittest.main()
