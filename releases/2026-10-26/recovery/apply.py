#!/usr/bin/env python3
"""Apply only the recovery module and one idempotent bootstrap to an existing source tree."""
import ast
import hashlib
import pathlib
import sys
from datetime import datetime,timezone

project=pathlib.Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()
main=project/'app/main.py'
if not main.is_file():raise SystemExit('Run with the existing project path containing app/main.py.')
original=main.read_text()
names={n.name for n in ast.walk(ast.parse(original)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
required={'prepare_backlog','prepare_item','publication_report','item_metadata','advanced_callback','_handle_message'}
if not required<=names:raise SystemExit('Unsupported source: missing '+','.join(sorted(required-names))+'. No files changed.')
for filename in ('dispatch.py','jobs.py','policy.py'):
 if not (project/'app'/filename).is_file():raise SystemExit('Unsupported project: app/'+filename+'. No files changed.')
module=(pathlib.Path(__file__).parent/'queue_recovery.py').read_text()
compile(module,'queue_recovery.py','exec')
marker='# QUEUE_RECOVERY_BOOTSTRAP'
code=original
if marker not in code:
 code+='\n'+marker+'\nimport sys as _recovery_sys\nfrom . import queue_recovery as _queue_recovery\n_queue_recovery.install(_recovery_sys.modules[__name__])\n'
compile(code,str(main),'exec')
stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
backup=pathlib.Path.home()/'bot-backups'/('source-recovery-'+stamp)
backup.mkdir(parents=True,mode=0o700)
(backup/'main.py').write_text(original)
old_module=project/'app/queue_recovery.py'
if old_module.exists():(backup/'queue_recovery.py').write_bytes(old_module.read_bytes())
old_module.write_text(module)
main.write_text(code)
print('Recovery applied. Existing AI providers, language patches, sources and settings preserved.')
print('Source backup: '+str(backup))
print('Recovery module SHA256: '+hashlib.sha256(module.encode()).hexdigest())
