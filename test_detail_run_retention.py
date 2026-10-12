from pathlib import Path
from datetime import datetime,timezone
import tempfile,unittest,json,hashlib,sqlite3
from contextlib import closing
from detail_run_retention import inspect_run,RetentionError

def raw(doc):return json.dumps(doc,sort_keys=True).encode()
def sha(data):return hashlib.sha256(data).hexdigest()

class RetentionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        base=Path(temp.name).resolve();self.state=base/'state';self.store=base/'store';self.name='20261008-120000'
        self.run=self.state/'runs'/self.name
        (self.run/'ledger').mkdir(parents=True);(self.run/'attempts').mkdir()
        (self.run/'attempts'/'mesh').write_bytes(b'scratch')
        self.manifest=b'{"chunks":[]}'
        complete=raw({'format':'UIE_GENERATION1','files':{'manifest.json':sha(self.manifest)}})
        self.generation=sha(complete);self.root=self.store/'generations'/self.generation;self.root.mkdir(parents=True)
        (self.root/'COMPLETE.json').write_bytes(complete);(self.root/'manifest.json').write_bytes(self.manifest)
        self.result={'verifiedSha256':'a'*64,'planSha256':'b'*64,'attempt':'attempt1'}
        with closing(sqlite3.connect(self.run/'queue.sqlite')) as db:
            db.execute('CREATE TABLE jobs (id,state,result)')
            db.execute('INSERT INTO jobs VALUES (?,?,?)',('job','succeeded',json.dumps(self.result)))
            db.commit()
        self.ledger={'format':'UIE_ATTEMPT_PUBLICATION1','phase':'published','jobId':'job','store':str(self.store),
          **self.result,'generation':self.generation,'publishedAt':'2026-10-08T12:00:00Z','manifestSha256':sha(self.manifest)}
        self.write_ledger()
    def write_ledger(self):(self.run/'ledger'/('a'*64+'.json')).write_bytes(raw(self.ledger))
    def inspect(self,**kwargs):return inspect_run(self.state,self.name,self.store,now=datetime(2026,10,8,15,tzinfo=timezone.utc),**kwargs)
    def test_inspection_never_deletes_or_lists_audit_as_scratch(self):
        report=self.inspect();self.assertTrue(report['inspectionOnly']);self.assertFalse(report['deleted'])
        self.assertEqual(report['files'],[{'file':'attempts/mesh','bytes':7}])
        self.assertEqual((self.run/'attempts'/'mesh').read_bytes(),b'scratch')
        self.assertTrue((self.run/'queue.sqlite').exists())
    def test_running_job_is_protected(self):
        with closing(sqlite3.connect(self.run/'queue.sqlite')) as db:
            db.execute("UPDATE jobs SET state='running'");db.commit()
        with self.assertRaises(RetentionError):self.inspect()
    def test_incomplete_or_foreign_ledger_is_protected(self):
        for key,value in [('phase','publishing'),('store','/wrong'),('attempt','another')]:
            old=self.ledger[key];self.ledger[key]=value;self.write_ledger()
            with self.assertRaises(RetentionError):self.inspect()
            self.ledger[key]=old
    def test_damaged_publication_prevents_retirement(self):
        (self.root/'manifest.json').write_bytes(b'bad')
        with self.assertRaises(RetentionError):self.inspect()
    def test_recent_or_future_publication_protected(self):
        for value in ['2026-10-08T14:59:00Z','2026-10-09T12:00:00Z','bad']:
            self.ledger['publishedAt']=value;self.write_ledger()
            with self.assertRaises(RetentionError):self.inspect()
    def test_scratch_symlink_refused(self):
        try:(self.run/'attempts'/'link').symlink_to(self.root/'manifest.json')
        except OSError:self.skipTest('symlink privilege unavailable')
        with self.assertRaises(RetentionError):self.inspect()
    def test_escape_name_and_short_grace_refused(self):
        with self.assertRaises(RetentionError):inspect_run(self.state,'../escape',self.store)
        with self.assertRaises(RetentionError):self.inspect(grace_seconds=0)

if __name__=='__main__':unittest.main()
