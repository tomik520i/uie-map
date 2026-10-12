"""Independent review regressions: readers survive interrupted publication."""
import unittest
import cave_pair_history as history

class RecoveryTests(unittest.TestCase):
    def test_long_crash_then_different_publication_retires_actual_pointer_now(self):
        a,b,c,d=['%064x'%n for n in range(1,5)]
        geometry='f'*64
        def prep(old,pointer,next_sha,now):
            return history.prepare(old,pointer_sha=pointer,index_sha=next_sha,
                                   geometry_sha=geometry,now=now).doc
        first=prep(None,None,a,0)
        ahead=prep(first,a,b,100)  # history B is durable, cap pointer still A
        third=prep(ahead,a,c,10000)  # much later: publish C, retiring actual A
        fourth=prep(third,c,d,10001)
        self.assertIn(a,fourth['records'], 'a reader of actual A still needs retirement grace')
        self.assertGreaterEqual(fourth['records'][a]['publishedAt'],10000)

    def test_long_crash_then_retry_same_publication_retires_actual_pointer_now(self):
        a,b,c=['%064x'%n for n in range(1,4)]
        geometry='f'*64
        def prep(old,pointer,next_sha,now):
            return history.prepare(old,pointer_sha=pointer,index_sha=next_sha,
                                   geometry_sha=geometry,now=now).doc
        first=prep(None,None,a,0)
        ahead=prep(first,a,b,100)
        recovered=prep(ahead,a,b,10000)  # actual pointer can become B only now
        next_doc=prep(recovered,b,c,10001)
        self.assertIn(a,next_doc['records'])
        self.assertGreaterEqual(next_doc['records'][a]['publishedAt'],10000)

if __name__=='__main__':unittest.main()
