import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import collision_world_watch as watch


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.stop=Mock();self.stop.is_set.return_value=False
        self.locks=[]
        def lock(out):
            value=Mock();self.locks.append(value);return value
        self.lock=lock;self.fn=Mock(return_value={'state':'complete'});self.emit=Mock()
        self.kw=dict(out=Path('out'),world=Path('world'),hires_store=Path('hires'),tools=object(),
                     stop=self.stop,emit=self.emit,pass_fn=self.fn,lock_fn=self.lock)

    def test_releases_lock_between_passes_and_keeps_same_cache(self):
        def wait(seconds):self.locks[-1].close.assert_called_once()
        self.stop.wait.side_effect=wait
        result=watch.run(**self.kw,passes=2)
        self.assertEqual(result,dict(state='finished',passes=2))
        self.assertIs(self.fn.call_args_list[0].kwargs['cache'],self.fn.call_args_list[1].kwargs['cache'])
        self.stop.wait.assert_called_once_with(900)
        for lock in self.locks:lock.close.assert_called_once()

    def test_busy_wait_does_not_run_or_consume_pass(self):
        real=Mock();self.kw['lock_fn']=Mock(side_effect=[None,real])
        result=watch.run(**self.kw)
        self.assertEqual(result['passes'],1);self.fn.assert_called_once()
        self.stop.wait.assert_called_once_with(60);real.close.assert_called_once()

    def test_budget_stops_immediately_releasing_lock(self):
        self.fn.return_value={'state':'stopped-budget'}
        self.assertEqual(watch.run(**self.kw,passes=0)['state'],'stopped-budget')
        self.stop.wait.assert_not_called();self.locks[0].close.assert_called_once()

    def test_partial_is_reported_and_next_pass_can_retry(self):
        self.fn.side_effect=[{'state':'partial','failedRegions':1},{'state':'complete'}]
        self.assertEqual(watch.run(**self.kw,passes=2)['passes'],2)
        self.assertEqual(self.emit.call_args_list[0].args[0]['result']['state'],'partial')

    def test_last_partial_is_not_reported_as_finished(self):
        self.fn.return_value={'state':'partial','failedRegions':1}
        self.assertEqual(watch.run(**self.kw),dict(state='partial',passes=1))

    def test_exception_and_interrupt_release_export_lock(self):
        for error in (ValueError('refused'),KeyboardInterrupt()):
            self.fn.side_effect=error
            with self.assertRaises(type(error)):watch.run(**self.kw)
            self.locks[-1].close.assert_called_once()

    def test_invalid_limits_touch_no_lock(self):
        for options in ({'passes':True},{'passes':-1},{'interval':0},{'budget_bytes':0},
                        {'min_free_bytes':1},{'allow_world_gc':'yes'}):
            with self.subTest(options=options),self.assertRaises(ValueError):watch.run(**self.kw,**options)
        self.assertEqual(self.locks,[])

    def test_stop_before_first_pass_does_nothing(self):
        self.stop.is_set.return_value=True
        self.assertEqual(watch.run(**self.kw),dict(state='stopped',passes=0));self.fn.assert_not_called()

    @unittest.skipUnless(os.name=='posix','real POSIX locks')
    def test_real_coordinator_lock_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fd=watch.acquire_coordinator(root)
            try:self.assertIsNone(watch.acquire_coordinator(root))
            finally:os.close(fd)
            fd=watch.acquire_coordinator(root);os.close(fd)
            (root/'.world-watch.lock').unlink();(root/'target').write_text('keep')
            (root/'.world-watch.lock').symlink_to(root/'target')
            with self.assertRaises(OSError):watch.acquire_coordinator(root)
            self.assertEqual((root/'target').read_text(),'keep')


if __name__=='__main__':unittest.main()
