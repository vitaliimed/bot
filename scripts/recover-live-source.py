#!/usr/bin/env python3
"""Recover source of the revision receiving 100% traffic, without changing service/data."""
import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import zipfile

REQUIRED = {'_handle_message', 'advanced_callback', 'item_metadata', 'prepare_backlog', 'prepare_item', 'publication_report'}

def supported(root):
    try:
        tree = ast.parse((Path(root) / 'app/main.py').read_text())
    except (OSError, SyntaxError, UnicodeError):
        return False
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return REQUIRED <= names and all((Path(root) / 'app' / n).is_file() for n in ('dispatch.py', 'jobs.py', 'policy.py'))

def run(*args, timeout=90):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Do not echo stderr: providers/CLI may include environment values.
        raise RuntimeError('Command failed: ' + ' '.join(args[:3]) + '. Check account permissions.')
    return result.stdout.strip()

def read(*args):
    return json.loads(run('gcloud', *args, '--format=json'))

def digest(value):
    value = str(value or '').rsplit('@', 1)[-1]
    return value if re.fullmatch(r'sha256:[a-fA-F0-9]{64}', value) else ''

def live_revision(service):
    active = [t for t in service.get('status', {}).get('traffic', []) if t.get('percent', 0) > 0]
    if len(active) != 1 or active[0]['percent'] != 100 or not active[0].get('revisionName'):
        raise RuntimeError('Cannot select source: traffic is split or revision is unknown.')
    return active[0]['revisionName']

def immutable_source(build):
    choices = [build.get('sourceProvenance', {}).get('resolvedStorageSource', {}),
               build.get('source', {}).get('storageSource', {})]
    for src in choices:
        if src.get('bucket') and src.get('object') and re.fullmatch(r'[0-9]+', str(src.get('generation', ''))):
            return f"gs://{src['bucket']}/{src['object']}#{src['generation']}"
    return ''

def safe_extract(archive, target):
    def safe(name):
        path = Path(name)
        return not path.is_absolute() and '..' not in path.parts and '\\' not in name
    total = 0
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as src:
            members = src.getmembers()
            for entry in members:
                total += entry.size
                if not safe(entry.name) or not (entry.isfile() or entry.isdir()):
                    raise RuntimeError('Unsafe archive entry; no extraction performed.')
            if total > 512 * 1024 * 1024 or len(members) > 20000:
                raise RuntimeError('Source archive is unexpectedly large.')
            # Extract regular files ourselves; no links, owners or privileged modes.
            for entry in members:
                dest = target / entry.name
                if entry.isdir(): dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with src.extractfile(entry) as source, dest.open('wb') as output:
                        import shutil
                        shutil.copyfileobj(source, output)
                    dest.chmod(0o700 if entry.mode & 0o111 else 0o600)
    elif zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as src:
            members = src.infolist()
            for entry in members:
                total += entry.file_size
                if not safe(entry.filename) or ((entry.external_attr >> 16) & 0o170000) not in (0, 0o100000, 0o040000):
                    raise RuntimeError('Unsafe ZIP entry; no extraction performed.')
            if total > 512 * 1024 * 1024 or len(members) > 20000:
                raise RuntimeError('Source archive is unexpectedly large.')
            src.extractall(target)
    else:
        raise RuntimeError('Unsupported source archive format.')

def recover(project, region, service_name, destination):
    service = read('run', 'services', 'describe', service_name, '--project='+project, '--region='+region)
    revision = live_revision(service)
    revision_data = read('run', 'revisions', 'describe', revision, '--project='+project, '--region='+region)
    image = digest(revision_data.get('status', {}).get('imageDigest'))
    if not image:
        image = next((digest(c.get('image')) for c in revision_data.get('spec', {}).get('containers', []) if digest(c.get('image'))), '')
    if not image:
        raise RuntimeError('Live image digest is unavailable; refusing to guess source.')
    print('Live revision:', revision)
    print('Live image digest:', image)
    matches = {}
    for location in dict.fromkeys((region, 'global')):
        try:
            builds = read('builds', 'list', '--project='+project, '--region='+location,
                          '--filter=status=SUCCESS', '--sort-by=~createTime', '--limit=100')
        except RuntimeError:
            print('Build listing unavailable in:', location)
            continue
        for build in builds:
            if any(digest(x.get('digest')) == image for x in build.get('results', {}).get('images', [])):
                # Fetch complete provenance rather than relying on abbreviated list output.
                detail = read('builds', 'describe', build['id'], '--project='+project, '--region='+location)
                uri = immutable_source(detail)
                if uri: matches[(build['id'], uri)] = detail
    if len(matches) != 1:
        raise RuntimeError('Cannot prove one immutable source archive for the live image. No source replaced or deployed.')
    (build_id, uri), _ = next(iter(matches.items()))
    destination.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='bot-live-', dir=destination))
    work.chmod(0o700)
    archive = work / 'build-source.archive'
    run('gcloud', 'storage', 'cp', uri, str(archive), timeout=180)
    unpack = work / 'source'; unpack.mkdir(mode=0o700)
    safe_extract(archive, unpack)
    roots = [p.parent.parent for p in unpack.rglob('app/main.py')]
    # Build archives can contain nested old releases: prefer the actual build root.
    root = unpack if (unpack/'app/main.py').is_file() else roots[0] if len(roots)==1 else None
    if root is None or not supported(root):
        raise RuntimeError('Live source also lacks v2 queue functions. Recovered source retained at '+str(unpack)+'. No deployment performed.')
    if not (root/'Dockerfile').is_file() or not (root/'requirements.txt').is_file():
        raise RuntimeError('Recovered source is incomplete; no deployment performed.')
    current = read('run', 'services', 'describe', service_name, '--project='+project, '--region='+region)
    if live_revision(current) != revision:
        raise RuntimeError('Traffic changed while recovering source. Rerun updater.')
    (work/'provenance.json').write_text(json.dumps({'revision':revision,'image_digest':image,'build_id':build_id,'storage_source':uri,'root':str(root)},indent=2))
    print('Source matched to live image; build:', build_id)
    return root

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check')
    parser.add_argument('--project', default='winged-ratio-377512')
    parser.add_argument('--region', default='europe-north1')
    parser.add_argument('--service', default='finland-news-bot')
    parser.add_argument('--output-file')
    args = parser.parse_args()
    if args.check:
        return 0 if supported(args.check) else 3
    if not args.output_file:
        parser.error('--output-file is required')
    root = recover(args.project,args.region,args.service,Path.home()/'projects')
    Path(args.output_file).write_text(str(root)+'\n')
    print('Recovered source:', root)
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print('STOP:', str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        raise SystemExit(1)
