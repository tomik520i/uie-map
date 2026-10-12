import hashlib,json,os,stat,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_cave_pair as pairs
import test_cave_pair_index as index_fixture
import cave_pair_store as store
import cave_pair as cp
import cave_pair_history as history
import cave_retention as cr
import hires_bundle as hb
import hires_history as root_history
import hires_retention as hr
from cave_pair_publish import publish_from_jobs
from cave_cap_codec import encode,CELLS
from world_hires import encode_tile
from test_world_hires import quad

GRACE=cr.GRACE_SECONDS
def canonical(doc):return json.dumps(doc,sort_keys=True,separators=(',',':')).encode()

class RetentionTests(unittest.TestCase):
    def setUp(self):
        pairs.PairTests.setUp(self)
        self.raw_geometry=self.geometry
        index_fixture.IndexTests.setUp(self)
        self.temp=tempfile.TemporaryDirectory(prefix='uie-cave-retention-')
        self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name).resolve()
        (self.out/hb.OWNED_MARKER).write_text('test')
        for folder in ['indexes','objects','jobs']:(self.out/folder).mkdir()
        self.root=self.out/'cave-data'
        self.pair_sha,_=self.store_pair(self.raw_geometry,self.caps)
        self.link_root(self.doc['geometrySha256'])
        self.write_geometry()
        self.job={'capPairs':{'-1,2':self.pair_sha}}
        self.write_job()

    # ---- fixture helpers
    def store_pair(self,geometry,caps):
        return store.store(self.out,geometry,caps,atlas_sha=self.doc['atlasSha256'],renderer_identity=self.doc['rendererIdentity'])
    def link_root(self,geometry_sha):
        os.link(self.root/'objects'/f'{geometry_sha}.uih',self.out/'objects'/f'{geometry_sha}.uih')
    def write_job(self,name='0_0.json',job=None):
        (self.out/'jobs'/name).write_text(json.dumps(self.job if job is None else job))
    def write_geometry(self):
        previous=getattr(self,'sha',None)
        raw=canonical(self.geometry);self.sha=hashlib.sha256(raw).hexdigest()
        (self.out/'indexes'/f'{self.sha}.json').write_bytes(raw)
        (self.out/'current.json').write_text(json.dumps({'format':'UIE_HIRES_POINTER1','indexSha256':self.sha}))
        root_history.record_publication(self.out,self.sha,previous)
    def publish(self):
        result=publish_from_jobs(self.out,self.sha);self.assertTrue(result['published'],result)
        return result['indexSha256']
    def orphan(self):
        """A stored pair for another tile that no job, index or history references."""
        return self.store_pair(encode_tile(0,128,[quad(1,30,129)]),encode(0,128,bytearray(CELLS),present_mask=1))
    def next_generation(self,step,*,publish=True,link=True):
        """New geometry for the same tile; caps bytes identical, so the cap payload is shared."""
        sha,doc=self.store_pair(encode_tile(-64,128,[quad(-63+step,30,129)]),self.caps)
        if link:self.link_root(doc['geometrySha256'])
        self.geometry['tiles']['-1,2']=doc['geometrySha256'];self.write_geometry()
        self.job={'capPairs':{'-1,2':sha}};self.write_job()
        return sha,doc,(self.publish() if publish else None)
    def write_cap_index(self,doc):
        raw=canonical(doc);sha=hashlib.sha256(raw).hexdigest()
        (self.root/'indexes'/f'{sha}.json').write_bytes(raw);return sha
    def cap_index(self,sha):return json.loads((self.root/'indexes'/f'{sha}.json').read_bytes())
    def age_bootstrap(self):
        doc=history.load(self.root);doc['bootstrapAt']=0
        (self.root/history.NAME).write_bytes(history.serialize(doc))
    def plan(self,now=None):
        return cr.plan_retention(self.out,now=time.time()+3*GRACE if now is None else now)
    def path(self,kind,sha):return f'cave-data/{kind}/{sha}{cr.KINDS[kind]}'
    def paths(self,result):return {item['path'] for item in result['candidates']}
    def snapshot(self):
        state={}
        for folder,dirs,files in os.walk(self.out):
            for name in dirs+files:
                path=Path(folder)/name;info=os.lstat(path)
                state[str(path)]=(info.st_mode,info.st_size,info.st_mtime_ns,
                                  path.read_bytes() if stat.S_ISREG(info.st_mode) else None)
        return state
    def symlink(self,target,link,directory=False):
        try:os.symlink(target,link,target_is_directory=directory)
        except (OSError,NotImplementedError,AttributeError) as error:self.skipTest(f'symlinks unavailable: {error}')

    # ---- protection
    def test_published_fixture_protects_publication_and_lists_only_hardlink_alias(self):
        index=self.publish();before=self.snapshot()
        result=self.plan()
        self.assertEqual(before,self.snapshot())                       # read-only
        self.assertEqual((result['dryRun'],result['state'],result['reclaimableBytes']),(True,'planned',None))
        self.assertEqual((result['capPointerIndexSha256'],result['rootCurrentIndexSha256']),(index,self.sha))
        self.assertEqual(result['protected'],{'rootIndexes':1,'capIndexes':1,'pairs':1,'jobPairs':1,'youngPairs':0,
                                              'caps':1,'aliases':0})
        alias=self.path('objects',self.doc['geometrySha256'])
        self.assertEqual(self.paths(result),{alias})                   # linked from root objects/: removable name
        item=result['candidates'][0]
        self.assertEqual(item['links'],2);self.assertEqual(item['identity']['size'],len(self.raw_geometry))
        self.assertEqual(set(item['identity']),{'device','inode','size','mtimeNs','ctimeNs'})
        self.assertEqual(result['logicalBytes'],len(self.raw_geometry))
        self.assertEqual((result['candidateCount'],result['candidatesOmitted'],result['candidatesTruncated']),(1,0,False))
        self.assertLessEqual(result['metadataFiles'],cr.MAX_METADATA_FILES);self.assertGreater(result['metadataBytes'],0)
    def test_orphans_are_candidates_unknown_names_and_root_objects_never(self):
        index=self.publish();orphan,doc=self.orphan()
        for path in [self.root/'caps/notes.txt',self.root/'pairs'/f'{orphan}.json.bak',self.root/'objects/.cave-tmp',
                     self.out/'jobs/readme.txt',self.out/'indexes/notes']:path.write_text('x')
        (self.root/'caps/sub').mkdir()
        result=self.plan()
        self.assertEqual(self.paths(result),{self.path('objects',self.doc['geometrySha256']),self.path('pairs',orphan),
                                             self.path('caps',doc['capSha256']),self.path('objects',doc['geometrySha256'])})
        self.assertEqual(result['unknown'],{'rootIndexes':1,'jobs':1,'indexes':0,'pairs':1,'caps':2,'objects':1})
        for path in self.paths(result):
            self.assertTrue(path.startswith('cave-data/'));self.assertIn(path.split('/')[1],cr.KINDS)
        self.assertNotIn(self.path('indexes',index),self.paths(result))
    def test_no_history_before_first_publication_has_zero_candidates(self):
        result=self.plan()
        self.assertEqual((result['state'],result['candidates'],result['candidateCount']),('no-cap-history',[],0))
        self.assertEqual(result['protected']['jobPairs'],1);self.assertGreaterEqual(result['suppressedCandidates'],1)
    def test_bootstrap_grace_including_equality_suppresses_candidates(self):
        orphan,_=self.orphan();self.publish()
        bootstrap=history.load(self.root)['bootstrapAt']
        waiting=self.plan(bootstrap+GRACE)
        self.assertEqual((waiting['state'],waiting['candidates'],waiting['logicalBytes']),('bootstrap-grace',[],0))
        self.assertGreaterEqual(waiting['suppressedCandidates'],1)
        planned=self.plan(bootstrap+GRACE+1)
        self.assertEqual(planned['state'],'planned');self.assertIn(self.path('pairs',orphan),self.paths(planned))
    def test_young_cap_index_protected_and_cap_index_of_root_geometry_always_protected(self):
        index=self.publish();doc=self.cap_index(index);self.age_bootstrap()
        other=self.write_cap_index({**doc,'geometryIndexSha256':'9'*64,'pairs':{}})     # unknown geometry
        same=self.write_cap_index({**doc,'pairs':{}})                                   # root current geometry
        young=self.plan(time.time())
        self.assertEqual((young['state'],young['candidates']),('planned',[]))           # all young, equality incl.
        old=self.plan()
        self.assertIn(self.path('indexes',other),self.paths(old))
        self.assertNotIn(self.path('indexes',same),self.paths(old))
        self.assertEqual(old['protected']['capIndexes'],2)
    def test_previous_generation_protected_then_released(self):
        first=self.publish();p2,d2,second=self.next_generation(1)
        result=self.plan()
        self.assertEqual((result['protected']['capIndexes'],result['protected']['pairs'],result['protected']['caps']),(2,2,1))
        self.assertEqual(self.paths(result),{self.path('objects',self.doc['geometrySha256']),self.path('objects',d2['geometrySha256'])})
        self.next_generation(2)
        result=self.plan()
        self.assertIn(self.path('indexes',first),self.paths(result));self.assertIn(self.path('pairs',self.pair_sha),self.paths(result))
        self.assertNotIn(self.path('indexes',second),self.paths(result));self.assertNotIn(self.path('pairs',p2),self.paths(result))
        self.assertNotIn(self.path('caps',self.doc['capSha256']),self.paths(result))   # shared by protected pairs
    def test_crash_gap_job_protects_unpublished_pair_and_single_link_alias(self):
        index=self.publish()
        p2,d2,_=self.next_generation(1,publish=False,link=False)      # root geometry ahead of the cap pointer
        result=self.plan()
        self.assertEqual(result['state'],'planned')                    # mismatch is normal while waiting
        self.assertEqual((result['capPointerIndexSha256'],result['rootCurrentIndexSha256']),(index,self.sha))
        self.assertNotIn(self.path('pairs',p2),self.paths(result));self.assertNotIn(self.path('caps',d2['capSha256']),self.paths(result))
        self.assertEqual(result['protected']['aliases'],1)              # only name of a protected geometry
        self.assertEqual(self.paths(result),{self.path('objects',self.doc['geometrySha256'])})
        self.link_root(d2['geometrySha256'])                           # now a removable second name
        self.assertIn(self.path('objects',d2['geometrySha256']),self.paths(self.plan()))
    def test_dot_prefixed_job_references_are_protected(self):
        self.publish();orphan,_=self.orphan()
        hidden=self.out/'jobs/.hidden.json';self.write_job('.hidden.json',{'capPairs':{'0,2':orphan}})
        self.assertIn(hidden,list((self.out/'jobs').glob('*.json')))    # the publisher reads it too
        result=self.plan()
        self.assertNotIn(self.path('pairs',orphan),self.paths(result));self.assertEqual(result['protected']['jobPairs'],2)
        self.assertEqual(result['jobs'],2)

    def test_job_population_matches_publisher_glob_exactly(self):
        self.publish();orphan,_=self.orphan()
        for name in ('.json','.hidden.json','UPPER.JSON','x.json.tmp'):
            with self.subTest(name):
                path=self.out/'jobs'/name;self.write_job(name,{'capPairs':{'0,2':orphan}})
                try:
                    read_by_publisher=path in list((self.out/'jobs').glob('*.json'))
                    protected=self.path('pairs',orphan) not in self.paths(self.plan())
                    self.assertEqual(protected,read_by_publisher)
                finally:path.unlink()
    def test_young_unreferenced_manifest_protects_its_older_reused_cap(self):
        self.publish();self.age_bootstrap()
        orphan,doc=self.orphan()                                       # old pair, cap and alias
        stored=[os.lstat(path) for path in (self.root/'pairs'/f'{orphan}.json',self.root/'caps'/f"{doc['capSha256']}.uic.gz",
                                            self.root/'objects'/f"{doc['geometrySha256']}.uih")]
        cutoff=max(max(info.st_mtime,info.st_ctime) for info in stored)+0.05
        time.sleep(0.25)                                               # filesystem clocks may be coarse
        # Producer crashed after storing this manifest but before its job record: nobody references it yet,
        # and its content-addressed cap payload was reused from the older pair.
        young=dict(doc,rendererIdentity='e'*64);raw=cp.canonical(young);young_sha=hashlib.sha256(raw).hexdigest()
        (self.root/'pairs'/f'{young_sha}.json').write_bytes(raw)
        result=self.plan(cutoff+GRACE)
        self.assertEqual(result['state'],'planned')
        self.assertIn(self.path('pairs',orphan),self.paths(result))     # the old manifest itself is unreferenced
        self.assertNotIn(self.path('pairs',young_sha),self.paths(result))
        self.assertNotIn(self.path('caps',doc['capSha256']),self.paths(result))
        self.assertNotIn(self.path('objects',doc['geometrySha256']),self.paths(result))  # single-link alias
        self.assertGreaterEqual(result['protected']['youngPairs'],1)
    def test_young_malformed_manifest_fails_closed(self):
        self.publish();self.age_bootstrap()
        (self.root/'pairs'/f"{'e'*64}.json").write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.plan(time.time())

    # ---- fail closed
    def test_missing_or_corrupt_references_fail_closed(self):
        index=self.publish();orphan,_=self.orphan();self.age_bootstrap()
        manifest=self.root/'pairs'/f'{self.pair_sha}.json';cap=self.root/'caps'/f"{self.doc['capSha256']}.uic.gz"
        def rename(path):
            moved=path.with_name(path.name+'.moved');path.rename(moved);return lambda:moved.rename(path)
        def write(path,raw):
            saved=path.read_bytes() if path.exists() else None;path.write_bytes(raw)
            return lambda:path.write_bytes(saved) if saved is not None else path.unlink()
        mismatched=self.cap_index(index);mismatched['pairs']={'-1,2':orphan}
        cases=[
          ('protected cave pair missing',lambda:write(self.out/'jobs/x.json',canonical({'capPairs':{'0,2':'f'*64}}))),
          ('protected cave pair missing',lambda:rename(manifest)),
          ('hash mismatch',lambda:write(manifest,b'{}')),
          ('cap payload missing',lambda:rename(cap)),
          ('protected cave cap index missing',lambda:rename(self.root/'indexes'/f'{index}.json')),
          ('hash mismatch',lambda:write(self.root/'indexes'/f"{'f'*64}.json",b'{}')),
          ('job',lambda:write(self.out/'jobs/bad.json',b'[]')),
          ('job',lambda:write(self.out/'jobs/bad.json',canonical({'capPairs':{'-1,2':'x'}}))),
          ('without cap history',lambda:rename(self.root/history.NAME)),
          ('reference differs',lambda:write(self.root/'indexes'/f'{hashlib.sha256(canonical(mismatched)).hexdigest()}.json',canonical(mismatched))),
          ('not in cap history',lambda:write(self.root/'current.json',canonical({'format':'UIE_CAVE_CAP_POINTER1','indexSha256':orphan}))),
          ('invalid cave cap pointer',lambda:write(self.root/'current.json',b'{"format":"UIE_CAVE_CAP_POINTER1"}')),
        ]
        for message,damage in cases:
            with self.subTest(message):
                restore=damage()
                try:
                    with self.assertRaisesRegex(ValueError,message):self.plan()
                finally:restore()
        for missing in (self.out/root_history.NAME,self.out/'jobs'):
            with self.subTest(str(missing.name)):
                restore=rename(missing)
                try:
                    with self.assertRaises(FileNotFoundError):self.plan()
                finally:restore()
        self.assertEqual(self.plan()['state'],'planned')               # restored fixture is valid again
    def test_retained_surface_geometry_fails_closed(self):
        self.publish();self.geometry['context']['caves']=False;self.write_geometry()
        with self.assertRaisesRegex(ValueError,'not cave geometry'):self.plan()
    def test_symlinked_directories_and_files_rejected(self):
        self.publish();target=self.out.parent/f'{self.out.name}-target.json';target.write_text('{}')
        self.addCleanup(lambda:target.unlink(missing_ok=True))
        for link in [self.root/'caps'/f"{'e'*64}.uic.gz",self.out/'jobs/link.json',self.root/'pairs'/f"{'e'*64}.json"]:
            with self.subTest(link.name):
                self.symlink(target,link)
                try:
                    with self.assertRaisesRegex(ValueError,'symlink'):self.plan()
                finally:link.unlink()
        link=self.root/'pairs';real=self.root/'pairs-real';link.rename(real)
        self.symlink(real,link,directory=True)
        def unlink_directory_symlink():
            try:os.unlink(link)
            except OSError:os.rmdir(link)                              # Windows directory symlink
        self.addCleanup(unlink_directory_symlink)
        with self.assertRaisesRegex(ValueError,'real directory'):self.plan()
    def test_budget_exhaustion_raises_without_plan(self):
        self.publish();self.orphan()
        for name,value in (('MAX_METADATA_BYTES',200),('MAX_METADATA_FILES',3),('MAX_SCANNED',3)):
            with self.subTest(name),patch.object(cr,name,value):
                with self.assertRaisesRegex(ValueError,'capacity'):self.plan()
    def test_candidate_listing_truncated_only_after_complete_scan(self):
        self.publish();self.orphan()
        with patch.object(cr,'MAX_CANDIDATES',1):result=self.plan()
        self.assertEqual(len(result['candidates']),1);self.assertTrue(result['candidatesTruncated'])
        self.assertEqual(result['candidateCount'],4);self.assertEqual(result['candidatesOmitted'],3)
        self.assertEqual(result['protected']['pairs'],1)                # protection complete despite the cut
    def test_pointer_or_history_change_during_scan_fails(self):
        self.publish();real=hr._index
        def index(out,sha,charge):
            doc=history.load(self.root);doc['bootstrapAt']=0
            (self.root/history.NAME).write_bytes(history.serialize(doc));return real(out,sha,charge)
        with patch.object(hr,'_index',side_effect=index):
            with self.assertRaisesRegex(ValueError,'changed during'):self.plan()
    def test_invalid_now_rejected(self):
        for now in (True,float('nan'),float('inf'),-1,10**400):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):cr.plan_retention(self.out,now=now)
