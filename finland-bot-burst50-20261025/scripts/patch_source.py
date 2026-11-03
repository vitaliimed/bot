#!/usr/bin/env python3
"""Patch a verified live-source copy, never the user's repository."""
from __future__ import annotations
import argparse
import ast
from pathlib import Path
import shutil
import sys

SIGNATURE='Finland burst50 addon v1, 2026-10-25'
PACKAGE=Path(__file__).resolve().parent.parent

def patch(root):
    root=Path(root).resolve()
    main=root/'app'/'main.py'; target=root/'app'/'burst_ai.py'
    if not main.is_file(): raise RuntimeError('Missing app/main.py')
    tree=ast.parse(main.read_text(encoding='utf-8'))
    fn={x.name for x in tree.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef))}
    required={'prepare_image','publish','handle_message','editor_config','say','admin_access'}
    missing=required-fn
    if missing:raise RuntimeError('Incompatible deployed revision: missing '+', '.join(sorted(missing)))
    if not any(isinstance(x,ast.Assign) and any(getattr(t,'id',None)=='app' for t in x.targets) for x in tree.body):
        # FastAPI main may instead set `app` from another module; assume named app only on validated AST.
        if 'app' not in main.read_text(encoding='utf-8'):
            raise RuntimeError('FastAPI app is missing')
    content=main.read_text(encoding='utf-8')
    if SIGNATURE in content:raise RuntimeError('This add-on is already installed')
    if not (root/'requirements.txt').exists():raise RuntimeError('Missing requirements.txt')
    shutil.copy2(PACKAGE/'app'/'burst_ai.py',target)
    content += ('\n# '+SIGNATURE+'\nfrom app import burst_ai as _burst50_addon\n'
                '_burst50_addon.install(globals())\n')
    ast.parse(content)
    main.write_text(content,encoding='utf-8')
    requirements=root/'requirements.txt'
    value=requirements.read_text(encoding='utf-8')
    if not any(x.strip().startswith('google-auth') for x in value.splitlines()):
        value+='\ngoogle-auth>=2.30,<3\n'
    requirements.write_text(value,encoding='utf-8')
    print('PATCH READY: original main preserved except for final two-line registration')
    print('PATCH READY: Cloudflare and Haimaker runtime integration staged (not deployed)')
    return True

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('source_dir');ns=ap.parse_args()
    try:patch(ns.source_dir)
    except Exception as e:print('STOP:',e);sys.exit(2)
