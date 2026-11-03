import unittest, tempfile, json
from pathlib import Path
from unittest.mock import patch
import importlib.util
s=importlib.util.spec_from_file_location('fsetup','/mnt/data/finland_ai_setup_20261025.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)

class OfflineTests(unittest.TestCase):
 def test_ukrainian_gate(self):
  self.assertTrue(m.ukrainian('31 жовтня о 12:00 у Києві відбудеться сімейна подія, захід безкоштовний.'))
  self.assertFalse(m.ukrainian('31. lokakuuta klo 12. Helsingissä on tapahtuma.'))
  self.assertFalse(m.ukrainian('Сімейна подія в Україні.'))
 def test_haimaker_endpoint_and_model(self):
  called=[]
  def fake(url,key,body,**kwargs):
   called.append((url,body));return {'choices':[{'message':{'content':'31 жовтня 2026 року о 12:00 відбудеться безкоштовна сімейна подія в Гельсінкі.'}}]}
  with patch.object(m,'http_post',fake):
   v=m.test_haimaker('fake_key_for_test_only','z-ai/glm-5.3')
  self.assertTrue(v['pass']);self.assertEqual(called[0][0],'https://api.haimaker.ai/v1/chat/completions');self.assertEqual(called[0][1]['model'],'z-ai/glm-5.3')
 def test_cf_image(self):
  import base64
  data=b'\x89PNG\r\n\x1a\n'+b'A'*4096
  with tempfile.TemporaryDirectory() as temp:
   with patch.object(m,'http_post',return_value={'success':True,'result':{'image':base64.b64encode(data).decode()}}):
    result=m.test_cf_image('no_real_secret','b1485cf2910a3ec64d102525b7ec51a6',Path(temp))
    self.assertTrue(result['pass']);self.assertTrue(Path(result['file']).exists())
 def test_secret_never_cli_arguments(self):
  calls=[]
  def fake(*args,**kwargs):
   calls.append((args,kwargs));
   class R: returncode=0;stdout='';stderr=''
   return R()
  with patch.object(m,'shell',fake):m.store_hai_secret('sk-UNITTESTFAKEKEY_123456789012345')
  self.assertTrue(any('versions' in args for args,kw in calls))
  self.assertTrue(all('sk-UNITTESTFAKE' not in ' '.join(args) for args,kw in calls))
  self.assertTrue(any(kw.get('stdin','').startswith('sk-UNIT') for args,kw in calls))

if __name__=='__main__':unittest.main()
