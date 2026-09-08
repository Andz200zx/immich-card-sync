#!/usr/bin/env python3
"""Install the optional RAW worker on a processing Mac; separate from the card helper."""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from install import normalize_url, NoRedirect

LABEL = 'io.github.andz200zx.immich-raw-worker'


def install(enable=False):
    os.umask(0o077)
    if sys.platform != 'darwin' or sys.version_info < (3, 10):
        raise ValueError('The worker installer requires macOS and Python 3.10 or newer.')
    package = Path(__file__).resolve().parents[1]
    source = package/'RAW Worker' if (package/'RAW Worker').exists() else package/'src'
    support = Path.home()/'Library/Application Support/Immich RAW Worker'
    support.mkdir(parents=True, exist_ok=True, mode=0o700)
    (support/'state').mkdir(exist_ok=True, mode=0o700)
    agent = Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    agent.parent.mkdir(parents=True, exist_ok=True)
    config_path = support/'config.json'
    if not config_path.exists():
        server = normalize_url(input('Immich server URL: '))
        fallback = input('Optional fallback URL: ').strip()
        key = getpass.getpass('Immich API key (hidden): ').strip()
        if not key:
            raise ValueError('An API key is required.')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(server+'/api/users/me', headers={'x-api-key': key})
        with opener.open(request, timeout=15) as response:
            owner = json.load(response)
        credentials = support/'credentials.json'
        credentials.write_text(json.dumps({'uploadKey': key}))
        credentials.chmod(0o600)
        config = {'servers': [server] + ([normalize_url(fallback)] if fallback else []),
            'userId': owner['id'], 'credentialsFile': str(credentials),
            'renderer': shutil.which('rawtherapee-cli') or '/opt/homebrew/bin/rawtherapee-cli',
            'rendererVersion': 'RawTherapee 5.13',
            'exiftool': shutil.which('exiftool') or '/opt/homebrew/bin/exiftool',
            'profilesDirectory': '/Applications/RawTherapee.app/Contents/Resources/share/profiles',
            'threads': 2, 'minimumFreeGB': 8, 'maxRawMB': 1024}
        config_path.write_text(json.dumps(config, indent=2))
        config_path.chmod(0o600)
    with (support/'state/worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('The worker is busy. Wait for this batch to finish before installing.')
        with tempfile.TemporaryDirectory(prefix='.install-', dir=support) as temp:
            stage = Path(temp)/'bin'
            stage.mkdir()
            for name in ['ingest.py', 'raw_worker.py', 'raw_service.py', 'raw-full-resolution.pp3']:
                shutil.copy2(source/name, stage/name)
            subprocess.run([sys.executable, str(stage/'raw_worker.py'), '--config', str(config_path),
                            '--state', str(Path(temp)/'probe-state'), 'probe'], check=True)
            label_target = f'gui/{os.getuid()}/{LABEL}'
            was_loaded = subprocess.run(['launchctl', 'print', label_target], capture_output=True).returncode == 0
            old_agent = agent.read_bytes() if agent.exists() else None
            old = Path(temp)/'old-bin'
            if was_loaded:
                subprocess.run(['launchctl', 'bootout', label_target], check=True)
            try:
                if (support/'bin').exists():
                    (support/'bin').rename(old)
                stage.rename(support/'bin')
                plist = {'Label': LABEL, 'RunAtLoad': True, 'StartInterval': 300,
                    'ProcessType': 'Background', 'Nice': 10,
                    'ProgramArguments': ['/usr/bin/caffeinate', '-i', sys.executable, str(support/'bin/raw_service.py')],
                    'WorkingDirectory': str(support)}
                (support/'launch-agent.plist').write_bytes(plistlib.dumps(plist))
                if enable or old_agent is not None:
                    agent.write_bytes(plistlib.dumps(plist))
                if enable or was_loaded:
                    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(agent)], check=True)
            except Exception:
                subprocess.run(['launchctl', 'bootout', label_target], capture_output=True)
                if (support/'bin').exists():
                    shutil.rmtree(support/'bin')
                if old.exists():
                    old.rename(support/'bin')
                if old_agent is not None:
                    agent.write_bytes(old_agent)
                    if was_loaded:
                        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(agent)], check=True)
                else:
                    agent.unlink(missing_ok=True)
                raise
    print('RAW worker installed. It checks for new RAWs every five minutes while you are logged in.'
          if enable or was_loaded else 'RAW worker installed. Enable it when ready using --enable.')
    print('Logs and progress: ' + str(support))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    try:
        install(args.enable)
    except Exception as error:
        print('Installation did not complete: ' + str(error), file=sys.stderr)
        sys.exit(1)
