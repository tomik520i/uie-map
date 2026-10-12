"""Independent integration regressions discovered during batch review."""
import tempfile,unittest
from pathlib import Path
import cave_batch as batch
from test_cave_batch import render_result,PUBLISHED
import sys,os,signal,json
import test_cave_pair_publish as fixture

class ReviewTests(unittest.TestCase):
    def test_interruption_records_error_from_render_or_publish(self):
        def interrupted(*args):raise batch.BatchInterrupted('SIGTERM')
        for phase in ('render','publish'):
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(batch.BatchInterrupted):
                    batch.controller(Path(folder),max_steps=2,
                        render=interrupted if phase=='render' else lambda step:render_result(),
                        publish=interrupted,session=object())
                status=json.loads((Path(folder)/batch.STATUS_NAME).read_bytes())
                self.assertEqual(status['state'],'error');self.assertIsNone(status['phase'])
                self.assertIn('SIGTERM',status['reason'])
    def test_verification_warmup_continues_with_same_session(self):
        with tempfile.TemporaryDirectory() as folder:
            records=iter([{'state':'verifying','published':False},PUBLISHED])
            session=object();calls=[]
            def publish(actual):
                self.assertIs(actual,session);calls.append(actual);return next(records)
            result=batch.controller(Path(folder),max_steps=1,render=lambda step:render_result(),
                                    publish=publish,session=session,sleep=lambda seconds:None)
            self.assertEqual(result['state'],'completed')
            self.assertEqual(result['completedSteps'],1)
            self.assertEqual(len(calls),2)

@unittest.skipUnless(sys.platform.startswith('linux'),'POSIX batch lock')
class ContextTests(unittest.TestCase):
    setUp=fixture.PublishTests.setUp
    write_geometry=fixture.PublishTests.write_geometry
    write_job=fixture.PublishTests.write_job
    def test_sigterm_becomes_cleanup_exception(self):
        old=signal.signal(signal.SIGTERM,batch._terminate)
        try:
            with self.assertRaises(batch.BatchInterrupted):os.kill(os.getpid(),signal.SIGTERM)
        finally:signal.signal(signal.SIGTERM,old)
    def test_wrong_world_refused_before_renderer_or_status_write(self):
        with self.assertRaisesRegex(batch.BatchError,'world/dimension'):
            batch.run_batch(self.out,world=self.out,catalog_dir=self.out,world_id='another-world',
                            dimension_id='0',render=lambda step:self.fail('must not render'),
                            publish=lambda session:self.fail('must not publish'))
        self.assertFalse((self.out/batch.STATUS_NAME).exists())

if __name__=='__main__':unittest.main()
