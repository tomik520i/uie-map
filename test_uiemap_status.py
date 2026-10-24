import json,subprocess,sys,unittest
from pathlib import Path
import test_uiemap_serve as fixtures
import uiemap_status as status


class StatusTests(unittest.TestCase):
    setUp=fixtures.WebConfigTests.setUp
    index=fixtures.WebConfigTests.index

    def record(self,**values):
        doc=dict(format='UIE_HIRES_STATUS1',state='running',updatedAt=950,total=10,done=2)
        doc.update(values);(self.stores['0']/'status.json').write_text(json.dumps(doc))

    def test_published_and_unpublished_separate_readonly(self):
        (self.stores['42']/'current.json').unlink()
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result=status.inspect(self.file,now=1000)
        self.assertEqual(result['dimensions'][0]['publication']['state'],'published')
        self.assertFalse(result['dimensions'][0]['publication']['payloadsVerified'])
        self.assertEqual(result['dimensions'][1]['publication']['state'],'unpublished')
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_running_stale_future_and_done(self):
        for updated,expected in ((950,'running-reported'),(800,'stale-running-record'),(1100,'stale-running-record')):
            self.record(updatedAt=updated)
            row=status.inspect(self.file,now=1000)['dimensions'][0]
            self.assertEqual(row['render']['state'],expected);self.assertFalse(row['render']['processVerified'])
        self.record(state='done',planComplete=False)
        row=status.inspect(self.file,now=1000)['dimensions'][0]
        self.assertFalse(row['render']['planComplete'])

    def test_corrupt_progress_does_not_hide_published_map(self):
        self.record(done=True)
        row=status.inspect(self.file,now=1000)['dimensions'][0]
        self.assertEqual(row['render']['state'],'error');self.assertEqual(row['publication']['state'],'published')

    def test_failed_render_keeps_prior_publication(self):
        self.record(state='done',failedJobs=1,planComplete=False)
        row=status.inspect(self.file,now=1000)['dimensions'][0]
        self.assertTrue(row['render']['hasFailures']);self.assertEqual(row['publication']['state'],'published')

    def test_wrong_dimension_does_not_hide_other_dimension(self):
        self.index(self.stores['0'],'42')
        rows=status.inspect(self.file,now=1000)['dimensions']
        self.assertEqual(rows[0]['publication']['state'],'error')
        self.assertEqual(rows[1]['publication']['state'],'published')

    def test_hash_tamper_is_error_and_cli_returns_two(self):
        pointer=json.loads((self.stores['0']/'current.json').read_text())
        (self.stores['0']/'indexes'/(pointer['indexSha256']+'.json')).write_text('{}')
        run=subprocess.run([sys.executable,'-B',str(Path(status.__file__).with_name('uiemap.py')),'status',str(self.file)],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertIn('hash',json.loads(run.stdout)['dimensions'][0]['publication']['error'])


if __name__=='__main__':unittest.main()
