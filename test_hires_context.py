import hashlib,json,os,unittest
import hires_bundle as hb
import hires_backfill as bf
import test_hires_backfill as fixtures
from hires_context import validate_context
from hires_publish_session import PublishSession

class ContextTests(unittest.TestCase):
    setUp=fixtures.BackfillTests.setUp
    alias=fixtures.BackfillTests.alias
    publish_legacy=fixtures.BackfillTests.publish_legacy

    def context(self):
        path=self.out/'atlas-layout.json'
        path.write_text(json.dumps({'format':'UIE_ATLAS_LAYOUT1','registered':{}}))
        sha=hb.ingest_layout(self.out,path)
        return dict(world='uie',dimension='overworld',renderer=self.meta['renderer'],caves=False,layoutSha256=sha)

    def test_v2_publication_hash_binds_context_and_layout(self):
        context=self.context();prepared=bf.step(self.out)
        result=hb.publish_index(self.out,prepared['references'],prepared['atlasSha256'],prepared['rendererIdentity'],context=context)
        raw=(self.out/'indexes'/f'{result["indexSha256"]}.json').read_bytes()
        doc=json.loads(raw)
        self.assertEqual(doc['format'],hb.INDEX_FORMAT_V2)
        self.assertEqual(doc['context'],context)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),result['indexSha256'])
        self.assertEqual((self.out/'layouts'/f'{context["layoutSha256"]}.json').stat().st_ino,(self.out/'atlas-layout.json').stat().st_ino)

    def test_invalid_context_rejected(self):
        good=self.context()
        for key,value in [('world',''),('world',' uie'),('world','a\n'),('world','č'),('dimension','a'*129),('caves',1),('layoutSha256','z'*64)]:
            with self.subTest(key=key,value=value):
                with self.assertRaises(ValueError):validate_context({**good,key:value})
        with self.assertRaises(ValueError):validate_context({**good,'extra':1})

    def test_corrupt_layout_keeps_previous_pointer(self):
        prepared=bf.step(self.out)
        hb.publish_index(self.out,prepared['references'],prepared['atlasSha256'],prepared['rendererIdentity'])
        before=(self.out/'current.json').read_bytes();context=self.context()
        (self.out/'layouts'/f'{context["layoutSha256"]}.json').write_bytes(b'{}')
        with self.assertRaisesRegex(hb.BundleError,'layout hash mismatch'):
            hb.publish_index(self.out,prepared['references'],prepared['atlasSha256'],prepared['rendererIdentity'],context=context)
        self.assertEqual((self.out/'current.json').read_bytes(),before)

    @unittest.skipUnless(os.name=='posix','bounded session requires POSIX')
    def test_session_refuses_world_switch_downgrade_and_wrong_cave_mode(self):
        prepared=bf.step(self.out);context=self.context();session=PublishSession()
        session.publish_prepared(self.out,prepared,context=context,plan_complete=True)
        before=(self.out/'current.json').read_bytes()
        for other in [None,{**context,'world':'another'},{**context,'dimension':'nether'},{**context,'caves':True}]:
            with self.assertRaises(hb.BundleError):session.publish_prepared(self.out,prepared,context=other,plan_complete=True)
            self.assertEqual((self.out/'current.json').read_bytes(),before)

    def test_layout_schema_and_duplicates_rejected_before_link(self):
        path=self.out/'bad.json'
        for raw in [b'{}',b'{"format":"UIE_ATLAS_LAYOUT1","registered":{},"registered":{}}',b'{"format":"UIE_ATLAS_LAYOUT1","registered":[],"x":NaN}']:
            path.write_bytes(raw)
            with self.assertRaises(hb.BundleError):hb.ingest_layout(self.out,path)
