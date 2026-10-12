import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import cave_pair_history as ch

A,B,C,D,X='a'*64,'b'*64,'c'*64,'d'*64,'e'*64
G1,G2,G3,G4='1'*64,'2'*64,'3'*64,'4'*64
T=1_760_000_000
GRACE=ch.GRACE_SECONDS

def sha(n):return f'{n:064x}'

class HistoryTests(unittest.TestCase):
    def prepare(self,existing,pointer,index,geometry,now,**kwargs):
        return ch.prepare(existing,pointer_sha=pointer,index_sha=index,geometry_sha=geometry,now=now,**kwargs)
    def published(self):
        """History after A (first) then B, pointer committed to B."""
        first=self.prepare(None,None,A,G1,T).doc
        return self.prepare(first,A,B,G2,T+10).doc

    def test_bootstrap_first_publication_is_canonical_and_parsable(self):
        result=self.prepare(None,None,A,G1,T)
        self.assertTrue(result.changed);self.assertEqual(result.pruned,0)
        self.assertEqual(result.doc,{'format':ch.FORMAT,'bootstrapAt':T,'currentIndexSha256':A,'previousIndexSha256':None,
                                     'records':{A:{'publishedAt':T,'geometryIndexSha256':G1}}})
        self.assertEqual(ch.parse(result.raw),result.doc)
        self.assertEqual(result.raw,json.dumps(result.doc,sort_keys=True,separators=(',',':')).encode())
    def test_bootstrap_of_existing_legacy_pointer(self):
        same=self.prepare(None,A,A,G1,T)                     # fast path: pointer already names this index
        self.assertEqual((same.doc['currentIndexSha256'],same.doc['previousIndexSha256']),(A,None))
        with self.assertRaisesRegex(ValueError,'geometry of the actual cap pointer'):self.prepare(None,A,B,G2,T)
        other=self.prepare(None,A,B,G2,T,pointer_geometry_sha=G1)
        self.assertEqual((other.doc['currentIndexSha256'],other.doc['previousIndexSha256']),(B,A))
        self.assertEqual(other.doc['records'][A],{'publishedAt':T,'geometryIndexSha256':G1})
        self.assertEqual(other.doc['bootstrapAt'],T)
    def test_advance_retires_actual_pointer_and_preserves_bootstrap(self):
        doc=self.published()
        self.assertEqual((doc['currentIndexSha256'],doc['previousIndexSha256'],doc['bootstrapAt']),(B,A,T))
        self.assertEqual(doc['records'][A]['publishedAt'],T+10)  # retirement refreshes the grace
        later=self.prepare(doc,B,C,G3,T+10*GRACE).doc
        self.assertEqual((later['currentIndexSha256'],later['previousIndexSha256'],later['bootstrapAt']),(C,B,T))
        self.assertNotIn(A,later['records'])                  # old, unprotected, outside grace
    def test_same_publication_is_idempotent_without_time_refresh(self):
        doc=self.published();raw=ch.serialize(doc)
        for now in (T+11,T+100*GRACE):
            result=self.prepare(doc,B,B,G2,now)
            self.assertFalse(result.changed);self.assertEqual(result.raw,raw);self.assertEqual(result.pruned,0)
        self.assertEqual(doc['records'][B]['publishedAt'],T+10)
    def test_long_crash_then_retry_same_candidate_retires_actual_pointer_at_recovery(self):
        ahead=self.prepare(self.published(),B,C,G3,T+20).doc  # history C/previous B durable, pointer write crashed
        late=T+20+5*GRACE                                     # hours later B is still the served pointer
        retry=self.prepare(ahead,B,C,G3,late)
        self.assertTrue(retry.changed)                        # pointer != candidate: never "unchanged"
        self.assertEqual((retry.doc['currentIndexSha256'],retry.doc['previousIndexSha256']),(C,B))
        self.assertEqual(retry.doc['records'][B]['publishedAt'],late)
        again=self.prepare(retry.doc,B,C,G3,late+60)          # repeated failure: protection keeps following B
        self.assertEqual(again.doc['records'][B]['publishedAt'],late+60)
        following=self.prepare(again.doc,C,D,G4,late+61).doc  # pointer reached C; next generation
        self.assertEqual(following['records'][B]['publishedAt'],late+60)
        self.assertNotIn(A,following['records'])
        gone=self.prepare(following,D,X,G1,late+60+GRACE+1).doc
        self.assertNotIn(B,gone['records'])                   # grace measured from the real retirement
    def test_long_crash_then_different_candidate_retires_actual_pointer_now(self):
        ahead=self.prepare(self.published(),B,C,G3,T+20).doc
        late=T+20+5*GRACE
        other=self.prepare(ahead,B,D,G4,late).doc             # C abandoned, D published, actual B retired now
        self.assertEqual((other['currentIndexSha256'],other['previousIndexSha256']),(D,B))
        self.assertEqual((other['records'][B]['publishedAt'],other['records'][C]['publishedAt']),(late,late))
        following=self.prepare(other,D,X,G1,late+1).doc
        self.assertIn(B,following['records']);self.assertIn(C,following['records'])  # crash-ahead kept conservatively
        gone=self.prepare(following,X,sha(7),G2,late+GRACE+1).doc
        self.assertNotIn(B,gone['records']);self.assertNotIn(C,gone['records'])
    def test_crash_ahead_then_republish_actual_pointer(self):
        ahead=self.prepare(self.published(),B,C,G3,T+20).doc
        result=self.prepare(ahead,B,B,G2,T+30)
        self.assertTrue(result.changed)
        self.assertEqual((result.doc['currentIndexSha256'],result.doc['previousIndexSha256']),(B,None))
        self.assertEqual((result.doc['records'][B]['publishedAt'],result.doc['records'][C]['publishedAt']),(T+30,T+30))
        self.assertFalse(self.prepare(result.doc,B,B,G2,T+40).changed)  # now pointer == candidate == current
    def test_pointer_disagreeing_with_history_fails_closed(self):
        doc=self.published()
        for pointer in (C,None):
            with self.subTest(pointer=pointer):
                with self.assertRaisesRegex(ValueError,'not in cave cap history'):self.prepare(doc,pointer,D,G4,T+20)
        first=self.prepare(None,None,A,G1,T).doc               # crash before the very first pointer
        self.assertEqual(self.prepare(first,None,B,G2,T+1).doc['previousIndexSha256'],None)
    def test_geometry_mismatch_for_recorded_index_fails_closed(self):
        doc=self.published()
        with self.assertRaisesRegex(ValueError,'different geometry'):self.prepare(doc,B,A,G3,T+20)
        with self.assertRaisesRegex(ValueError,'different geometry'):self.prepare(doc,B,B,G3,T+20)
        with self.assertRaisesRegex(ValueError,'pointer recorded'):self.prepare(doc,B,C,G3,T+20,pointer_geometry_sha=G1)
    def test_grace_boundary_and_future_records(self):
        doc=self.published()
        doc['records'][C]={'publishedAt':T+5,'geometryIndexSha256':G3}
        doc['records'][X]={'publishedAt':T+10**6,'geometryIndexSha256':G4}  # clock regression / future dated
        kept=self.prepare(doc,B,D,G4,T+5+GRACE).doc
        self.assertIn(C,kept['records']);self.assertIn(X,kept['records'])
        gone=self.prepare(doc,B,D,G4,T+6+GRACE)
        self.assertNotIn(C,gone.doc['records']);self.assertIn(X,gone.doc['records']);self.assertGreaterEqual(gone.pruned,1)
    def test_protected_geometry_keeps_old_records(self):
        doc=self.published()
        later=self.prepare(doc,B,C,G3,T+10*GRACE,protected_geometry=frozenset({G1})).doc
        self.assertIn(A,later['records'])
        for bad in (G1,[G1,'x'],{True},list(sha(n) for n in range(ch.MAX_PROTECTED_GEOMETRY+1))):
            with self.subTest(bad=str(bad)[:20]):
                with self.assertRaisesRegex(ValueError,'protected geometry'):
                    self.prepare(doc,B,C,G3,T,protected_geometry=bad)
    def test_capacity_overflow_fails_closed_without_evicting_protected(self):
        doc=self.published()
        for n in range(ch.MAX_RECORDS-2):doc['records'][sha(n+1000)]={'publishedAt':T,'geometryIndexSha256':G1}
        ch.serialize(doc)                                     # exactly at capacity is valid
        snapshot=copy.deepcopy(doc)
        with self.assertRaisesRegex(ValueError,'capacity'):self.prepare(doc,B,C,G3,T+20)   # all young
        with self.assertRaisesRegex(ValueError,'capacity'):
            self.prepare(doc,B,C,G3,T+10*GRACE,protected_geometry={G1})  # old but protected
        self.assertEqual(doc,snapshot)                        # pure: input never mutated
        self.assertEqual(len(self.prepare(doc,B,C,G3,T+10*GRACE).doc['records']),2)  # unprotected old ones prune
        with patch.object(ch,'MAX_BYTES',200):
            with self.assertRaisesRegex(ValueError,'byte capacity'):self.prepare(None,None,A,G1,T)
    def test_input_validation(self):
        doc=self.published()
        for now in (True,float('nan'),float('inf'),-1,'1',10**400,ch.MAX_TIMESTAMP+1,float(2**60)):
            with self.subTest(now=now):
                with self.assertRaises(ValueError):self.prepare(doc,B,C,G3,now)
        for kwargs in ({'pointer':'x'},{'index':'A'*64},{'geometry':None}):
            args={'pointer':B,'index':C,'geometry':G3,**kwargs}
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):self.prepare(doc,args['pointer'],args['index'],args['geometry'],T)
        with self.assertRaises(ValueError):self.prepare(doc,B,C,G3,T,pointer_geometry_sha='z')
        broken=copy.deepcopy(doc);broken['records'].pop(A)   # previous without record
        with self.assertRaisesRegex(ValueError,'no record'):self.prepare(broken,B,C,G3,T)
    def test_parse_rejects_malformed_documents(self):
        doc=self.published();raw=ch.serialize(doc);text=raw.decode()
        def variant(**changes):return json.dumps({**doc,**changes},sort_keys=True,separators=(',',':')).encode()
        record=lambda **c:{**doc['records'][A],**c}
        bad={
          'indent':json.dumps(doc,sort_keys=True,indent=1).encode(),
          'unsorted':json.dumps(dict(reversed(list(doc.items()))),separators=(',',':')).encode(),
          'duplicate':text.replace('{"bootstrapAt"','{"bootstrapAt":1,"bootstrapAt"',1).encode(),
          'nan':text.replace(f'"bootstrapAt":{T}','"bootstrapAt":NaN').encode(),
          'overflow':text.replace(f'"bootstrapAt":{T}','"bootstrapAt":1e999').encode(),
          'bool time':variant(bootstrapAt=True),'negative':variant(bootstrapAt=-1),
          'huge int':variant(bootstrapAt=10**400),'out of range':variant(bootstrapAt=ch.MAX_TIMESTAMP+1),
          'record huge int':variant(records={**doc['records'],A:record(publishedAt=10**400)}),
          'record bool':variant(records={**doc['records'],A:record(publishedAt=False)}),
          'record extra':variant(records={**doc['records'],A:record(extra=1)}),
          'record geometry':variant(records={**doc['records'],A:record(geometryIndexSha256='x')}),
          'record key':variant(records={**doc['records'],'A'*64:record()}),
          'missing field':json.dumps({k:v for k,v in doc.items() if k!='bootstrapAt'},sort_keys=True,separators=(',',':')).encode(),
          'extra field':variant(extra=1),'format':variant(format='UIE_HIRES_PUBLICATION_HISTORY1'),
          'current missing':variant(currentIndexSha256=C),'previous missing':variant(previousIndexSha256=C),
          'previous is current':variant(previousIndexSha256=B),'empty records':variant(records={}),
          'too many':variant(records={**doc['records'],**{sha(n):record() for n in range(ch.MAX_RECORDS)}}),
          'list':b'[]','utf8':b'\xff','empty':b'','deep':b'['*30000+b']'*30000,
          'oversize':raw+b' '*ch.MAX_BYTES,'not bytes':text,
        }
        for name,value in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):ch.parse(value)
        self.assertEqual(ch.parse(raw),doc)
        edge=variant(bootstrapAt=ch.MAX_TIMESTAMP);self.assertEqual(ch.parse(edge)['bootstrapAt'],ch.MAX_TIMESTAMP)

class LoadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='uie-cap-history-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'cave-data';self.root.mkdir()
        self.raw=ch.prepare(None,pointer_sha=None,index_sha=A,geometry_sha=G1,now=T).raw
    def symlink(self,target,link,directory=False):
        try:os.symlink(target,link,target_is_directory=directory)
        except (OSError,NotImplementedError,AttributeError) as error:self.skipTest(f'symlinks unavailable: {error}')
    def test_missing_valid_and_corrupt(self):
        self.assertIsNone(ch.load(self.root))
        (self.root/ch.NAME).write_bytes(self.raw);self.assertEqual(ch.load(self.root),ch.parse(self.raw))
        (self.root/ch.NAME).write_bytes(self.raw+b'\n')
        with self.assertRaisesRegex(ValueError,'canonical'):ch.load(self.root)
        (self.root/ch.NAME).write_bytes(b'')
        with self.assertRaises(ValueError):ch.load(self.root)
    def test_non_regular_history_and_missing_root(self):
        (self.root/ch.NAME).mkdir()
        with self.assertRaises(ValueError):ch.load(self.root)
        with self.assertRaises(FileNotFoundError):ch.load(self.root.parent/'absent')
    def test_symlinked_history_file_rejected(self):
        target=self.root.parent/'elsewhere.json';target.write_bytes(self.raw)
        self.symlink(target,self.root/ch.NAME)
        with self.assertRaises(ValueError):ch.load(self.root)
    def test_symlinked_root_rejected(self):
        (self.root/ch.NAME).write_bytes(self.raw);link=self.root.parent/'linked'
        self.symlink(self.root,link,directory=True)
        with self.assertRaises(ValueError):ch.load(link)
