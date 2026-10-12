import unittest
import hires_schedule as schedule

class ReviewTests(unittest.TestCase):
    def test_successful_job_becomes_new_pending_once_not_every_selection(self):
        first={'job':[0,0],'kind':'changed','distance':0}
        def select(items,ledger,now,free=100):
            return schedule.select(items,ledger,now=now,max_jobs=1,free_bytes=free,
                floor_bytes=0,reserve_bytes=0,job_reserve_bytes=1)
        _,ledger,_=select([first],None,1)
        ledger=schedule.finish(ledger,[0,0],ok=True,now=2)
        # Changed again, but unable to dispatch while disk is unavailable.
        _,ledger,_=select([first],ledger,10,free=0)
        self.assertEqual(ledger['regions']['0,0']['pendingSince'],10)
        newcomer={'job':[1,0],'kind':'changed','distance':0}
        _,ledger,_=select([first,newcomer],ledger,20,free=0)
        self.assertEqual(ledger['regions']['0,0']['pendingSince'],10)
        chosen,_,_=select([first,newcomer],ledger,30)
        self.assertEqual(chosen[0]['job'],[0,0])

if __name__=='__main__':unittest.main()
