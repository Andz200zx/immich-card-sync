import base64
import contextlib
import email
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('ingest',Path(__file__).resolve().parents[1]/'src/ingest.py')
ingest=importlib.util.module_from_spec(spec);spec.loader.exec_module(ingest)

class ServerState:
    def __init__(self):
        self.assets={};self.stacks={};self.tags={};self.tagged={};self.uploads=0;self.fail_response_once=False;self.metadata_ready=True;self.requests=[]
        self.lock=threading.RLock()
    def asset(self, aid):
        a=dict(self.assets[aid]);a['exifInfo']={'dateTimeOriginal':'2026-09-07T10:00:00Z','make':'SONY','model':'ILCE-7SM2'} if self.metadata_ready else {}
        a['stack']=next(({'id':sid,'assetCount':len(s['assets']),'primaryAssetId':s['primaryAssetId']} for sid,s in self.stacks.items() if aid in s['assets']),None)
        return a

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def respond(self,data,code=200):
        body=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        st=self.server.state
        with st.lock:
            st.requests.append(('GET',self.path))
            if self.path.startswith('/api/assets/'):
                self.respond(st.asset(self.path.rsplit('/',1)[1]))
            elif self.path.startswith('/api/stacks/'):
                sid=self.path.rsplit('/',1)[1];s=st.stacks[sid];self.respond({'id':sid,'primaryAssetId':s['primaryAssetId'],'assets':[st.asset(a) for a in s['assets']]})
            else:self.respond({},404)
    def do_PUT(self):
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with self.server.state.lock:
            st=self.server.state
            if self.path=='/api/tags':
                for name in data['tags']:
                    if name not in st.tags:st.tags[name]=str(len(st.tags)+1)
                self.respond([{'id':st.tags[n],'value':n} for n in data['tags']]);return
            if self.path.startswith('/api/tags/'):
                tag_id=self.path.split('/')[3];st.tagged.setdefault(tag_id,set()).update(data['ids']);self.respond([{'id':a,'success':True} for a in data['ids']]);return
            sid=self.path.rsplit('/',1)[1];st.stacks[sid]['primaryAssetId']=data['primaryAssetId'];self.respond({})
    def do_POST(self):
        data=self.rfile.read(int(self.headers['Content-Length']))
        st=self.server.state
        with st.lock:
            st.requests.append(('POST',self.path))
            if self.path=='/api/assets/bulk-upload-check':
                results=[]
                for row in json.loads(data)['assets']:
                    aid=next((aid for aid,a in st.assets.items() if base64.b64decode(a['checksum']).hex()==row['checksum']),None)
                    r={'id':row['id'],'action':'reject' if aid else 'accept'}
                    if aid:r.update(assetId=aid,reason='duplicate',isTrashed=st.assets[aid].get('isTrashed',False))
                    results.append(r)
                self.respond({'results':results})
            elif self.path=='/api/assets':
                msg=email.message_from_bytes(('Content-Type: '+self.headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+data)
                fields={part.get_param('name',header='content-disposition'):part.get_payload(decode=True) for part in msg.walk() if part.get_content_maintype()!='multipart'}
                digest=hashlib.sha1(fields['assetData']).digest();aid=next((aid for aid,a in st.assets.items() if base64.b64decode(a['checksum'])==digest),None)
                status='duplicate' if aid else 'created'
                if not aid:
                    aid=str(len(st.assets)+1);st.assets[aid]={'id':aid,'checksum':base64.b64encode(digest).decode(),'originalFileName':fields['filename'].decode(),'isTrashed':False,'isOffline':False};st.uploads+=1
                if st.fail_response_once:
                    st.fail_response_once=False;self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                self.respond({'id':aid,'status':status},201 if status=='created' else 200)
            elif self.path=='/api/stacks':
                ids=json.loads(data)['assetIds'];sid=str(len(st.stacks)+1);st.stacks[sid]={'assets':ids,'primaryAssetId':ids[0]};self.respond({'id':sid,'primaryAssetId':ids[0]},201)
            else:self.respond({},404)

class Tests(unittest.TestCase):
    def setUp(self):
        ingest.STOP.clear();self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'card';self.root.mkdir();self.state=Path(self.temp.name)/'state';self.state.mkdir()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.state=ServerState();self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.api=ingest.API('http://127.0.0.1:'+str(self.server.server_port),'test-key');self.connect=patch.object(ingest,'connect',return_value=self.api);self.connect.start()
        self.output=patch.object(ingest,'emit');self.output.start()
    def tearDown(self):
        self.connect.stop();self.output.stop();self.server.shutdown();self.server.server_close();self.temp.cleanup();ingest.STOP.clear()
    def write(self,name,data):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p
    def pair(self,folder='DCIM/100MSDCF'):
        self.write(folder+'/DSC00001.JPG',('jpeg '+folder).encode());self.write(folder+'/DSC00001.ARW',('raw '+folder).encode())
    def sync(self,**kwargs):return ingest.sync({},self.root,'card-uuid',self.state,**kwargs)
    def test_repeat_sync_and_jpeg_cover(self):
        self.pair();before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        first=self.sync();second=self.sync()
        self.assertEqual(first['uploaded'],2);self.assertEqual(second['uploaded'],0);self.assertEqual(second['skipped'],2);self.assertEqual(second['cachedHashes'],2)
        self.assertEqual(len(self.server.state.stacks),1);self.assertEqual(self.server.state.uploads,2)
        stack=next(iter(self.server.state.stacks.values()));self.assertTrue(self.server.state.assets[stack['primaryAssetId']]['originalFileName'].endswith('.JPG'))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_two_cameras_same_names_stay_separate(self):
        self.pair('DCIM/100MSDCF');self.pair('DCIM/101MSDCF');self.sync();self.assertEqual(len(self.server.state.stacks),2)
        self.assertTrue(all(len(x['assets'])==2 for x in self.server.state.stacks.values()))
    def test_disconnect_after_server_saved_upload_is_retryable(self):
        self.write('DCIM/DJI_0001.MP4',b'camera movie');self.server.state.fail_response_once=True;self.sync();self.assertEqual(self.server.state.uploads,1)
    def test_content_change_invalidates_hash_cache(self):
        p=self.write('DCIM/DSC0001.JPG',b'original1');self.sync();p.write_bytes(b'original2');result=self.sync();self.assertEqual(result['cachedHashes'],0);self.assertEqual(self.server.state.uploads,2)
    def test_empty_card_and_sidecars_not_imported(self):
        self.write('PRIVATE/M4ROOT/CLIP/C0001.XML',b'<xml/>');self.write('DCIM/DJI_0001.LRF',b'preview');self.assertEqual(self.sync()['files'],0)
    def test_symlinks_and_hidden_files_are_ignored(self):
        self.write('DCIM/.DSC0001.JPG',b'hidden');external=Path(self.temp.name)/'external.JPG';external.write_bytes(b'outside');(self.root/'link.JPG').symlink_to(external);self.assertEqual(ingest.scan(self.root),[])
    def test_dry_run_makes_no_media_or_stack_mutations(self):
        self.pair();r=self.sync(dry_run=True);self.assertEqual(r['wouldUpload'],2);self.assertEqual(self.server.state.uploads,0);self.assertEqual(self.server.state.stacks,{})
    def test_ambiguous_pair_is_not_stacked(self):
        self.pair();self.write('DCIM/100MSDCF/DSC00001.DNG',b'extra raw');r=self.sync();self.assertEqual(len(r['warnings']),1);self.assertEqual(self.server.state.stacks,{})
    def test_conflicting_existing_stack_is_preserved(self):
        self.pair();self.sync();st=self.server.state;stack=st.stacks['1'];stack['assets'].append('unrelated');r=self.sync();self.assertEqual(len(stack['assets']),3);self.assertEqual(len(r['warnings']),1)
    def test_existing_two_asset_stack_gets_jpeg_cover(self):
        self.pair();self.sync();st=self.server.state;s=st.stacks['1'];s['primaryAssetId']=next(a for a in s['assets'] if st.assets[a]['originalFileName'].endswith('.ARW'));self.sync();self.assertTrue(st.assets[s['primaryAssetId']]['originalFileName'].endswith('.JPG'))
    def test_pending_metadata_finishes_without_the_card(self):
        self.pair();self.server.state.metadata_ready=False
        with patch.object(ingest.STOP,'wait',return_value=False):r=self.sync()
        self.assertEqual(r['pendingPairs'],1);self.server.state.metadata_ready=True
        for p in self.root.rglob('*'):
            if p.is_file():p.unlink()
        paired,pending=ingest.finish_stacks(self.api,self.state,[]);self.assertEqual((paired,pending),(1,0))
    def test_trashed_asset_is_reported(self):
        self.write('DCIM/A.JPG',b'jpeg');self.sync();self.server.state.assets['1']['isTrashed']=True
        with self.assertRaisesRegex(ingest.IngestError,'bin'):self.sync()
    def test_cancel_stops_before_mutations(self):
        self.pair();ingest.STOP.set()
        with self.assertRaises(ingest.Cancelled):self.sync()
        self.assertEqual(self.server.state.uploads,0)
    def test_filename_is_preserved_in_multipart(self):
        name='DCIM/a, b; "c".JPG';self.write(name,b'jpeg');self.sync();self.assertEqual(self.server.state.assets['1']['originalFileName'],Path(name).name)
    def test_permanent_upload_failure_stops_remaining_work(self):
        for n in range(40):self.write(f'DCIM/{n:03}.JPG',f'jpeg {n}'.encode())
        calls=[];lock=threading.Lock()
        def fail(record,progress=None):
            with lock:
                calls.append(record['relative']);first=len(calls)==1
            if first:raise ingest.ApiError(403,'Forbidden')
            ingest.STOP.wait(3);ingest.check_stop()
        with patch.object(self.api,'upload',side_effect=fail):
            with self.assertRaises(ingest.ApiError):self.sync()
        self.assertLess(len(calls),10)
        self.assertEqual(self.server.state.uploads,0)
    def test_upload_date_tag_only_applies_to_new_files(self):
        self.pair()
        with patch.object(ingest,'today_tag',return_value='SD imports/2026-09-07'):r=self.sync()
        self.assertEqual(r['tagged'],2)
        self.write('DCIM/100MSDCF/DSC00002.JPG',b'new day jpeg')
        with patch.object(ingest,'today_tag',return_value='SD imports/2026-09-08'):r=self.sync()
        self.assertEqual(r['tagged'],1)
        st=self.server.state
        self.assertEqual(len(st.tagged[st.tags['SD imports/2026-09-07']]),2)
        self.assertEqual(len(st.tagged[st.tags['SD imports/2026-09-08']]),1)
    def test_lost_tag_response_is_recovered_without_reupload(self):
        self.pair();original=self.api.request;fail=[True]
        def request(method,path,*args,**kwargs):
            result=original(method,path,*args,**kwargs)
            if path.startswith('/tags/') and fail[0]:fail[0]=False;raise ingest.IngestError('lost tag response')
            return result
        with patch.object(self.api,'request',side_effect=request),patch.object(ingest,'today_tag',return_value='SD imports/2026-09-07'):
            with self.assertRaises(ingest.IngestError):self.sync()
        with patch.object(ingest,'today_tag',return_value='SD imports/2026-09-08'):self.sync()
        st=self.server.state
        self.assertEqual(st.uploads,2);self.assertNotIn('SD imports/2026-09-08',st.tags)
        self.assertEqual(len(st.tagged[st.tags['SD imports/2026-09-07']]),2)
    def test_pairs_with_different_capture_times_stay_separate(self):
        self.pair();original=self.server.state.asset
        def asset(aid):
            a=original(aid)
            if a['originalFileName'].endswith('.ARW'):a['exifInfo']['dateTimeOriginal']='2026-09-07T11:00:00Z'
            return a
        self.server.state.asset=asset;r=self.sync();self.assertEqual(len(r['warnings']),1);self.assertEqual(self.server.state.stacks,{})

if __name__=='__main__':unittest.main(verbosity=2)
