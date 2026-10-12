import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import cave_run_budget as budget
import hires_bundle as hb

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name).resolve();(self.out/hb.OWNED_MARKER).write_bytes(b'')
        self.patch=patch.object(budget.shutil,'disk_usage',return_value=SimpleNamespace(free=30*1024**3))
        self.patch.start();self.addCleanup(self.patch.stop)
    def run_budget(self,**kwargs):
        return budget.inspect_budget(self.out,max_store_bytes=kwargs.pop('max_store_bytes',2*1024**3),**kwargs)
    def test_hardlinks_counted_once_and_no_writes(self):
        data=self.out/'data';data.write_bytes(b'x'*10000);os.link(data,self.out/'alias')
        before={p.name:p.stat().st_mtime_ns for p in self.out.iterdir()}
        result=self.run_budget()
        self.assertEqual(result['logicalUniqueBytes'],10000);self.assertEqual(result['state'],'ready')
        self.assertEqual(before,{p.name:p.stat().st_mtime_ns for p in self.out.iterdir()})
    def test_store_reserve_blocks_before_limit_reached(self):
        (self.out/'data').write_bytes(b'x'*10000)
        result=self.run_budget(max_store_bytes=100000,reserve_bytes=100000)
        self.assertEqual(result['reasons'],['store-budget'])
    def test_floor_reserve_equality_and_shortfall(self):
        reserve=budget.DEFAULT_RESERVE_BYTES
        with patch.object(budget.shutil,'disk_usage',return_value=SimpleNamespace(free=budget.MIN_FREE_BYTES+reserve)):
            self.assertEqual(self.run_budget()['state'],'ready')
        with patch.object(budget.shutil,'disk_usage',return_value=SimpleNamespace(free=budget.MIN_FREE_BYTES+reserve-1)):
            self.assertEqual(self.run_budget()['reasons'],['free-disk-floor'])
    def test_symlink_and_capacity_refused(self):
        data=self.out/'data';data.write_bytes(b'x')
        with patch.object(budget,'MAX_ENTRIES',1):
            with self.assertRaisesRegex(ValueError,'capacity'):self.run_budget()
        try:(self.out/'link').symlink_to(data)
        except OSError as e:self.skipTest(str(e))
        with self.assertRaisesRegex(ValueError,'symlink'):self.run_budget()
    def test_invalid_options(self):
        for kwargs in [{'max_store_bytes':True},{'reserve_bytes':0},{'reserve_bytes':-1}]:
            with self.assertRaises(ValueError):self.run_budget(**kwargs)
    def test_entry_headroom_blocks_before_retention_scan_limit(self):
        with patch.object(budget,'MAX_ENTRIES',10),patch.object(budget,'ENTRY_RESERVE',9):
            self.assertEqual(self.run_budget()['state'],'ready')  # only owned marker
            (self.out/'next').write_bytes(b'x')
            result=self.run_budget()
            self.assertEqual(result['reasons'],['entry-budget'])
            self.assertLess(result['scanned'],result['maxEntries'])

if __name__=='__main__':unittest.main()
