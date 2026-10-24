import json,subprocess,unittest,sys
from pathlib import Path
import test_uiemap_serve as fixtures
import uiemap_update as update


class UpdateTests(unittest.TestCase):
    setUp=fixtures.WebConfigTests.setUp
    index=fixtures.WebConfigTests.index

    def work(self):
        for name in ('world/region','world/DIM42/region'):(self.root/name/'r.0.0.mca').write_bytes(b'\0'*8192)
        return update.plan(self.file)

    def reply(self,job):
        return subprocess.CompletedProcess([],0,json.dumps(dict(format='UIEMAP_RENDER_RESULT1',world='mine',
           dimension=job['dimension'],out=job['out'],published=True,indexSha256='a'*64)), '')

    def test_sequential_pass_requires_publication_receipts(self):
        work=self.work();seen=[]
        def run(command,**kwargs):
            job=work['jobs'][len(seen)];seen.append(job['dimension'])
            self.assertIn(work['configSha256'],command)
            return self.reply(job)
        result=update.execute(work,run)
        self.assertTrue(result['complete']);self.assertEqual(seen,['0','42'])

    def test_failure_or_missing_receipt_stops_remaining_dimensions(self):
        work=self.work()
        for reply in (subprocess.CompletedProcess([],1,'','failed'),subprocess.CompletedProcess([],0,'{}','')):
            calls=[]
            def run(*args,**kwargs):calls.append(args);return reply
            result=update.execute(work,run)
            self.assertFalse(result['complete']);self.assertEqual(result['pending'],['42']);self.assertEqual(len(calls),1)

    def test_configuration_change_stops_between_dimensions(self):
        work=self.work();calls=[]
        def run(*args,**kwargs):
            calls.append(args);self.file.write_text(self.file.read_text()+' ')
            return self.reply(work['jobs'][0])
        with self.assertRaisesRegex(ValueError,'changed'):update.execute(work,run)
        self.assertEqual(len(calls),1)

    def test_unsupported_dimension_rejected_before_execution(self):
        self.work();self.doc['dimensions'][1]['mode']='caves';self.file.write_text(json.dumps(self.doc))
        with self.assertRaisesRegex(ValueError,'cave'):update.plan(self.file)

    def test_child_refuses_changed_configuration_before_start(self):
        self.work()
        run=subprocess.run([sys.executable,'-B',str(Path(update.__file__).with_name('uiemap_render.py')),
            str(self.file),'--dimension','0','--apply','--expected-config-sha','0'*64],capture_output=True,text=True)
        self.assertEqual(run.returncode,2);self.assertIn('changed after update planning',run.stderr)


if __name__=='__main__':unittest.main()
