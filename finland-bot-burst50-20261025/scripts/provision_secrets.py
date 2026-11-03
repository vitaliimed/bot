#!/usr/bin/env python3
"""Secure one-time wiring of already verified CF and Haimaker API credentials.

Only writes Cloudflare token in Secret Manager if token has been fetched from user's
existing local diagnostic connector. Never exposes secret bytes on argv or in logs.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT='winged-ratio-377512'
CF_SECRET='finland-bot-cloudflare-workers-token'
HAI_SECRET='finland-bot-haimaker-api-key'

def run(*cmd,stdin=None,allowed=False):
    p=subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, timeout=75)
    if p.returncode and not allowed:
        raise RuntimeError(f'{cmd[0]} failed exit={p.returncode}: {p.stderr[:150]}')
    return p

def provision():
    if run('gcloud','secrets','describe',HAI_SECRET,'--project',PROJECT,allowed=True).returncode:
        raise RuntimeError('Haimaker secret absent. First complete previous Haimaker setup.')
    existing=run('gcloud','secrets','versions','list',CF_SECRET,'--project',PROJECT,
                 '--filter=state:ENABLED','--limit=1','--format=value(name)',allowed=True)
    if existing.returncode==0 and existing.stdout.strip():
        print('Existing Cloudflare token secret is available. Skipping token extraction.')
        return
    base=Path.home()/'projects'/'bot'
    audit_file=base/'finland_all_ai_test_20261025.py'
    setup=base/'finland-ai-connect-pack'/'finland-ai-connect-pack-20261025'/'finland_ai_setup_20261025.py'
    if not setup.exists() or not audit_file.exists():
        raise RuntimeError('Cloudflare token is not provisioned and previous diagnostic scripts are missing. Stop before deploy.')
    spec=importlib.util.spec_from_file_location('connect_setup',setup)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    audit=mod.load_audit()
    service=mod.json_command('gcloud','run','services','describe','finland-news-bot',
                             '--project',PROJECT,'--region','europe-north1','--format=json')
    token=mod.resolve_cf_key(audit,service)
    if not token or len(token)<15:raise RuntimeError('Existing Cloudflare token could not be read; stop')
    if run('gcloud','secrets','describe',CF_SECRET,'--project',PROJECT,allowed=True).returncode:
        run('gcloud','secrets','create',CF_SECRET,'--project',PROJECT,'--replication-policy=automatic')
    # Do not echo it; do not include it in the process argument list.
    run('gcloud','secrets','versions','add',CF_SECRET,'--project',PROJECT,'--data-file=-',stdin=token)
    token=''
    print('Cloudflare credential securely linked to Secret Manager, value not shown.')

if __name__=='__main__':
    try:provision()
    except Exception as e:
        print('STOP: credential preparation failed:',str(e)[:200]);sys.exit(2)
