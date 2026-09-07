import importlib.util
import io
import json
from pathlib import Path
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
