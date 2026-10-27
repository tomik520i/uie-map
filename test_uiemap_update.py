import json,subprocess,unittest,sys
from pathlib import Path
from unittest.mock import patch
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
        self.work();(self.root/'world/DIM-1/region').mkdir(parents=True)
        (self.root/'world/DIM-1/region/r.0.0.mca').write_bytes(b'\0'*8192)
        self.doc['dimensions'][1]['key']='-1';self.file.write_text(json.dumps(self.doc))
        with self.assertRaisesRegex(ValueError,'Nether'):update.plan(self.file)

    def test_cave_gc_rejected_before_any_renderer(self):
        self.work();self.doc['dimensions'][1]['mode']='caves';self.file.write_text(json.dumps(self.doc))
        work=update.plan(self.file)
        with patch.object(update.subprocess,'run') as run,self.assertRaisesRegex(ValueError,'surface'):
            update.execute(work,cleanup=True)
        run.assert_not_called()

    def test_cave_update_refuses_geometry_only_receipt(self):
        self.work();self.doc['dimensions']=[dict(key='42',title='Cave',mode='caves')]
        self.file.write_text(json.dumps(self.doc));work=update.plan(self.file)
        self.assertFalse(update.execute(work,lambda *a,**k:self.reply(work['jobs'][0]))['complete'])
        reply=self.reply(work['jobs'][0]);doc=json.loads(reply.stdout)
        doc.update(mode='caves',capIndexSha256='b'*64);reply.stdout=json.dumps(doc)
        self.assertTrue(update.execute(work,lambda *a,**k:reply)['complete'])

    def test_child_refuses_changed_configuration_before_start(self):
        self.work()
        run=subprocess.run([sys.executable,'-B',str(Path(update.__file__).with_name('uiemap_render.py')),
            str(self.file),'--dimension','0','--apply','--expected-config-sha','0'*64],capture_output=True,text=True)
        self.assertEqual(run.returncode,2);self.assertIn('changed after update planning',run.stderr)

    def test_cleanup_is_opt_in_and_follows_successful_publication(self):
        work=self.work()
        def run(command,**kwargs):
            key=command[command.index('--dimension')+1]
            return self.reply(next(job for job in work['jobs'] if job['dimension']==key))
        with patch.object(update.uiemap_gc,'collect',return_value={'collection':{'state':'executed','removed':0}}) as gc:
            self.assertTrue(update.execute(work,run)['complete']);gc.assert_not_called()
            self.assertTrue(update.execute(work,run,cleanup=True)['complete'])
            self.assertEqual(gc.call_count,2)
            self.assertEqual(gc.call_args.kwargs['expected_config_sha'],work['configSha256'])
        with patch.object(update.uiemap_gc,'collect') as gc:
            update.execute(work,lambda *a,**k:subprocess.CompletedProcess([],1,'','failure'),cleanup=True)
            gc.assert_not_called()

    def test_cleanup_failure_keeps_publication_receipt(self):
        work=self.work()
        with patch.object(update.uiemap_gc,'collect',side_effect=ValueError('bad store')):
            result=update.execute(work,lambda *a,**k:self.reply(work['jobs'][0]),cleanup=True)
        self.assertFalse(result['complete']);self.assertTrue(result['results'][0]['published'])
        self.assertEqual(result['pending'],['42'])

    def test_collision_updates_follow_surface_and_stop_on_bad_receipt(self):
        work=self.work();seen=[]
        def run(command,**kwargs):
            key=command[command.index('--dimension')+1];job=next(j for j in work['jobs'] if j['dimension']==key)
            is_collision=command[2].endswith('uiemap_collisions.py');seen.append((key,is_collision))
            self.assertIn(work['configSha256'],command)
            if not is_collision:return self.reply(job)
            row=next(r for r in update.uiemap_config.load(self.file)['dimensions'] if r['key']==key)
            out=row['outputs']['collisions']
            return subprocess.CompletedProcess([],0,json.dumps(dict(format='UIEMAP_COLLISION_RESULT1',world='mine',dimension=key,
                out=out,store=str(Path(out)/'store'),result=dict(state='complete'))),'')
        self.assertTrue(update.execute(work,run,collisions=True)['complete'])
        self.assertEqual(seen,[('0',False),('0',True),('42',False),('42',True)])
        def bad(command,**kwargs):
            if command[2].endswith('uiemap_collisions.py'):return subprocess.CompletedProcess([],0,'{}','')
            return self.reply(work['jobs'][0])
        result=update.execute(work,bad,collisions=True)
        self.assertFalse(result['complete']);self.assertTrue(result['results'][0]['published']);self.assertEqual(result['pending'],['42'])

    def test_caves_reject_collision_update_before_render(self):
        work=self.work();work['jobs'][0]['mode']='caves'
        with patch.object(update.subprocess,'run') as run,self.assertRaisesRegex(ValueError,'surface'):
            update.execute(work,collisions=True)
        run.assert_not_called()

    def test_collision_failure_never_runs_collection(self):
        work=self.work()
        def run(command,**kwargs):
            if command[2].endswith('uiemap_collisions.py'):
                return subprocess.CompletedProcess([],1,'','disk floor reached')
            return self.reply(work['jobs'][0])
        with patch.object(update.uiemap_gc,'collect') as gc:
            result=update.execute(work,run,cleanup=True,collisions=True)
        gc.assert_not_called();self.assertFalse(result['complete'])
        self.assertIn('disk floor',result['error']);self.assertTrue(result['results'][0]['published'])

    def test_configuration_changed_by_collision_child_is_rejected(self):
        work=self.work()
        def run(command,**kwargs):
            if command[2].endswith('uiemap_collisions.py'):
                self.file.write_text(self.file.read_text()+' ')
                return subprocess.CompletedProcess([],0,'{}','')
            return self.reply(work['jobs'][0])
        with self.assertRaisesRegex(ValueError,'changed during collision'):
            update.execute(work,run,collisions=True)


if __name__=='__main__':unittest.main()
