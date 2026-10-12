import unittest,tempfile,json,subprocess,sys
from pathlib import Path
from unittest.mock import patch
from render_status import write_status,validate

def record():
 return {'format':'UIE_RENDER_STATUS1','state':'rendering','updatedAt':'2026-09-29T20:00:00.000Z',
 'completedBatches':2,'totalBatches':5,'etaSeconds':30,'lastPublishedAt':'2026-09-26T10:00:00.000Z','message':'Pracuje'}

class StatusTests(unittest.TestCase):
 def test_bad_progress_does_not_replace_previous(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'state.json';old=record();write_status(p,old);before=p.read_bytes()
   for change in ({'completedBatches':6},{'etaSeconds':float('nan')},{'totalBatches':0},{'completedBatches':True},{'updatedAt':'2026-09-29'},{'message':'x'*301}):
    with self.assertRaises(ValueError):write_status(p,{**old,**change})
    self.assertEqual(before,p.read_bytes())
 def test_failed_replace_keeps_old_and_cleans_temporary(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'state.json';old=record();write_status(p,old)
   with patch('render_status.os.replace',side_effect=OSError('simulated')):
    with self.assertRaises(OSError):write_status(p,{**old,'completedBatches':3})
   self.assertEqual(json.loads(p.read_text()),old);self.assertEqual(list(Path(d).iterdir()),[p])
 def test_cli_preserves_last_publication_and_normalizes_times(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'state.json';data=record();data['lastPublishedAt']='2026-09-26 12:00:00+0200';write_status(p,data)
   self.assertEqual(json.loads(p.read_text())['lastPublishedAt'],'2026-09-26T10:00:00.000Z')
   subprocess.run([sys.executable,str(Path(__file__).with_name('render_status.py')),'--output',str(p),'--state','error','--message','worker stopped'],check=True)
   actual=json.loads(p.read_text());self.assertEqual(actual['lastPublishedAt'],'2026-09-26T10:00:00.000Z');self.assertEqual(actual['state'],'error')
   self.assertRegex(actual['updatedAt'],r'T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
 def test_utf16_limit_matches_browser(self):
  validate({**record(),'message':'😀'*150})
  with self.assertRaises(ValueError):validate({**record(),'message':'😀'*151})

if __name__=='__main__':unittest.main()
