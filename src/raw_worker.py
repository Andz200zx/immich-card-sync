#!/usr/bin/env python3
"""Resumable Immich RAW development worker. No original media is modified or deleted."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import ingest

RAW = ['.arw', '.nef', '.cr2', '.dng', '.cr3', '.nrw', '.crw', '.raf', '.rw2', '.orf', '.pef', '.srw']
UTC = dt.timezone.utc


class Review(ingest.IngestError):
    """An ambiguous or unsupported photo needs a person's review."""


def stamp():
    return dt.datetime.now(UTC).isoformat()


def parse_date(value):
    if not value:
        return None
    try:
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value if value.tzinfo else None
    except ValueError:
        return None


def checksum_hex(value):
    try:
        if len(value) == 40:
            bytes.fromhex(value)
            return value.lower()
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) == 20:
            return decoded.hex()
    except (ValueError, TypeError):
        pass
    raise Review('The RAW has no valid content checksum.')


def sha1(path):
    result = hashlib.sha1()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            ingest.check_stop()
            result.update(chunk)
    return result.hexdigest()


def load_api(config):
    if 'credentialsFile' not in config:
        return ingest.connect(config)
    path = Path(config['credentialsFile']).expanduser()
    stat = path.stat()
    if path.is_symlink() or stat.st_mode & 0o077 or stat.st_uid != os.getuid():
        raise ingest.IngestError('The credentials file must belong to you and have mode 600.')
    secret = json.loads(path.read_text())
    for base in config['servers']:
        api = ingest.API(base, secret['uploadKey'], secret.get('stackKey'))
        try:
            owner = api.request('GET', '/users/me', timeout=8)
            if owner.get('id') != config['userId']:
                raise Review('The server returned a different Immich account.')
            return api
        except Review:
            raise
        except ingest.IngestError:
            continue
    raise ingest.IngestError('Immich is unreachable. The worker will try again later.')


def probe(api):
    required = {'asset.read', 'asset.download', 'asset.upload', 'user.read', 'tag.asset', 'tag.create'}
    stack_required = {'asset.read', 'stack.read', 'stack.create', 'stack.update'}
    for stack, wanted in [(False, required), (True, stack_required)]:
        actual = set(api.request('GET', '/api-keys/me', stack=stack)['permissions'])
        missing = wanted - actual
        if missing and 'all' not in actual:
            raise ingest.IngestError('API key needs: ' + ', '.join(sorted(missing)))


def renderer_version(config):
    result = subprocess.run([config['renderer'], '--version'], capture_output=True, text=True, timeout=30)
    match = re.search(r'RawTherapee, version ([0-9][\w.\-]*)', result.stdout + result.stderr)
    if not match:
        raise ingest.IngestError('Cannot identify the installed RawTherapee CLI version.')
    config['rendererVersion'] = 'RawTherapee ' + match[1]


class Store:
    def __init__(self, state, config):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.state / 'worker.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, raw_checksum TEXT NOT NULL, asset TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', receipt TEXT NOT NULL DEFAULT '{}',
            attempts INTEGER NOT NULL DEFAULT 0, retry_after REAL NOT NULL DEFAULT 0,
            message TEXT, updated_at TEXT NOT NULL)''')
        identity = {'userId': config['userId'], 'servers': config['servers']}
        previous = self.get('identity')
        if previous and (previous['userId'] != identity['userId'] or
                         not set(previous['servers']).intersection(identity['servers'])):
            raise Review('This worker state belongs to another library. Use a separate state directory.')
        self.put('identity', identity)

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))
        self.db.commit()

    def add(self, asset):
        # Validate the identifier before it can be used in a local scratch path.
        uuid.UUID(asset['id'])
        checksum = checksum_hex(asset['checksum'])
        old = self.db.execute('SELECT raw_checksum FROM jobs WHERE id=?', (asset['id'],)).fetchone()
        if old and old[0] != checksum:
            self.update(asset['id'], 'review', message='The original checksum changed; left for review.')
            return
        self.db.execute('''INSERT OR IGNORE INTO jobs(id,raw_checksum,asset,updated_at)
                           VALUES (?,?,?,?)''', (asset['id'], checksum, json.dumps(asset), stamp()))
        self.db.commit()

    def job(self, aid):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (aid,)).fetchone()
        if not row:
            return None
        row = dict(row)
        row['asset'] = json.loads(row['asset'])
        row['receipt'] = json.loads(row['receipt'])
        return row

    def update(self, aid, status, receipt=None, message=None, retry_after=0, attempt=False):
        self.db.execute('''UPDATE jobs SET status=?,message=?,updated_at=?,retry_after=?,
                           attempts=attempts+?,receipt=COALESCE(?,receipt) WHERE id=?''',
                        (status, message, stamp(), retry_after, int(attempt),
                         json.dumps(receipt) if receipt is not None else None, aid))
        self.db.commit()

    def pending(self, limit):
        return [r[0] for r in self.db.execute('''SELECT id FROM jobs
            WHERE status IN ('queued','prepared','uploaded','retry') AND retry_after<=?
            ORDER BY CASE WHEN status='uploaded' THEN 0 WHEN status='prepared' THEN 1 ELSE 2 END,
            rowid LIMIT ?''', (time.time(), limit))]

    def summary(self):
        return dict(self.db.execute('SELECT status,count(*) FROM jobs GROUP BY status'))


def scan(api, store, pages):
    """Round-robin bounded scans; stacking cannot change pagination because withStacked is true."""
    sources = [(extension, visibility) for extension in RAW for visibility in ['timeline', 'archive']]
    offset = store.get('scanOffset', 0)
    visited = 0
    while pages > 0 and visited < len(sources):
        extension, visibility = sources[offset % len(sources)]
        offset += 1
        visited += 1
        key = 'scan:' + extension + ':' + visibility
        cursor = store.get(key, {'page': 1, 'before': stamp(), 'after': None})
        if cursor.get('waitUntil', 0) > time.time():
            continue
        if 'before' not in cursor:
            # A weekly full sweep catches older assets whose metadata was repaired later.
            after = cursor.get('after') if time.time() - cursor.get('fullSweepAt', 0) < 7 * 86400 else None
            cursor = {**cursor, 'page': 1, 'before': stamp(), 'after': after}
        query = {'type': 'IMAGE', 'originalFileName': extension, 'visibility': visibility,
                 'withExif': True, 'withStacked': True, 'withDeleted': False,
                 'size': 100, 'page': cursor['page'], 'order': 'asc', 'createdBefore': cursor['before']}
        if cursor.get('after'):
            query['createdAfter'] = cursor['after']
        result = api.request('POST', '/search/metadata', query)['assets']
        for asset in result['items']:
            if (Path(asset['originalFileName']).suffix.lower() == extension and
                    asset.get('ownerId') == store.get('identity')['userId']):
                store.add(asset)
        if result.get('nextPage'):
            cursor['page'] += 1
        else:
            full = time.time() if not cursor.get('after') else cursor.get('fullSweepAt', 0)
            after = (parse_date(cursor['before']) - dt.timedelta(minutes=5)).isoformat()
            cursor = {'after': after, 'fullSweepAt': full, 'waitUntil': time.time() + 3600}
        store.put(key, cursor)
        store.put('scanOffset', offset)
        pages -= 1
        ingest.emit('scan', extension=extension, visibility=visibility, found=len(result['items']))


def check_source(asset, job, owner):
    if asset.get('ownerId') != owner or checksum_hex(asset.get('checksum', '')) != job['raw_checksum']:
        raise Review('The RAW identity or account changed.')
    if asset.get('isTrashed') or asset.get('isOffline'):
        raise Review('The RAW is trashed or offline.')
    if asset.get('visibility', 'timeline') not in {'timeline', 'archive'}:
        raise Review('Hidden and locked photos require manual review.')
    if Path(asset['originalFileName']).suffix.lower() not in RAW:
        raise Review('This asset is not a supported RAW file.')


def jpeg_equivalent(api, raw, ignore_id=None):
    """Conservative matching: renamed same-time JPEGs are reviewed, never guessed into a stack."""
    exif = raw.get('exifInfo') or {}
    taken = parse_date(exif.get('dateTimeOriginal'))
    if not taken or not exif.get('model'):
        raise Review('Capture date or camera model is missing; cannot safely rule out an existing JPEG.')
    query = {'type': 'IMAGE', 'takenAfter': (taken - dt.timedelta(seconds=1)).isoformat(),
             'takenBefore': (taken + dt.timedelta(seconds=1)).isoformat(),
             'model': exif['model'], 'withExif': True, 'withStacked': True, 'size': 1000, 'page': 1}
    candidates = []
    for visibility in ['timeline', 'archive']:
        result = api.request('POST', '/search/metadata', {**query, 'visibility': visibility})['assets']
        if result.get('nextPage'):
            raise Review('Too many same-time candidates to safely identify a JPEG equivalent.')
        candidates.extend(result['items'])
    renamed = False
    for candidate in candidates:
        if (candidate['id'] == ignore_id or candidate.get('ownerId') != raw['ownerId'] or
                Path(candidate['originalFileName']).suffix.lower() not in ingest.JPEG or
                candidate.get('isTrashed')):
            continue
        ce = candidate.get('exifInfo') or {}
        ct = parse_date(ce.get('dateTimeOriginal'))
        if not ct or abs((taken - ct).total_seconds()) >= 1:
            continue
        if any(exif.get(k) and ce.get(k) and exif[k].casefold() != ce[k].casefold() for k in ['make', 'model']):
            continue
        if Path(candidate['originalFileName']).stem.casefold() == Path(raw['originalFileName']).stem.casefold():
            return candidate['id']
        renamed = True
    if renamed:
        raise Review('A differently named JPEG has matching capture metadata; left for review.')
    # Legacy exports can lose camera-specific timezone data too. A same-name JPEG with
    # the same local clock but a different resolved instant must not cause another render.
    name_query = {**query, 'originalFileName': Path(raw['originalFileName']).stem,
                  'takenAfter': (taken - dt.timedelta(hours=26)).isoformat(),
                  'takenBefore': (taken + dt.timedelta(hours=26)).isoformat()}
    local = parse_date(raw.get('localDateTime')) or taken
    for visibility in ['timeline', 'archive']:
        result = api.request('POST', '/search/metadata', {**name_query, 'visibility': visibility})['assets']
        if result.get('nextPage'):
            raise Review('Too many same-name JPEG candidates; left for review.')
        for candidate in result['items']:
            p = Path(candidate['originalFileName'])
            if (candidate['id'] == ignore_id or candidate.get('ownerId') != raw['ownerId'] or
                    candidate.get('isTrashed') or p.suffix.lower() not in ingest.JPEG or
                    p.stem.casefold() != Path(raw['originalFileName']).stem.casefold()):
                continue
            ce = candidate.get('exifInfo') or {}
            if ce.get('model') and ce['model'].casefold() != exif['model'].casefold():
                continue
            clock = parse_date(candidate.get('localDateTime')) or parse_date(ce.get('dateTimeOriginal'))
            if not clock or abs((clock.replace(tzinfo=None) - local.replace(tzinfo=None)).total_seconds()) < 1:
                raise Review('A same-name JPEG has missing or different timezone metadata; left for review.')
    return None


def download(api, asset, destination, maximum):
    expected = checksum_hex(asset['checksum'])
    if destination.exists() and sha1(destination) == expected:
        return
    part = destination.with_suffix(destination.suffix + '.part')
    request = urllib.request.Request(api.base + '/api/assets/' + asset['id'] + '/original?edited=false',
                                     headers={'x-api-key': api.key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ingest.NoRedirect())
    digest = hashlib.sha1()
    size = 0
    try:
        with opener.open(request, timeout=120) as response, part.open('wb') as target:
            if int(response.headers.get('Content-Length', 0)) > maximum:
                raise Review('The RAW exceeds the configured scratch-file limit.')
            while chunk := response.read(1024 * 1024):
                ingest.check_stop()
                size += len(chunk)
                if size > maximum:
                    raise Review('The RAW exceeds the configured scratch-file limit.')
                target.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ingest.IngestError('Downloaded RAW checksum did not match Immich.')
        part.replace(destination)
    except urllib.error.HTTPError as error:
        raise ingest.ApiError(error.code, error.reason) from None
    finally:
        part.unlink(missing_ok=True)


def jpeg_size(path):
    """Read JPEG SOF dimensions from the actual image stream, not copied EXIF dimensions."""
    with Path(path).open('rb') as stream:
        if stream.read(2) != b'\xff\xd8':
            raise Review('The renderer did not produce a JPEG.')
        while True:
            prefix = stream.read(1)
            if prefix != b'\xff':
                raise Review('The JPEG header is incomplete.')
            marker = stream.read(1)
            while marker == b'\xff':
                marker = stream.read(1)
            if not marker or marker[0] in {0xda, 0xd9}:
                raise Review('No JPEG pixel dimensions were found.')
            length_bytes = stream.read(2)
            if len(length_bytes) != 2:
                raise Review('The JPEG is truncated.')
            length = struct.unpack('>H', length_bytes)[0]
            if length < 2:
                raise Review('The JPEG segment length is invalid.')
            if marker[0] in {0xc0, 0xc1, 0xc2}:
                data = stream.read(5)
                if len(data) != 5:
                    raise Review('The JPEG dimensions are truncated.')
                height, width = struct.unpack('>HH', data[1:])
                return width, height
            stream.seek(length - 2, 1)


def command(argv, log=None, env=None, timeout=600):
    with (log.open('ab') if log else open(os.devnull, 'wb')) as output:
        child = subprocess.Popen(argv, stdout=output, stderr=output, env=env, start_new_session=True)
        started = time.monotonic()
        while child.poll() is None:
            if ingest.STOP.wait(0.2) or time.monotonic() - started > timeout:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                ingest.check_stop()
                raise ingest.IngestError('The renderer timed out.')
        if child.returncode:
            raise Review('Conversion or metadata copy failed; see the per-photo renderer log.')


def capture_tags(raw):
    # Some cameras store the timezone only in MakerNotes. Preserve Immich's resolved instant
    # and wall-clock time explicitly when writing a standard JPEG without those RAW MakerNotes.
    taken = parse_date((raw.get('exifInfo') or {}).get('dateTimeOriginal'))
    if not taken:
        raise Review('No resolved capture time is available for this RAW.')
    local = parse_date(raw.get('localDateTime')) or taken
    local_clock = local.replace(tzinfo=None)
    seconds = (local_clock - taken.astimezone(UTC).replace(tzinfo=None)).total_seconds()
    if abs(seconds) > 14 * 3600 or seconds % 60:
        raise Review('The RAW capture time and local timezone are inconsistent.')
    minutes = int(abs(seconds) // 60)
    offset = ('-' if seconds < 0 else '+') + f'{minutes // 60:02}:{minutes % 60:02}'
    date = local_clock.strftime('%Y:%m:%d %H:%M:%S')
    fraction = f'{local_clock.microsecond:06}'.rstrip('0')
    return ['-DateTimeOriginal=' + date, '-CreateDate=' + date,
            '-OffsetTimeOriginal=' + offset, '-OffsetTimeDigitized=' + offset,
            '-SubSecTimeOriginal=' + fraction, '-SubSecTimeDigitized=' + fraction]


def render(config, raw, source, output):
    tool = config['exiftool']
    metadata = json.loads(subprocess.check_output([tool, '-j', '-n', '-ImageWidth', '-ImageHeight',
        '-ExifImageWidth', '-ExifImageHeight', '-RawImageWidth', '-RawImageHeight', '-ISO', str(source)], timeout=30))[0]
    dimensions = [(metadata.get(w, 0), metadata.get(h, 0)) for w, h in
                  [('ImageWidth', 'ImageHeight'), ('ExifImageWidth', 'ExifImageHeight'), ('RawImageWidth', 'RawImageHeight')]]
    dimensions += [((raw.get('exifInfo') or {}).get('exifImageWidth') or 0,
                    (raw.get('exifInfo') or {}).get('exifImageHeight') or 0)]
    expected = max(dimensions, key=lambda pair: pair[0] * pair[1])
    if min(expected) < 256:
        raise Review('Cannot verify the full RAW resolution from metadata.')
    iso = metadata.get('ISO') or 100
    level = 'High' if iso > 3200 else 'Medium' if iso >= 800 else 'Low'
    base_profile = Path(config['profilesDirectory']) / f'Auto-Matched Curve - ISO {level}.pp3'
    overlay = Path(__file__).with_name('raw-full-resolution.pp3')
    recipe = hashlib.sha256(base_profile.read_bytes() + overlay.read_bytes() +
                            config['rendererVersion'].encode()).hexdigest()
    env = {**os.environ, 'OMP_NUM_THREADS': str(config.get('threads', 2))}
    log = output.parent / 'renderer.log'
    output.unlink(missing_ok=True)
    command([config['renderer'], '-o', str(output), '-p', str(base_profile), '-p', str(overlay),
             '-j95', '-js3', '-c', str(source)], log=log, env=env)
    width, height = jpeg_size(output)
    if any(actual < target * 0.90 or actual > target * 1.10
           for actual, target in zip(sorted((width, height)), sorted(expected))):
        raise Review(f'Output {width}x{height} is inconsistent with the full RAW resolution {expected[0]}x{expected[1]}.')
    provenance = 'Immich RAW Worker; source=' + raw['id'] + '; sha1=' + checksum_hex(raw['checksum']) + '; recipe=' + recipe
    command([tool, '-overwrite_original', '-TagsFromFile', str(source), '-EXIF:all', '-GPS:all',
             '--MakerNotes', '--IFD1:all', '-Orientation#=1', '-ColorSpace#=1',
             '-ExifImageWidth=' + str(width), '-ExifImageHeight=' + str(height),
             '-UserComment=' + provenance, '-Software=Immich RAW Worker / ' + config['rendererVersion'],
             *capture_tags(raw), str(output)], log=log, timeout=60)
    if jpeg_size(output) != (width, height):
        raise Review('JPEG dimensions changed during metadata copy.')
    return {'width': width, 'height': height, 'recipe': recipe, 'profile': level}


def folder_for(store, aid):
    uuid.UUID(aid)
    folder = store.state / 'scratch' / aid
    if folder.is_symlink():
        raise Review('Scratch directory must not be a symbolic link.')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def cleanup(store, aid):
    folder = store.state / 'scratch' / aid
    if folder.exists() and not folder.is_symlink():
        shutil.rmtree(folder)


def prepare(config, api, store, raw):
    folder = folder_for(store, raw['id'])
    minimum = config.get('minimumFreeGB', 8) * 1024 ** 3
    if shutil.disk_usage(folder).free < minimum:
        raise ingest.IngestError('Not enough free scratch space. Waiting for disk space to be available.')
    source = folder / ('original' + Path(raw['originalFileName']).suffix.lower())
    # The server permits duplicate filenames; provenance and checksums disambiguate different cameras.
    stem = Path(raw['originalFileName']).stem.replace('\n', '_').replace('\r', '_')[:160]
    output = folder / (stem + '.JPG')
    download(api, raw, source, config.get('maxRawMB', 1024) * 1024 ** 2)
    details = render(config, raw, source, output)
    return {**details, 'filename': output.name, 'sha1': sha1(output), 'size': output.stat().st_size,
            'tag': 'RAW renders/' + dt.datetime.now().astimezone().date().isoformat()}


def tag_companion(api, raw, jpeg_id, tag_name):
    tags = api.request('PUT', '/tags', {'tags': [tag_name]})
    generated_tag = next((t for t in tags if t.get('value') == tag_name), None)
    if not generated_tag:
        raise ingest.IngestError('Immich did not confirm the render-batch tag.')
    tag_ids = {t['id'] for t in (raw.get('tags') or [])} | {generated_tag['id']}
    for tag_id in sorted(tag_ids):
        results = api.request('PUT', '/tags/' + tag_id + '/assets', {'ids': [jpeg_id]})
        if (len(results) != 1 or results[0].get('id') != jpeg_id or
                not results[0].get('success') and results[0].get('error') != 'duplicate'):
            raise ingest.IngestError('A companion tag was not confirmed; it will be retried.')


def process(config, api, store, aid):
    job = store.job(aid)
    raw = api.request('GET', '/assets/' + aid)
    check_source(raw, job, config['userId'])
    receipt = job['receipt']
    if receipt.get('sha1'):
        checked = api.check([{'relative': aid, 'sha1': receipt['sha1']}])[aid]
        if checked['action'] == 'reject':
            receipt['jpegId'] = checked['assetId']
        elif receipt.get('jpegId'):
            raise Review('A previously uploaded companion is missing; left for review.')
    if not receipt.get('jpegId'):
        if raw.get('stack'):
            stack = api.request('GET', '/stacks/' + raw['stack']['id'], stack=True)
            if any(Path(a['originalFileName']).suffix.lower() in ingest.JPEG for a in stack['assets']):
                store.update(aid, 'skipped', message='An existing stack already contains a JPEG.')
                cleanup(store, aid)
                return
            raise Review('The RAW is already in a stack without a JPEG; preserve its membership for review.')
        if jpeg_equivalent(api, raw):
            store.update(aid, 'skipped', message='A JPEG equivalent is already in Immich.')
            cleanup(store, aid)
            return
        output = folder_for(store, aid) / receipt.get('filename', 'not-prepared')
        if not receipt.get('sha1') or not output.exists() or sha1(output) != receipt['sha1']:
            receipt = prepare(config, api, store, raw)
            output = folder_for(store, aid) / receipt['filename']
        # Commit before any upload so a lost response can be recovered by checksum.
        store.update(aid, 'prepared', receipt=receipt)
        current = api.request('GET', '/assets/' + aid)
        check_source(current, job, config['userId'])
        if current.get('stack') or jpeg_equivalent(api, current):
            raise Review('A JPEG or stack appeared during conversion; left for review.')
        record = {'path': str(output), 'relative': aid, 'signature': ingest.signature(output.stat()),
                  'size': output.stat().st_size, 'sha1': receipt['sha1'],
                  'fileCreatedAt': raw['fileCreatedAt'], 'fileModifiedAt': raw['fileModifiedAt'],
                  'visibility': raw.get('visibility', 'timeline')}
        uploaded = api.upload(record)
        receipt['jpegId'] = uploaded['id']
        store.update(aid, 'uploaded', receipt=receipt)
    jpeg_id = receipt['jpegId']
    jpeg = api.request('GET', '/assets/' + jpeg_id)
    ingest.check_asset(jpeg, {'sha1': receipt['sha1'], 'relative': receipt['filename']})
    if jpeg.get('ownerId') != config['userId']:
        raise Review('The JPEG belongs to a different account.')
    raw = api.request('GET', '/assets/' + aid)
    check_source(raw, job, config['userId'])
    if jpeg.get('visibility', 'timeline') != raw.get('visibility', 'timeline'):
        raise Review('RAW/JPEG visibility differs; left for review.')
    if jpeg_equivalent(api, raw, ignore_id=jpeg_id):
        raise Review('Another JPEG equivalent appeared; the generated companion is left for review.')
    # Existing matching two-file stacks may be repaired, but other stack members are never removed.
    status, reason = ingest.ensure_pair(api, {'relative': 'jpeg', 'sha1': receipt['sha1']},
        {'relative': 'raw', 'sha1': job['raw_checksum']}, {'jpeg': jpeg_id, 'raw': aid})
    if status == 'warning':
        raise Review(reason)
    if status == 'pending':
        store.update(aid, 'uploaded', receipt=receipt, message=reason, retry_after=time.time() + 120)
        return
    tag_companion(api, raw, jpeg_id, receipt['tag'])
    store.update(aid, 'done', receipt=receipt)
    cleanup(store, aid)
    ingest.emit('converted', source=aid, jpeg=jpeg_id, width=receipt['width'], height=receipt['height'])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('action', choices=['probe', 'status', 'scan', 'sample', 'run', 'retry'])
    parser.add_argument('--asset-id')
    parser.add_argument('--limit', type=int, default=250)
    parser.add_argument('--max-conversions', type=int, default=25)
    parser.add_argument('--max-seconds', type=int, default=900)
    parser.add_argument('--scan-pages', type=int, default=12)
    args = parser.parse_args()
    signal.signal(signal.SIGINT, lambda *_: ingest.STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: ingest.STOP.set())
    config = json.loads(args.config.read_text())
    store = Store(args.state, config)
    with (args.state / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ingest.IngestError('The worker is already running.')
        if args.action == 'status':
            ingest.emit('status', jobs=store.summary())
            return
        api = load_api(config)
        if args.action in {'probe', 'sample', 'run'}:
            renderer_version(config)
        if args.action == 'probe':
            probe(api)
            for path in [config['renderer'], config['exiftool'], config['profilesDirectory']]:
                if not Path(path).exists():
                    raise ingest.IngestError('Missing dependency: ' + path)
            ingest.emit('ready', message='Connected. Required API permissions and dependencies are available.')
            return
        if args.asset_id:
            uuid.UUID(args.asset_id)
            asset = api.request('GET', '/assets/' + args.asset_id)
            if asset.get('ownerId') != config['userId']:
                raise Review('The requested photo belongs to a different account.')
            store.add(asset)
            if args.action == 'retry':
                store.update(args.asset_id, 'queued')
            if args.action == 'sample':
                check_source(asset, store.job(args.asset_id), config['userId'])
                result = prepare(config, api, store, asset)
                ingest.atomic_json(folder_for(store, args.asset_id) / 'sample.json', result)
                ingest.emit('sample', source=args.asset_id, **result)
                return
        elif args.action in {'sample', 'retry'}:
            parser.error('--asset-id is required for sample or retry')
        if args.action in {'scan', 'run'}:
            scan(api, store, max(0, args.scan_pages))
        if args.action == 'run':
            started = time.monotonic()
            conversions = 0
            # Fast skips can drain an already-paired library without making the backfill take months.
            for aid in store.pending(max(0, args.limit)):
                ingest.check_stop()
                if conversions >= args.max_conversions or time.monotonic() - started >= args.max_seconds:
                    break
                try:
                    process(config, api, store, aid)
                    if store.job(aid)['status'] in {'done', 'prepared', 'uploaded'}:
                        conversions += 1
                except ingest.Cancelled:
                    raise
                except Review as error:
                    store.update(aid, 'review', message=str(error))
                    ingest.emit('review', source=aid, message=str(error))
                    folder = store.state / 'scratch' / aid
                    log = folder / 'renderer.log'
                    if log.exists():
                        logs = store.state / 'review-logs'
                        logs.mkdir(exist_ok=True, mode=0o700)
                        (logs / (aid + '.log')).write_bytes(log.read_bytes()[-100000:])
                        for old in sorted(logs.glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)[100:]:
                            old.unlink()
                    cleanup(store, aid)
                except Exception as error:
                    attempts = store.job(aid)['attempts']
                    delay = min(86400, 300 * 2 ** min(attempts, 8))
                    message = str(error) if isinstance(error, ingest.IngestError) else type(error).__name__
                    store.update(aid, 'retry', message=message, retry_after=time.time() + delay, attempt=True)
                    ingest.emit('retry', source=aid, message=message)
        summary = {'finishedAt': stamp(), 'jobs': store.summary()}
        ingest.atomic_json(args.state / 'last-result.json', summary)
        ingest.emit('result', **summary)


if __name__ == '__main__':
    try:
        main()
    except ingest.Cancelled:
        sys.exit(130)
    except Exception as error:
        ingest.emit('error', message=str(error) if isinstance(error, ingest.IngestError) else type(error).__name__)
        sys.exit(1)
