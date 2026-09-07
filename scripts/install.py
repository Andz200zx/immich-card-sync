#!/usr/bin/env python3
"""Interactive installation of the downloaded app; credentials go directly to Keychain."""
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
import urllib.parse
import urllib.request
import uuid

LABEL = 'io.github.andz200zx.immich-card-sync'
SERVICE = 'immich-card-sync'
LEGACY_LABEL = 'local.andz.immich-card-sync'

def normalize_url(value):
    u = urllib.parse.urlsplit(value.strip())
    if u.scheme not in {'http','https'} or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('Enter an http(s) server address without credentials, query, or fragment.')
    path = u.path.rstrip('/')
    if path in {'/photos','/api'}:path = ''
    return urllib.parse.urlunsplit((u.scheme,u.netloc,path,'',''))

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Use the final server address; API redirects are not followed.')

def configure(executable, config_path, key_account=None):
    server = normalize_url(input('Immich server URL: '))
    fallback = input('Optional second URL (e.g. Tailscale; Enter to skip): ').strip()
    servers = [server] + ([normalize_url(fallback)] if fallback else [])
    key = getpass.getpass('Immich API key (hidden): ').strip()
    if not key:raise ValueError('An API key is required.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    req = urllib.request.Request(server+'/api/users/me',headers={'x-api-key':key})
    with opener.open(req,timeout=15) as response:owner = json.load(response)
    if not owner.get('id'):raise ValueError('Immich did not identify your account.')
    account = key_account or server
    subprocess.run([str(executable),'--store-key-stdin',SERVICE,account],input=key.encode(),check=True)
    config = {'servers':servers,'userId':owner['id'],'keyAccount':account,
              'uploadKeyService':SERVICE,'stackKeyService':SERVICE,'pythonPath':sys.executable}
    temp = config_path.with_suffix('.tmp');temp.write_text(json.dumps(config,indent=2));temp.chmod(0o600);temp.replace(config_path)

def same_library(previous, current):
    """Pending asset IDs are safe only for the same account and known server."""
    if not previous.get('userId') or previous.get('userId') != current.get('userId'):
        return False
    old_servers = {normalize_url(url) for url in previous.get('servers', [])}
    new_servers = {normalize_url(url) for url in current.get('servers', [])}
    return bool(old_servers & new_servers)

def remove_path(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()

def install(package, home, reconfigure=False):
    support = home/'Library/Application Support/Immich Card Sync'
    app = home/'Applications/Immich Card Sync.app'
    agent = home/'Library/LaunchAgents'/f'{LABEL}.plist'
    legacy_agent = agent.with_name(f'{LEGACY_LABEL}.plist')
    source = package/'Immich Card Sync.app'
    for relative in ['Contents/MacOS/Immich Card Sync', 'Contents/Resources/ingest.py', 'Contents/Info.plist']:
        if not (source/relative).is_file():
            raise ValueError('Use the release ZIP, or run scripts/build.sh first.')
    for p in [support/'state',support/'logs',app.parent,agent.parent]:p.mkdir(parents=True,exist_ok=True)
    with (support/'state/sync.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('A sync is running. Let it finish or stop it before installing.')
        config = support/'config.json'
        previous = json.loads(config.read_text()) if config.exists() else {}
        credential_account = None
        committed = False
        # Staging and probing must not replace the running app or its credentials.
        with tempfile.TemporaryDirectory(prefix='.install-', dir=support) as temporary:
            stage = Path(temporary)
            staged_app = stage/app.name
            staged_config = stage/'config.json'
            try:
                shutil.copytree(source, staged_app)
                if not config.exists() or reconfigure:
                    credential_account = 'installation-' + uuid.uuid4().hex
                    configure(staged_app/'Contents/MacOS/Immich Card Sync', staged_config, credential_account)
                    current = json.loads(staged_config.read_text())
                else:
                    current = dict(previous)
                current['pythonPath'] = sys.executable
                staged_config.write_text(json.dumps(current, indent=2))
                staged_config.chmod(0o600)
                subprocess.run([sys.executable, str(staged_app/'Contents/Resources/ingest.py'),
                                '--config', str(staged_config), '--state', str(stage/'probe-state'), '--probe'], check=True)
                reset_state = not same_library(previous, current)
                if reset_state and any(path.exists() and json.loads(path.read_text()) for path in
                                       [support/'state/pending-tags.json', support/'state/pending-stacks.json']):
                    raise ValueError('Finish pending tags and stacks with the current server/account before changing libraries.')
                domain = f'gui/{os.getuid()}'
                loaded = {}
                for label, path in [(LABEL, agent), (LEGACY_LABEL, legacy_agent)]:
                    loaded[label] = subprocess.run(['/bin/launchctl', 'print', f'{domain}/{label}'], capture_output=True).returncode == 0
                    if loaded[label] and not path.exists():
                        raise ValueError(f'The running {label} login service has no plist to restore. Quit it before reinstalling.')
                value = {'Label': LABEL, 'ProgramArguments': [str(app/'Contents/MacOS/Immich Card Sync')],
                         'RunAtLoad': True, 'ProcessType': 'Interactive', 'LimitLoadToSessionType': 'Aqua', 'WorkingDirectory': str(support),
                         'StandardOutPath': str(support/'logs/agent.log'), 'StandardErrorPath': str(support/'logs/agent.log')}
                staged_agent = stage/agent.name
                staged_agent.write_bytes(plistlib.dumps(value))
                staged_agent.chmod(0o644)
                backup = support/'backups'/('installation-' + uuid.uuid4().hex)
                backup.mkdir(parents=True)
                changes = []
                stopped = []
                bootstrap_attempted = False

                def replace(target, replacement, name):
                    saved = backup/name
                    existed = target.exists() or target.is_symlink()
                    if existed:
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        target.rename(saved)
                    changes.append((target, saved if existed else None))
                    if replacement is not None:
                        replacement.rename(target)

                try:
                    for label in [LABEL, LEGACY_LABEL]:
                        if loaded[label]:
                            subprocess.run(['/bin/launchctl', 'bootout', f'{domain}/{label}'], check=True, capture_output=True)
                            stopped.append(label)
                    replace(app, staged_app, app.name)
                    replace(config, staged_config, 'config.json')
                    replace(agent, staged_agent, agent.name)
                    if legacy_agent.exists():
                        replace(legacy_agent, None, legacy_agent.name)
                    if reset_state:
                        for path in list((support/'state').iterdir()):
                            if path.name != 'sync.lock':
                                replace(path, None, 'state/' + path.name)
                    bootstrap_attempted = True
                    subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(agent)], check=True)
                    committed = True
                except BaseException as error:
                    recovery_errors = []
                    if bootstrap_attempted:
                        try:
                            subprocess.run(['/bin/launchctl', 'bootout', f'{domain}/{LABEL}'], capture_output=True)
                        except OSError as recovery_error:
                            recovery_errors.append(str(recovery_error))
                    for target, saved in reversed(changes):
                        try:
                            remove_path(target)
                            if saved is not None:
                                saved.rename(target)
                        except OSError as recovery_error:
                            recovery_errors.append(str(recovery_error))
                    for label in stopped:
                        path = agent if label == LABEL else legacy_agent
                        try:
                            subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(path)], check=True, capture_output=True)
                        except (OSError, subprocess.SubprocessError) as recovery_error:
                            recovery_errors.append(str(recovery_error))
                    if recovery_errors:
                        raise ValueError(f'Installation failed; recovery needs attention. Backups: {backup}. ' + '; '.join(recovery_errors)) from error
                    raise
            finally:
                if credential_account and not committed:
                    # This is a newly staged credential; the working key was never changed.
                    subprocess.run(['/usr/bin/security', 'delete-generic-password', '-s', SERVICE, '-a', credential_account], capture_output=True)
    print('Installed. Insert a camera card to begin. The app will start at login.')

def main():
    if sys.platform != 'darwin':raise ValueError('The desktop app requires macOS.')
    if sys.version_info < (3,10):raise ValueError('Python 3.10 or later is required.')
    os.umask(0o077)
    install(Path(__file__).resolve().parent.parent, Path.home(), '--configure' in sys.argv)

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print(f'Installation did not complete: {error}',file=sys.stderr);sys.exit(1)
