import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('recover',Path(__file__).resolve().parents[1]/'scripts/recover-live-source.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class RecoverySourceTests(unittest.TestCase):
    def source(self,root):
        (root/'app').mkdir(parents=True)
        (root/'app/main.py').write_text('\n'.join('async def '+name+'(): pass' for name in m.REQUIRED))
        for name in ('dispatch.py','jobs.py','policy.py'):(root/'app'/name).write_text('')
        (root/'Dockerfile').write_text('FROM python:3.12-slim\n')
        (root/'requirements.txt').write_text('')
    def test_old_source_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'app').mkdir();(root/'app/main.py').write_text('async def prepare_item(): pass')
            self.assertFalse(m.supported(root))
    def test_v2_source_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            self.source(Path(d));self.assertTrue(m.supported(d))
    def test_split_traffic_rejected(self):
        with self.assertRaises(RuntimeError):m.live_revision({'status':{'traffic':[{'percent':50,'revisionName':'a'},{'percent':50,'revisionName':'b'}]}})
    def test_uses_traffic_not_latest_revision(self):
        self.assertEqual(m.live_revision({'status':{'latestReadyRevisionName':'candidate','traffic':[{'percent':100,'revisionName':'live'},{'tag':'candidate','revisionName':'candidate'}]}}),'live')
    def test_resolved_generation_preferred(self):
        build={'source':{'storageSource':{'bucket':'wrong','object':'a','generation':'1'}},'sourceProvenance':{'resolvedStorageSource':{'bucket':'actual','object':'b','generation':'2'}}}
        self.assertEqual(m.immutable_source(build),'gs://actual/b#2')
    def test_mutable_source_rejected(self):
        self.assertEqual(m.immutable_source({'source':{'storageSource':{'bucket':'bucket','object':'file'}}}),'')
    def test_invalid_digest_rejected(self):
        self.assertFalse(m.digest('sha256:'+'z'*64))
        self.assertEqual(m.digest('registry/image@sha256:'+'a'*64),'sha256:'+'a'*64)
    def test_unsafe_archive_does_not_extract(self):
        for name,kind in (('../escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE)):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as d:
                root=Path(d);archive=root/'source.tar'
                with tarfile.open(archive,'w') as out:
                    info=tarfile.TarInfo(name);info.type=kind;info.linkname='/etc/passwd'
                    out.addfile(info,io.BytesIO(b''))
                target=root/'dest';target.mkdir()
                with self.assertRaises(RuntimeError):m.safe_extract(archive,target)
                self.assertEqual(list(target.iterdir()),[])
    def test_safe_archive_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.source(root/'original')
            with tarfile.open(root/'source.tar','w') as out:out.add(root/'original',arcname='.')
            target=root/'dest';target.mkdir();m.safe_extract(root/'source.tar',target)
            self.assertTrue(m.supported(target))
    def test_recovery_matches_live_digest_and_generation(self):
        self.exercise_recovery(False)
    def test_changed_traffic_aborts(self):
        self.exercise_recovery(True)
    def exercise_recovery(self,change):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.source(root/'original');image='sha256:'+'a'*64
            service={'status':{'traffic':[{'percent':100,'revisionName':'live'}]}}
            build={'id':'build','results':{'images':[{'digest':image}]},'sourceProvenance':{'resolvedStorageSource':{'bucket':'bucket','object':'source','generation':'123'}}}
            reads=[];calls=[]
            def read(*args):
                reads.append(args)
                if args[:3]==('run','services','describe'):
                    n=sum(x[:3]==('run','services','describe') for x in reads)
                    return {'status':{'traffic':[{'percent':100,'revisionName':'changed'}]}} if change and n>1 else service
                if args[:3]==('run','revisions','describe'):return {'status':{'imageDigest':image}}
                if args[:2]==('builds','list'):return [build] if '--region=europe-north1' in args else []
                if args[:2]==('builds','describe'):return build
                raise AssertionError(args)
            def run(*args,**kwargs):
                calls.append(args)
                with tarfile.open(args[-1],'w') as out:out.add(root/'original',arcname='.')
                return ''
            with patch.object(m,'read',side_effect=read),patch.object(m,'run',side_effect=run):
                if change:
                    with self.assertRaisesRegex(RuntimeError,'Traffic changed'):m.recover('project','europe-north1','service',root/'recovered')
                else:
                    result=m.recover('project','europe-north1','service',root/'recovered')
                    self.assertTrue(m.supported(result))
                    self.assertEqual(json.loads((result.parent/'provenance.json').read_text())['revision'],'live')
            self.assertEqual(calls[0][3],'gs://bucket/source#123')
            self.assertTrue(m.supported(root/'original'))

if __name__=='__main__':unittest.main()
