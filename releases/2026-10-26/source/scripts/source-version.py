"""Stable deployment version for uploaded archives; no Git dependency."""
from pathlib import Path
import hashlib
root=Path(__file__).resolve().parents[1]
h=hashlib.sha256()
paths=[root/'Dockerfile',root/'requirements.txt']+sorted((root/'app').rglob('*.py'))+[root/'app/catalog.json']
for path in paths:
    h.update(str(path.relative_to(root)).encode());h.update(b'\0');h.update(path.read_bytes());h.update(b'\0')
print('v2-'+h.hexdigest()[:12])
