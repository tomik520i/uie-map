import os
import unittest
from unittest.mock import patch
import cave_full_render as full
import test_cave_full_render as fixture


@unittest.skipUnless(os.name=='posix','POSIX supervisor')
class LimitsTests(unittest.TestCase):
    setUp=fixture.CoverageTests.setUp
    write_geometry=fixture.CoverageTests.write_geometry
    write_job=fixture.CoverageTests.write_job

    def test_explicit_limits_reach_renderer_and_status(self):
        commands=[]
        def render(out,step,command,**kw):commands.append(command);return {'fake':True}
        def batch(*args,**kw):kw['render'](1);return {'state':'completed'}
        with patch.object(full,'coverage',side_effect=[{'totalRegions':1,'remainingRegions':1},
                                                       {'totalRegions':1,'remainingRegions':0}]), \
             patch.object(full,'maintain',return_value=[]),patch.object(full.batch,'run_batch',side_effect=batch), \
             patch.object(full.batch,'render_subprocess',side_effect=render):
            result=full.run(self.out,self.out,self.out,store_budget_gib=32,expansion_reserve_gib=80)
        self.assertEqual(result['state'],'completed')
        self.assertEqual((result['storeBudgetGiB'],result['expansionReserveGiB']),(32,80))
        self.assertEqual(commands[0][-4:],['--cave-store-budget-mib','32768','--cave-reserve-gib','80'])

    def test_invalid_limits_rejected_before_store_access(self):
        for kwargs in ({'store_budget_gib':0},{'store_budget_gib':True},{'store_budget_gib':8193},
                       {'expansion_reserve_gib':19},{'expansion_reserve_gib':False},
                       {'expansion_reserve_gib':24.0},{'expansion_reserve_gib':8193}):
            with self.subTest(kwargs),patch.object(full.batch,'check_store',side_effect=AssertionError('store accessed')):
                with self.assertRaises(ValueError):full.run(self.out,self.out,self.out,**kwargs)

if __name__=='__main__':unittest.main()
