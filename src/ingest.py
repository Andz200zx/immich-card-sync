#!/usr/bin/env python3
"""Card-only, resumable Immich upload. Python standard library; no AI or daemon polling."""
from __future__ import annotations
import argparse
import base64
import concurrent.futures
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import mimetypes
import os
from pathlib import Path
import signal
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

RAW = {'.arw', '.dng', '.cr2', '.cr3', '.nef', '.nrw', '.raf', '.rw2', '.orf', '.pef', '.srw'}
JPEG = {'.jpg', '.jpeg'}
MEDIA = RAW | JPEG | {'.heic', '.heif', '.png', '.tif', '.tiff', '.mp4', '.mov', '.m4v', '.mts', '.m2ts', '.avi', '.mpg', '.mpeg', '.3gp', '.insv', '.insp'}
STOP = threading.Event()
OUTPUT_LOCK = threading.Lock()

class IngestError(Exception):
    pass

class Cancelled(IngestError):
    pass

class ApiError(IngestError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'Immich HTTP {code}: {message}')

def emit(event, **values):
    with OUTPUT_LOCK:
        print(json.dumps({'event': event, **values}), flush=True)

def check_stop():
    if STOP.is_set():
        raise Cancelled('Sync stopped. It is safe to run it again; completed uploads will be skipped.')

def signature(stat):
    return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, getattr(stat, 'st_birthtime', 0)]

def scan(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise IngestError('The card is no longer connected.')
    records = []
    def on_error(error):
        raise IngestError(f'Cannot read the card: {error.filename}. Check macOS removable-volume access.')
    for folder, dirs, names in os.walk(root, followlinks=False, onerror=on_error):
        check_stop()
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(folder) / d).is_symlink())
        for name in sorted(names):
            p = Path(folder) / name
            if name.startswith('.') or p.is_symlink() or p.suffix.lower() not in MEDIA:
                continue
            stat = p.stat()
            if not p.is_file():
                continue
            if stat.st_size == 0:
                raise IngestError(f'Empty camera file: {p.relative_to(root)}. Sync has not completed.')
            records.append({'path': str(p), 'relative': str(p.relative_to(root)), 'signature': signature(stat), 'size': stat.st_size})
    if not root.is_dir():
        raise IngestError('The card was removed while reading it.')
    return records

def pair_candidates(records):
    groups = {}
    for r in records:
        p = Path(r['relative'])
        if p.suffix.lower() in RAW | JPEG:
            groups.setdefault((str(p.parent), p.stem.casefold()), []).append(r)
    pairs, ambiguous = [], []
    for (_, stem), members in sorted(groups.items()):
        raws = [r for r in members if Path(r['relative']).suffix.lower() in RAW]
        jpegs = [r for r in members if Path(r['relative']).suffix.lower() in JPEG]
        if raws and jpegs:
            if len(raws) == len(jpegs) == 1:
                pairs.append((jpegs[0], raws[0]))
            else:
                ambiguous.append(f"Multiple RAW/JPEG candidates for {members[0]['relative']}; left separate.")
    return pairs, ambiguous

class HashCache:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS hashes (volume TEXT, path TEXT, signature TEXT, sha1 TEXT, PRIMARY KEY(volume,path))')
    def hash(self, volume, record, force=False):
        p = Path(record['path'])
        sig = json.dumps(record['signature'])
        row = self.db.execute('SELECT signature,sha1 FROM hashes WHERE volume=? AND path=?', (volume,record['relative'])).fetchone()
        if not force and row and row[0] == sig and signature(p.stat()) == record['signature']:
            return row[1], True
        digest = hashlib.sha1()
        with p.open('rb') as stream:
            if signature(os.fstat(stream.fileno())) != record['signature']:
                raise IngestError(f'File changed before hashing: {record["relative"]}')
            while data := stream.read(4 * 1024 * 1024):
                check_stop()
                digest.update(data)
            if signature(os.fstat(stream.fileno())) != record['signature']:
                raise IngestError(f'File changed during hashing: {record["relative"]}')
        checksum = digest.hexdigest()
        self.db.execute('INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)', (volume, record['relative'], sig, checksum))
        self.db.commit()
        return checksum, False
    def close(self):
        self.db.close()

def keychain(service, account):
    try:
        p = subprocess.run(['/usr/bin/security', 'find-generic-password', '-w', '-s', service, '-a', account], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise IngestError('Keychain access was not approved. Open Immich Card Sync and retry, then approve the macOS Keychain prompt.') from None
    if p.returncode or not p.stdout.strip():
        raise IngestError(f'Cannot read the {service} credential from Keychain. Unlock your login Keychain and retry.')
    return p.stdout.strip()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IngestError('Immich redirected an API request. Check the configured server addresses.')

class API:
    def __init__(self, base, key, stack_key=None):
        self.base = base.rstrip('/')
        self.key = key
        self.stack_key = stack_key or key
    def request(self, method, path, data=None, stack=False, timeout=30):
        check_stop()
        raw = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(self.base + '/api' + path, data=raw, method=method,
            headers={'x-api-key': self.stack_key if stack else self.key, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=timeout) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            # Never log request headers or credentials.
            raise ApiError(error.code, error.reason) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise IngestError('Connection to Immich was interrupted. Reconnect and run Sync again.') from error
    def check(self, records):
        results = {}
        for offset in range(0, len(records), 500):
            batch = records[offset:offset+500]
            reply = self.request('POST', '/assets/bulk-upload-check', {'assets': [{'id': r['relative'], 'checksum': r['sha1']} for r in batch]})
            expected = {r['relative'] for r in batch}
            rows = reply.get('results', [])
            if len(rows) != len(batch) or {r.get('id') for r in rows} != expected:
                raise IngestError('Immich returned an incomplete checksum check. No files will be marked complete.')
            for row in rows:
                if row.get('action') == 'reject':
                    if row.get('reason') != 'duplicate' or not row.get('assetId'):
                        raise IngestError(f'Immich rejected {row["id"]}: {row.get("reason", "unknown reason")}')
                    if row.get('isTrashed'):
                        raise IngestError(f'{row["id"]} is in the Immich bin. Restore it in Immich before syncing this card.')
                elif row.get('action') != 'accept':
                    raise IngestError('Immich returned an unknown checksum-check result.')
                results[row['id']] = row
        return results
    def upload(self, record, progress=None):
        """Stream the original, keeping credentials out of argv and bytes off the Mac's disk."""
        p = Path(record['path'])
        if signature(p.stat()) != record['signature']:
            raise IngestError(f'File changed before upload: {record["relative"]}')
        boundary = 'immich-card-' + uuid.uuid4().hex
        stamp = dt.datetime.fromtimestamp(record['signature'][1] / 1e9, dt.timezone.utc).isoformat().replace('+00:00','Z')
        fields = {'fileCreatedAt': record.get('fileCreatedAt', stamp),
                  'fileModifiedAt': record.get('fileModifiedAt', stamp), 'filename': p.name}
        # Derived companions retain their source's timeline/archive visibility and capture date.
        if 'visibility' in record:
            fields['visibility'] = record['visibility']
        prefix = b''
        for name, value in fields.items():
            prefix += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        # A neutral multipart filename avoids CR/LF and quote injection; filename field preserves the original.
        prefix += f'--{boundary}\r\nContent-Disposition: form-data; name="assetData"; filename="original{p.suffix.lower()}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        suffix = f'\r\n--{boundary}--\r\n'.encode()
        url = urllib.parse.urlsplit(self.base)
        cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        connection = cls(url.hostname, url.port, timeout=120)
        digest = hashlib.sha1()
        sent = 0
        last_progress = 0.0
        try:
            connection.putrequest('POST', url.path.rstrip('/') + '/api/assets')
            connection.putheader('x-api-key', self.key)
            connection.putheader('Content-Type', f'multipart/form-data; boundary={boundary}')
            connection.putheader('Content-Length', str(len(prefix) + record['size'] + len(suffix)))
            connection.endheaders()
            connection.send(prefix)
            with p.open('rb') as stream:
                if signature(os.fstat(stream.fileno())) != record['signature']:
                    raise IngestError(f'File changed before upload: {record["relative"]}')
                while data := stream.read(1024 * 1024):
                    check_stop()
                    connection.send(data)
                    digest.update(data)
                    sent += len(data)
                    if progress and time.monotonic() - last_progress > 1:
                        progress(sent)
                        last_progress = time.monotonic()
                if signature(os.fstat(stream.fileno())) != record['signature'] or sent != record['size'] or digest.hexdigest() != record['sha1']:
                    raise IngestError(f'File changed during upload: {record["relative"]}. Retry with Verify card contents.')
            # Do not terminate multipart until the exact content has been checked.
            connection.send(suffix)
            response = connection.getresponse()
            body = response.read()
            if response.status not in {200, 201}:
                raise ApiError(response.status, response.reason)
            result = json.loads(body)
            if not result.get('id') or result.get('status') not in {'created', 'duplicate'}:
                raise IngestError('Immich returned an unconfirmed upload. Run Sync again to check it.')
            return result
        finally:
            connection.close()

def connect(config):
    upload_key = keychain(config['uploadKeyService'], config['keyAccount'])
    stack_key = keychain(config['stackKeyService'], config['keyAccount'])
    errors = []
    for base in config['servers']:
        api = API(base, upload_key, stack_key)
        try:
            owner = api.request('GET', '/users/me', timeout=4)
            if owner.get('id') != config['userId']:
                raise IngestError('This address signed in to a different Immich account.')
            # The stack credential has server.about and asset/stack permissions, not user.read.
            api.request('GET', '/server/about', stack=True, timeout=4)
            emit('progress', message='Connected to Immich', server=base)
            return api
        except IngestError as error:
            errors.append(str(error))
    raise IngestError('Cannot connect to Immich. Join your home network or turn on Tailscale, then choose Sync connected cards. ' + errors[-1])

def check_asset(asset, record):
    checksum = asset.get('checksum', '')
    expected = base64.b64encode(bytes.fromhex(record['sha1'])).decode()
    if checksum not in {expected, record['sha1']}:
        raise IngestError(f'Could not verify the server copy of {record["relative"]}.')
    if asset.get('isTrashed') or asset.get('isOffline'):
        raise IngestError(f'The server copy of {record["relative"]} is trashed or offline.')

def ensure_pair(api, jpeg, raw, ids, dry_run=False):
    """Only the two assets identified by this card's exact checksums may be stacked."""
    j_id, r_id = ids[jpeg['relative']], ids[raw['relative']]
    if j_id == r_id:
        return 'warning', 'RAW and JPEG resolved to the same asset; left unchanged.'
    j = api.request('GET', '/assets/' + j_id)
    r = api.request('GET', '/assets/' + r_id)
    check_asset(j, jpeg)
    check_asset(r, raw)
    js, rs = j.get('stack'), r.get('stack')
    if js or rs:
        if not (js and rs and js['id'] == rs['id'] and js['assetCount'] == rs['assetCount'] == 2):
            return 'warning', f'{jpeg["relative"]}: an existing stack contains other assets; left unchanged.'
        existing = api.request('GET', '/stacks/' + js['id'], stack=True)
        if {a['id'] for a in existing['assets']} != {j_id, r_id}:
            return 'warning', f'{jpeg["relative"]}: stack membership changed; left unchanged.'
        if existing['primaryAssetId'] == j_id:
            return 'existing', None
        if not dry_run:
            api.request('PUT', '/stacks/' + js['id'], {'primaryAssetId': j_id}, stack=True)
            verified = api.request('GET', '/stacks/' + js['id'], stack=True)
            if verified['primaryAssetId'] != j_id:
                raise IngestError('JPEG cover could not be verified.')
        return 'cover', None
    je, re = j.get('exifInfo') or {}, r.get('exifInfo') or {}
    dates = [je.get('dateTimeOriginal'), re.get('dateTimeOriginal')]
    if not all(dates):
        return 'pending', f'{jpeg["relative"]}: waiting for Immich to extract capture metadata.'
    if abs((dt.datetime.fromisoformat(dates[0].replace('Z','+00:00')) - dt.datetime.fromisoformat(dates[1].replace('Z','+00:00'))).total_seconds()) >= 1:
        return 'warning', f'{jpeg["relative"]}: capture times differ; left separate.'
    for field in ['make', 'model']:
        if je.get(field) and re.get(field) and str(je[field]).casefold() != str(re[field]).casefold():
            return 'warning', f'{jpeg["relative"]}: camera metadata differs; left separate.'
    if not dry_run:
        created = api.request('POST', '/stacks', {'assetIds': [j_id, r_id]}, stack=True)
        verified = api.request('GET', '/stacks/' + created['id'], stack=True)
        if verified['primaryAssetId'] != j_id or {a['id'] for a in verified['assets']} != {j_id, r_id}:
            raise IngestError('The new RAW/JPEG stack could not be verified. Review it in Immich.')
    return 'created', None

def today_tag():
    return 'SD imports/' + dt.datetime.now().astimezone().date().isoformat()

def finish_tags(api, state):
    path = state / 'pending-tags.json'
    pending = json.loads(path.read_text()) if path.exists() else []
    if not pending:
        return 0
    records = [{'relative':x['sha1'], 'sha1':x['sha1']} for x in pending]
    checked = api.check(records)
    groups = {}
    for item in pending:
        check = checked[item['sha1']]
        if check['action'] == 'reject':
            groups.setdefault(item['tag'], set()).add(check['assetId'])
    tagged = 0
    for name, asset_ids in groups.items():
        emit('progress', message=f'Applying upload tag: {name}')
        tags = api.request('PUT', '/tags', {'tags':[name]})
        tag = next((t for t in tags if t.get('value') == name), None)
        if not tag:
            raise IngestError('Immich did not confirm the upload-date tag. It will be retried.')
        asset_ids = sorted(asset_ids)
        for offset in range(0, len(asset_ids), 500):
            batch = asset_ids[offset:offset+500]
            results = api.request('PUT', '/tags/' + tag['id'] + '/assets', {'ids':batch})
            if len(results) != len(batch) or {r.get('id') for r in results} != set(batch):
                raise IngestError('The upload-date tag was not confirmed for every asset. It will be retried.')
            if any(not r.get('success') and r.get('error') != 'duplicate' for r in results):
                raise IngestError('An upload-date tag could not be applied. It will be retried.')
            tagged += len(batch)
    # Entries still absent from the server will get a fresh receipt when their upload is retried.
    atomic_json(path, [])
    return tagged

def sync(config, root, volume, state, dry_run=False, force_hash=False):
    api = connect(config)
    if not dry_run:
        finish_tags(api, state)
    records = scan(root)
    if not records:
        return {'uploaded':0, 'skipped':0, 'pairs':0, 'warnings':[], 'files':0, 'message':'No supported photos or videos found.'}
    pairs, warnings = pair_candidates(records)
    cache = HashCache(state / 'hashes.sqlite3')
    cached = 0
    try:
        for n, record in enumerate(records, 1):
            check_stop()
            record['sha1'], reused = cache.hash(volume, record, force=force_hash)
            cached += int(reused)
            if n == 1 or n % 10 == 0 or n == len(records):
                emit('progress', message=f'Checking card contents: {n} of {len(records)}', completed=n, total=len(records))
    finally:
        cache.close()
    checks = api.check(records)
    ids = {name:r['assetId'] for name,r in checks.items() if r['action']=='reject'}
    pending = [r for r in records if checks[r['relative']]['action']=='accept']
    emit('progress', message=f'{len(pending)} new files; {len(ids)} already in Immich', total=len(records))
    if dry_run:
        return {'uploaded':0, 'wouldUpload':len(pending), 'skipped':len(ids), 'pairs':len(pairs), 'warnings':warnings, 'files':len(records), 'dryRun':True, 'cachedHashes':cached}
    tag_name = today_tag()
    # Journal before sending bytes, so even a lost upload response retains its date tag.
    atomic_json(state / 'pending-tags.json', [{'sha1':checksum, 'tag':tag_name} for checksum in sorted({r['sha1'] for r in pending})])
    uploaded = 0
    uploaded_lock = threading.Lock()
    def send(record):
        nonlocal uploaded
        for attempt in range(3):
            check_stop()
            try:
                # Also resolves an upload whose response was lost on a previous attempt.
                if attempt:
                    repeated = api.check([record])[record['relative']]
                    if repeated['action'] == 'reject':
                        return record['relative'], repeated['assetId']
                emit('progress', message=f'Uploading {record["relative"]}')
                result = api.upload(record, lambda sent: emit('progress', message=f'Uploading {record["relative"]}: {sent * 100 // record["size"]}%'))
                with uploaded_lock:
                    uploaded += int(result['status']=='created')
                    emit('progress', message=f'Uploaded {uploaded} of {len(pending)} new files')
                return record['relative'], result['id']
            except Cancelled:
                raise
            except ApiError as error:
                if error.code < 500 and error.code not in {408,429}:
                    raise
                if attempt==2: raise
            except (OSError, http.client.HTTPException, IngestError):
                if attempt==2: raise
            STOP.wait(2 ** attempt)
        raise IngestError('Upload failed.')
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    futures = [pool.submit(send, record) for record in pending]
    try:
        for future in concurrent.futures.as_completed(futures):
            name, asset_id = future.result()
            ids[name] = asset_id
    except BaseException:
        STOP.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    check_stop()
    emit('progress', message='Verifying that every card file is present in Immich…')
    final = api.check(records)
    if any(v['action'] != 'reject' for v in final.values()):
        raise IngestError('Some files are still missing from Immich. Keep the card and run Sync again.')
    ids = {k:v['assetId'] for k,v in final.items()}
    tagged = finish_tags(api, state)
    # Persist only unambiguous checksum-identified pairs. They can finish after card removal.
    queue_path = state / 'pending-stacks.json'
    queue = json.loads(queue_path.read_text()) if queue_path.exists() else []
    by_ids = {tuple(x['ids']): x for x in queue}
    for jpeg, raw in pairs:
        pair_ids = (ids[jpeg['relative']], ids[raw['relative']])
        by_ids[pair_ids] = {'ids':list(pair_ids),'jpeg':jpeg,'raw':raw}
    queue = list(by_ids.values())
    atomic_json(queue_path, queue)
    paired, pending_count = finish_stacks(api, state, warnings, rounds=3)
    # Final card inventory catches removal, edits, and files added during this run.
    after = scan(root)
    if {r['relative']:r['signature'] for r in after} != {r['relative']:r['signature'] for r in records}:
        raise IngestError('The card changed or was removed before final verification. Run Sync again to finish checking it.')
    return {'uploaded':len(pending), 'skipped':len(records)-len(pending), 'pairs':paired, 'pendingPairs':pending_count, 'warnings':warnings, 'files':len(records), 'cachedHashes':cached, 'server':api.base, 'batchTag':tag_name if pending else None, 'tagged':tagged}

def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)

def finish_stacks(api, state, warnings, rounds=1):
    path = state / 'pending-stacks.json'
    pending = json.loads(path.read_text()) if path.exists() else []
    completed = 0
    for attempt in range(rounds):
        remaining = []
        for n, pair in enumerate(pending, 1):
            check_stop()
            ids = {pair['jpeg']['relative']:pair['ids'][0], pair['raw']['relative']:pair['ids'][1]}
            status, message = ensure_pair(api, pair['jpeg'], pair['raw'], ids)
            if status == 'pending': remaining.append(pair)
            elif status == 'warning': warnings.append(message)
            else: completed += 1
            if n==1 or n%10==0 or n==len(pending):
                emit('progress', message=f'Checking RAW/JPEG stacks: {n} of {len(pending)}')
        pending = remaining
        atomic_json(path, pending)
        if not pending or attempt == rounds-1: break
        STOP.wait(5)
    return completed, len(pending)

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--volume-id', default='manual')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force-hash', action='store_true')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--finish-stacks', action='store_true')
    args=parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state/'sync.lock').open('w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IngestError('Another card sync is already running.')
        config=json.loads(args.config.read_text())
        if args.probe:
            api=connect(config)
            api.request('POST','/assets/bulk-upload-check',{'assets':[{'id':'connection-test','checksum':'0000000000000000000000000000000000000000'}]})
            emit('result',message='Connected and upload checksum checks are available.',server=api.base)
            return
        if args.finish_stacks:
            warnings=[]
            api=connect(config)
            tagged=finish_tags(api,args.state)
            paired,pending=finish_stacks(api,args.state,warnings,rounds=3)
            result={'pairs':paired,'pendingPairs':pending,'warnings':warnings,'stackOnly':True,'tagged':tagged}
        else:
            if not args.root: raise IngestError('Choose a connected camera card.')
            result=sync(config,args.root,args.volume_id,args.state,args.dry_run,args.force_hash)
        result['finishedAt']=dt.datetime.now(dt.timezone.utc).isoformat()
        if not args.dry_run:
            latest = args.state/'last-result.json'
            if args.finish_stacks and latest.exists():
                previous = json.loads(latest.read_text())
                previous['pendingPairs'] = result['pendingPairs']
                previous['pairs'] = previous.get('pairs',0) + result['pairs']
                previous['warnings'] = list(dict.fromkeys(previous.get('warnings',[]) + result['warnings']))
                previous['stacksUpdatedAt'] = result['finishedAt']
                atomic_json(latest,previous)
            else:
                atomic_json(latest,result)
        emit('result',**result)

if __name__=='__main__':
    try:
        main()
    except Cancelled as error:
        emit('cancelled',message=str(error))
        sys.exit(130)
    except Exception as error:
        emit('error',message=str(error) if isinstance(error,IngestError) else f'Sync did not complete ({type(error).__name__}). Keep the card and try again.')
        sys.exit(1)
