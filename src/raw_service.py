#!/usr/bin/env python3
"""Bounded launchd invocation with private, rotating daily logs."""
import datetime
import fcntl
import os
from pathlib import Path
import subprocess
import sys

os.umask(0o077)
support = Path(__file__).resolve().parent.parent
logs = support / 'logs'
logs.mkdir(parents=True, exist_ok=True, mode=0o700)
for old in sorted(logs.glob('worker-*.log'), reverse=True)[29:]:
    old.unlink()
today = datetime.date.today().isoformat()
# A freshly bootstrapped service can start before the installer releases its upgrade lock.
with (support/'state/worker.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_SH)
    fcntl.flock(lock, fcntl.LOCK_UN)
with (logs / f'worker-{today}.log').open('ab') as stream:
    result = subprocess.run([sys.executable, str(support/'bin/raw_worker.py'),
        '--config', str(support/'config.json'), '--state', str(support/'state'), 'run'],
        stdout=stream, stderr=subprocess.STDOUT)
sys.exit(result.returncode)
