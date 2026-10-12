import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import model_candidate_run as mr

class WorkerBindingsTests(unittest.TestCase):
 def bindings(self,workers=1):
  return dict(out='/tmp/out',world='/tmp/world',catalog='/tmp/catalog',worldHires=str(mr.WORLD_HIRES),
   codeSha256='a'*64,command=mr.renderer_command('/tmp/out','/tmp/world','/tmp/catalog',workers))
 def test_only_canonical_worker_adjustment_is_accepted(self):
  self.assertTrue(mr.worker_change_only(self.bindings(1),self.bindings(4)))
  self.assertTrue(mr.worker_change_only(self.bindings(4),self.bindings(1)))
  for field in ['out','world','catalog','worldHires','codeSha256']:
   b=self.bindings(4);b[field]+='changed'
   self.assertFalse(mr.worker_change_only(self.bindings(),b),field)
  for mutation in [lambda c:c+['--caves'],lambda c:c+['--workers','2'],
    lambda c:c[:-1],lambda c:['other-python']+c[1:],
    lambda c:[x.replace('world_hires.py','other.py') for x in c]]:
   b=self.bindings(4);b['command']=mutation(b['command'])
   self.assertFalse(mr.worker_change_only(self.bindings(),b))
  for n in [0,5,True,'04','four']:
   b=self.bindings();b['command'][b['command'].index('--workers')+1]=str(n)
   self.assertFalse(mr.worker_change_only(b,self.bindings(4)))
 def test_change_requires_explicit_flag_and_still_checks_published_store_and_live_group(self):
  with tempfile.TemporaryDirectory() as tmp:
   out=Path(tmp);previous={'bindings':self.bindings(),'rendererPid':123}
   with patch.object(mr,'_group_alive',return_value=False):
    with self.assertRaisesRegex(mr.CandidateError,'bindings'):
     mr.check_resume(out,previous,self.bindings(4))
    mr.check_resume(out,previous,self.bindings(4),allow_worker_change=True)
    (out/'current.json').write_text('{}')
    with self.assertRaisesRegex(mr.CandidateError,'published'):
     mr.check_resume(out,previous,self.bindings(4),allow_worker_change=True)
    (out/'current.json').unlink()
    (out/'cave-data').mkdir()
    with self.assertRaisesRegex(mr.CandidateError,'cave-data'):
     mr.check_resume(out,previous,self.bindings(4),allow_worker_change=True)
    (out/'cave-data').rmdir()
   with patch.object(mr,'_group_alive',return_value=True):
    with self.assertRaisesRegex(mr.CandidateError,'still alive'):
     mr.check_resume(out,previous,self.bindings(4),allow_worker_change=True)
 def test_invalid_worker_or_non_resume_adjustment_is_refused_before_filesystem_access(self):
  with patch.object(mr.sys,'platform','linux'):
   for workers in [0,5,True,1.5,'2']:
    with self.assertRaisesRegex(ValueError,'workers'):
     mr.run('/does-not-exist','/world','/catalog',workers=workers)
   with self.assertRaisesRegex(ValueError,'explicit resume'):
    mr.run('/does-not-exist','/world','/catalog',allow_worker_change=True)
if __name__=='__main__':unittest.main()

