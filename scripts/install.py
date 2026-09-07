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
import time
import urllib.parse
import urllib.request

LABEL = 'io.github.andz200zx.immich-card-sync'
SERVICE = 'immich-card-sync'

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

def configure(executable, config_path):
    server = normalize_url(input('Immich server URL: '))
    fallback = input('Optional second URL (e.g. Tailscale; Enter to skip): ').strip()
    servers = [server] + ([normalize_url(fallback)] if fallback else [])
    key = getpass.getpass('Immich API key (hidden): ').strip()
    if not key:raise ValueError('An API key is required.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    req = urllib.request.Request(server+'/api/users/me',headers={'x-api-key':key})
    with opener.open(req,timeout=15) as response:owner = json.load(response)
    if not owner.get('id'):raise ValueError('Immich did not identify your account.')
    subprocess.run([str(executable),'--store-key-stdin',SERVICE,server],input=key.encode(),check=True)
    config = {'servers':servers,'userId':owner['id'],'keyAccount':server,
              'uploadKeyService':SERVICE,'stackKeyService':SERVICE,'pythonPath':sys.executable}
    temp = config_path.with_suffix('.tmp');temp.write_text(json.dumps(config,indent=2));temp.chmod(0o600);temp.replace(config_path)

def main():
    if sys.platform != 'darwin':raise ValueError('The desktop app requires macOS.')
    if sys.version_info < (3,10):raise ValueError('Python 3.10 or later is required.')
    os.umask(0o077)
    package = Path(__file__).resolve().parent.parent
    support = Path.home()/'Library/Application Support/Immich Card Sync'
    app = Path.home()/'Applications/Immich Card Sync.app'
    agent = Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    for p in [support/'state',support/'logs',app.parent,agent.parent]:p.mkdir(parents=True,exist_ok=True)
    with (support/'state/sync.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('A sync is running. Let it finish or stop it before installing.')
        source = package/'Immich Card Sync.app'
        if not source.is_dir():raise ValueError('Use the release ZIP, or run scripts/build.sh first.')
        subprocess.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],capture_output=True)
        # Stop the initial private build if upgrading on its original machine.
        subprocess.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/local.andz.immich-card-sync'],capture_output=True)
        if app.exists():app.rename(support/('previous-'+time.strftime('%Y%m%d-%H%M%S')+'.app'))
        shutil.copytree(source,app)
        config = support/'config.json'
        if not config.exists() or '--configure' in sys.argv:
            configure(app/'Contents/MacOS/Immich Card Sync',config)
        else:
            value=json.loads(config.read_text());value['pythonPath']=sys.executable
            config.write_text(json.dumps(value,indent=2));config.chmod(0o600)
        fcntl.flock(lock,fcntl.LOCK_UN)
        subprocess.run([sys.executable,str(app/'Contents/Resources/ingest.py'),'--config',str(config),'--state',str(support/'state'),'--probe'],check=True)
        value={'Label':LABEL,'ProgramArguments':[str(app/'Contents/MacOS/Immich Card Sync')],
               'RunAtLoad':True,'ProcessType':'Interactive','LimitLoadToSessionType':'Aqua','WorkingDirectory':str(support),
               'StandardOutPath':str(support/'logs/agent.log'),'StandardErrorPath':str(support/'logs/agent.log')}
        agent.write_bytes(plistlib.dumps(value));agent.chmod(0o644)
        subprocess.run(['/bin/launchctl','bootstrap',f'gui/{os.getuid()}',str(agent)],check=True)
    print('Installed. Insert a camera card to begin. The app will start at login.')

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print(f'Installation did not complete: {error}',file=sys.stderr);sys.exit(1)
