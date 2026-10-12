"""Permanent invalid-input failures must survive restart and cannot be reclaimed."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from render_queue import RenderQueue, StaleClaimError

class PermanentFailureTests(unittest.TestCase):
    def test_permanent_failure_survives_reopen_and_idempotent_enqueue(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'queue.db';key='a'*64
            job={'id':hashlib.sha256(b'plan').hexdigest(),'payload':{'plan':'immutable'}}
            with RenderQueue(path,'uie','0',key,max_attempts=3) as q:
                q.enqueue([job]);claim=q.claim(1)
                self.assertEqual(q.fail(claim['id'],claim['token'],'identity mismatch',2,retry=False),'failed')
                with self.assertRaises(StaleClaimError):q.complete(claim['id'],claim['token'],{},now=3)
            with RenderQueue(path,'uie','0',key,max_attempts=3) as q:
                self.assertEqual(q.enqueue([job]),{'added':0,'existing':1})
                self.assertIsNone(q.claim(10000))
                saved=q.list_jobs()[0]
                self.assertEqual((saved['state'],saved['attempt'],saved['error']),('failed',1,'identity mismatch'))

    def test_invalid_retry_flag_preserves_running_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            with RenderQueue(Path(temp)/'queue.db','uie','0','a'*64) as q:
                q.enqueue([{'id':'b'*64,'payload':{}}]);claim=q.claim(1)
                for invalid in (0,1,None,'false'):
                    with self.assertRaises(ValueError):q.fail(claim['id'],claim['token'],'x',2,retry=invalid)
                self.assertEqual(q.stats()['running'],1)
                self.assertEqual(q.fail(claim['id'],claim['token'],'transient',3,retry_delay=5),'queued')
                self.assertIsNone(q.claim(7))
                self.assertEqual(q.claim(8)['attempt'],2)

if __name__=='__main__':unittest.main()
