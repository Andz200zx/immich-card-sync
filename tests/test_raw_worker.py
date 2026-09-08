import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import raw_worker as worker


def asset(name='DSC00001.NEF', model='NIKON D750', taken='2020-03-04T10:20:30Z', data=b'raw'):
    return {'id': str(uuid.uuid4()), 'ownerId': 'owner', 'checksum': base64.b64encode(hashlib.sha1(data).digest()).decode(),
            'originalFileName': name, 'type': 'IMAGE', 'visibility': 'timeline', 'stack': None,
            'fileCreatedAt': taken, 'fileModifiedAt': taken, 'tags': [{'id': 'import-date'}],
            'exifInfo': {'make': 'NIKON', 'model': model, 'dateTimeOriginal': taken}}


class API:
    def __init__(self, raw):
        self.assets = {raw['id']: copy.deepcopy(raw)}
        self.stacks = {}
        self.tagged = {}
        self.uploads = 0
        self.lose_response = False

    def check(self, records):
        out = {}
        for record in records:
            found = next((a for a in self.assets.values() if worker.checksum_hex(a['checksum']) == record['sha1']), None)
            out[record['relative']] = {'action': 'reject', 'assetId': found['id']} if found else {'action': 'accept'}
        return out

    def upload(self, record):
        item = asset(Path(record['path']).name, data=Path(record['path']).read_bytes())
        item['visibility'] = record['visibility']
        self.assets[item['id']] = item
        self.uploads += 1
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError('Upload response lost')
        return {'id': item['id'], 'status': 'created'}

    def request(self, method, path, data=None, **kwargs):
        if method == 'GET' and path.startswith('/assets/'):
            return copy.deepcopy(self.assets[path.split('/')[-1]])
        if method == 'POST' and path == '/search/metadata':
            return {'assets': {'items': [copy.deepcopy(a) for a in self.assets.values()
                                         if a['visibility'] == data.get('visibility', 'timeline')], 'nextPage': None}}
        if method == 'POST' and path == '/stacks':
            sid = str(uuid.uuid4())
            self.stacks[sid] = {'id': sid, 'primaryAssetId': data['assetIds'][0], 'assets': data['assetIds']}
            for aid in data['assetIds']:
                self.assets[aid]['stack'] = {'id': sid, 'assetCount': len(data['assetIds'])}
            return {'id': sid}
        if method == 'GET' and path.startswith('/stacks/'):
            result = dict(self.stacks[path.split('/')[-1]])
            result['assets'] = [self.assets[aid] for aid in result['assets']]
            return result
        if method == 'PUT' and path.startswith('/stacks/'):
            self.stacks[path.split('/')[-1]].update(data)
            return {}
        if method == 'PUT' and path == '/tags':
            return [{'id': 'render-date', 'value': data['tags'][0]}]
        if method == 'PUT' and path.startswith('/tags/'):
            self.tagged.setdefault(path.split('/')[2], set()).update(data['ids'])
            return [{'id': aid, 'success': True} for aid in data['ids']]
        raise AssertionError((method, path))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        worker.ingest.STOP.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.config = {'userId': 'owner', 'servers': ['http://example.invalid']}
        self.store = worker.Store(self.state, self.config)
        self.raw = asset()
        self.api = API(self.raw)
        self.store.add(self.raw)
        self.emit = patch.object(worker.ingest, 'emit')
        self.emit.start()
        self.preparer = patch.object(worker, 'prepare', side_effect=self.prepare)
        self.preparer.start()
        self.prepared = 0

    def tearDown(self):
        self.emit.stop()
        self.preparer.stop()
        self.store.db.close()
        self.temp.cleanup()

    def prepare(self, config, api, store, raw):
        self.prepared += 1
        file = worker.folder_for(store, raw['id']) / 'DSC00001.JPG'
        file.write_bytes(b'generated jpeg')
        return {'filename': file.name, 'sha1': worker.sha1(file), 'size': file.stat().st_size,
                'width': 6000, 'height': 4000, 'recipe': 'recipe', 'tag': 'RAW renders/2026-09-08'}

    def process(self):
        worker.process(self.config, self.api, self.store, self.raw['id'])

    def test_lost_upload_response_resumes_without_duplicate_and_preserves_tags(self):
        self.api.lose_response = True
        with self.assertRaises(TimeoutError):
            self.process()
        self.assertEqual(self.store.job(self.raw['id'])['status'], 'prepared')
        self.process()
        self.assertEqual((self.api.uploads, self.prepared), (1, 1))
        job = self.store.job(self.raw['id'])
        self.assertEqual(job['status'], 'done')
        jpeg = job['receipt']['jpegId']
        self.assertEqual(next(iter(self.api.stacks.values()))['primaryAssetId'], jpeg)
        self.assertEqual(self.api.tagged['import-date'], {jpeg})
        self.assertEqual(self.api.tagged['render-date'], {jpeg})
        self.assertFalse((self.state/'scratch'/self.raw['id']).exists())
        self.assertEqual(self.api.assets[self.raw['id']]['checksum'], self.raw['checksum'])

    def test_original_changed_after_download_is_never_uploaded(self):
        original_prepare = self.prepare
        def replace(*args):
            receipt = original_prepare(*args)
            self.api.assets[self.raw['id']]['checksum'] = asset(data=b'changed')['checksum']
            return receipt
        with patch.object(worker, 'prepare', side_effect=replace), self.assertRaises(worker.Review):
            self.process()
        self.assertEqual(self.api.uploads, 0)

    def test_existing_jpeg_in_archive_prevents_conversion(self):
        jpeg = asset('DSC00001.JPG', data=b'camera jpeg')
        jpeg['visibility'] = 'archive'
        self.api.assets[jpeg['id']] = jpeg
        self.process()
        self.assertEqual(self.store.job(self.raw['id'])['status'], 'skipped')
        self.assertEqual((self.prepared, self.api.uploads), (0, 0))

    def test_renamed_same_time_jpeg_is_reviewed(self):
        jpeg = asset('holiday-edit.JPG', data=b'edited')
        self.api.assets[jpeg['id']] = jpeg
        with self.assertRaises(worker.Review):
            self.process()
        self.assertEqual(self.prepared, 0)

    def test_different_camera_filename_collision_is_not_paired(self):
        jpeg = asset('DSC00001.JPG', model='NIKON D800', data=b'other camera')
        self.api.assets[jpeg['id']] = jpeg
        self.assertIsNone(worker.jpeg_equivalent(self.api, self.raw))

    def test_missing_capture_metadata_never_guesses(self):
        self.api.assets[self.raw['id']]['exifInfo'].pop('dateTimeOriginal')
        with self.assertRaises(worker.Review):
            self.process()
        self.assertEqual(self.prepared, 0)

    def test_new_raw_waits_for_metadata_and_then_processes_automatically(self):
        raw=self.api.assets[self.raw['id']]
        raw['hasMetadata']=False
        previous=raw['exifInfo']
        raw['exifInfo']={}
        with self.assertRaises(worker.ingest.IngestError) as raised:
            self.process()
        self.assertNotIsInstance(raised.exception,worker.Review)
        self.assertEqual(self.prepared,0)
        raw['hasMetadata']=True
        raw['exifInfo']=previous
        self.process()
        self.assertEqual(self.store.job(self.raw['id'])['status'],'done')
        self.assertEqual(self.api.uploads,1)

    def test_existing_raw_stack_is_preserved(self):
        second = asset('DSC00002.NEF', data=b'second raw')
        self.api.assets[second['id']] = second
        self.api.request('POST', '/stacks', {'assetIds': [self.raw['id'], second['id']]})
        before = copy.deepcopy(self.api.stacks)
        with self.assertRaises(worker.Review):
            self.process()
        self.assertEqual(self.api.stacks, before)
        self.assertEqual(self.api.uploads, 0)

    def test_jpeg_arriving_after_interrupted_upload_prevents_new_stack(self):
        self.api.lose_response = True
        with self.assertRaises(TimeoutError):
            self.process()
        jpeg = asset('DSC00001.JPG', data=b'camera jpeg arrived later')
        self.api.assets[jpeg['id']] = jpeg
        with self.assertRaises(worker.Review):
            self.process()
        self.assertEqual(self.api.uploads, 1)
        self.assertFalse(self.api.stacks)

    def test_archive_visibility_is_preserved(self):
        self.api.assets[self.raw['id']]['visibility'] = 'archive'
        self.process()
        jpeg_id = self.store.job(self.raw['id'])['receipt']['jpegId']
        self.assertEqual(self.api.assets[jpeg_id]['visibility'], 'archive')

    def test_state_cannot_be_reused_for_another_account(self):
        with self.assertRaises(worker.Review):
            worker.Store(self.state, {'userId': 'someone-else', 'servers': self.config['servers']})

    def test_scan_commits_cursor_and_repeated_pages_do_not_duplicate_jobs(self):
        worker.scan(self.api, self.store, 1)
        first = self.store.summary()
        self.store.put('scanOffset', 0)
        self.store.put('scan:.arw:timeline', {'page': 1, 'before': worker.stamp()})
        worker.scan(self.api, self.store, 1)
        self.assertEqual(self.store.summary(), first)
        self.assertEqual(self.store.get('scanOffset'), 1)

    def test_jpeg_dimensions_use_sof_not_embedded_preview(self):
        # A fake APP1 segment has unrelated pixel-like bytes; only the real SOF counts.
        file = self.state/'dimensions.jpg'
        file.write_bytes(b'\xff\xd8\xff\xe1\x00\x08abcdef\xff\xc0\x00\x11\x08\x0f\xa0\x17\x70')
        self.assertEqual(worker.jpeg_size(file), (6000, 4000))
        file.write_bytes(b'\xff\xd8\xff\xe1\x00')
        with self.assertRaises(worker.Review):
            worker.jpeg_size(file)

    def test_nikon_makernote_timezone_and_subseconds_survive_standard_jpeg_export(self):
        raw=asset(taken='2019-07-11T23:28:56.300+00:00')
        raw['localDateTime']='2019-07-12T00:28:56.300Z'
        tags=worker.capture_tags(raw)
        self.assertIn('-DateTimeOriginal=2019:07:12 00:28:56',tags)
        self.assertIn('-OffsetTimeOriginal=+01:00',tags)
        self.assertIn('-SubSecTimeOriginal=3',tags)

    def test_half_hour_negative_timezone_preserves_the_capture_instant(self):
        raw=asset(taken='2020-03-04T02:20:30.125Z')
        raw['localDateTime']='2020-03-03T22:50:30.125Z'
        tags=worker.capture_tags(raw)
        self.assertIn('-DateTimeOriginal=2020:03:03 22:50:30',tags)
        self.assertIn('-OffsetTimeOriginal=-03:30',tags)

    def test_existing_jpeg_with_lost_timezone_does_not_create_another_companion(self):
        raw=asset(taken='2019-07-11T23:28:56.300Z')
        raw['localDateTime']='2019-07-12T00:28:56.300Z'
        jpeg=asset('DSC00001.JPG',taken='2019-07-12T00:28:56.300Z',data=b'legacy jpeg')
        jpeg['localDateTime']=raw['localDateTime']
        api=API(raw)
        api.assets[jpeg['id']]=jpeg
        with self.assertRaises(worker.Review):
            worker.jpeg_equivalent(api,raw)


if __name__ == '__main__':
    unittest.main()
