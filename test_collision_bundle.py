import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_collision_tile as fixtures
from test_offline_renderer import volume
from test_world_hires import quad
import collision_bundle as cb
from collision_bundle import publish_index, publish_pair


class CollisionBundleTests(unittest.TestCase):
    def test_pair_references_existing_immutable_objects_and_changes_with_blocks(self):
        volume=fixtures.CollisionTileTests().fixture()
        mesh={'chunks':{(-4,-4):quad(-63,64,-63)}}
        with tempfile.TemporaryDirectory() as tmp:
            store=Path(tmp)
            first=publish_pair(store,volume,mesh,'a'*64)
            second=publish_pair(store,volume,mesh,'a'*64)
            self.assertEqual(first,second)
            raw=(store/'pairs'/f"{first['pairSha256']}.json").read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),first['pairSha256'])
            self.assertTrue((store/'objects'/f"{first['geometrySha256']}.uih").is_file())
            self.assertTrue((store/'objects'/f"{first['blocksSha256']}.uib").is_file())
            volume.metas[0]=1
            changed=publish_pair(store,volume,mesh,'a'*64)
            self.assertNotEqual(changed['pairSha256'],first['pairSha256'])
            self.assertEqual(changed['geometrySha256'],first['geometrySha256'])
            self.assertTrue((store/'pairs'/f"{first['pairSha256']}.json").exists())

    def test_failed_object_write_does_not_publish_a_pair(self):
        volume=fixtures.CollisionTileTests().fixture()
        with tempfile.TemporaryDirectory() as tmp:
            real=cb._immutable
            def failing(path,data):
                if path.suffix=='.uih': raise OSError('simulated full disk')
                return real(path,data)
            with patch.object(cb,'_immutable',side_effect=failing):
                with self.assertRaises(OSError): publish_pair(tmp,volume,{'chunks':{0:quad(-63,64,-63)}},'b'*64)
            self.assertFalse((Path(tmp)/'pairs').exists())


class CollisionIndexTests(unittest.TestCase):
    """publish_index: verified references, immutable index first, pointer last and only on success."""

    def setUp(self):
        self._tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store=Path(self._tmp.name)
        # Tile -1,-1 (negative coordinates, x=z=-64), tile 0,0, and tile 1,0 rendered without geometry.
        self.a=publish_pair(self.store,fixtures.CollisionTileTests().fixture(),
                            {'chunks':{(-4,-4):quad(-63,64,-63)}},'a'*64)
        self.b=publish_pair(self.store,volume({(1,64,1):(1,0)},x0=0,z0=0,size=64,core=(0,0,64,64)),
                            {'chunks':{(0,0):quad(1,64,1)}},'a'*64)
        self.c=publish_pair(self.store,volume({},x0=64,z0=0,size=64,core=(64,0,64,64)),{'chunks':{}},'a'*64)
        self.good={'-1,-1':self.a['pairSha256'],'0,0':self.b['pairSha256'],'1,0':self.c['pairSha256']}

    def snapshot(self):
        current=self.store/'current.json'
        indexes=self.store/'indexes'
        return (current.read_bytes() if current.exists() else None,
                sorted(p.name for p in indexes.iterdir()) if indexes.exists() else [])

    def published(self):
        return publish_index(self.store,{'-1,-1':self.a['pairSha256'],'0,0':self.b['pairSha256']})

    def assert_refused(self,pairs,pattern):
        before=self.snapshot()
        with self.assertRaisesRegex(ValueError,pattern): publish_index(self.store,pairs)
        self.assertEqual(self.snapshot(),before,'a refused publication changes neither the pointer nor the indexes')

    def write_pair(self,**changes):
        record={k:v for k,v in self.b.items() if k!='pairSha256'}
        record.update(changes)
        raw=cb.canonical(record)
        sha=hashlib.sha256(raw).hexdigest()
        cb._immutable(self.store/'pairs'/f'{sha}.json',raw)
        return sha

    def test_good_pairs_publish_canonical_immutable_index_and_pointer(self):
        self.assertIsNone(self.c['geometrySha256'],'fixture: tile without geometry')
        result=publish_index(self.store,self.good)
        self.assertEqual(result['entries'],3)
        raw=(self.store/'indexes'/f"{result['indexSha256']}.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),result['indexSha256'])
        self.assertEqual(json.loads(raw),{'format':'UIE_COLLISION_INDEX1','tileSize':64,'pairs':self.good})
        self.assertEqual(raw,json.dumps(json.loads(raw),sort_keys=True,separators=(',',':')).encode())
        self.assertEqual((self.store/'current.json').read_bytes(),
                         b'{"format":"UIE_COLLISION_POINTER1","indexSha256":"'+result['indexSha256'].encode()+b'"}')
        self.assertEqual(publish_index(self.store,dict(reversed(self.good.items()))),result,'order-independent')
        self.assertEqual(json.loads((self.store/'pairs'/f"{self.a['pairSha256']}.json").read_bytes())['x'],-64)

    def test_new_index_replaces_pointer_and_keeps_old_index(self):
        first=self.published()
        second=publish_index(self.store,self.good)
        self.assertNotEqual(first['indexSha256'],second['indexSha256'])
        self.assertTrue((self.store/'indexes'/f"{first['indexSha256']}.json").is_file())
        self.assertIn(second['indexSha256'],(self.store/'current.json').read_text())
        self.assertEqual([p.name for p in self.store.iterdir() if p.name.startswith('.')],[],'no temporary files left')

    def test_wrong_or_malformed_keys_and_hashes_are_refused(self):
        self.published()
        a=self.a['pairSha256']
        self.assert_refused({'0,0':a},'covers -64,-64, not tile 0,0')
        self.assert_refused({'-1,-1':self.b['pairSha256']},'not tile -1,-1')
        for key in ['01,0','-0,0','0,0,0',' 0,0','0;0','468751,0','0,-468751','+1,0','']:
            self.assert_refused({key:a},'tile key')
        self.assert_refused({5:a},'invalid tile key')
        self.assert_refused({'-1,-1':a.upper()},'invalid pair hash')
        self.assert_refused({'-1,-1':a[:63]},'invalid pair hash')
        self.assert_refused({'-1,-1':None},'invalid pair hash')
        self.assert_refused({'-1,-1':'../'+a[3:]},'invalid pair hash')
        self.assert_refused([('-1,-1',a)],'must be a dict')

    def test_missing_or_corrupt_pair_leaves_pointer_unchanged(self):
        self.published()
        self.assert_refused({'-1,-1':self.a['pairSha256'],'0,0':'f'*64},'missing pairs/')
        (self.store/'pairs'/f"{self.b['pairSha256']}.json").write_bytes(b'{"format":"other"}')
        self.assert_refused(self.good,'does not match its hash')

    def test_missing_or_corrupt_objects_leave_pointer_unchanged(self):
        self.published()
        blocks=self.store/'objects'/f"{self.a['blocksSha256']}.uib"
        geometry=self.store/'objects'/f"{self.b['geometrySha256']}.uih"
        good_blocks,good_geometry=blocks.read_bytes(),geometry.read_bytes()
        blocks.unlink()
        self.assert_refused(self.good,'missing objects/')
        blocks.write_bytes(gzip.compress(b'different decoded content'))
        self.assert_refused(self.good,'does not match its identity')
        blocks.write_bytes(good_blocks[:-20])
        self.assert_refused(self.good,'truncated|not valid gzip')
        blocks.write_bytes(good_blocks)
        geometry.write_bytes(good_geometry+b'x')
        self.assert_refused(self.good,'does not match its identity')
        geometry.unlink()
        self.assert_refused(self.good,'missing objects/')
        geometry.write_bytes(good_geometry)
        publish_index(self.store,self.good)

    def test_pair_records_are_checked_against_key_objects_and_atlas_syntax(self):
        self.published()
        self.assert_refused({'1,0':self.write_pair(x=64)},'not the UIB1 tile at 64,0')
        self.assert_refused({'0,0':self.write_pair(x=64)},'covers 64,0, not tile 0,0')
        self.assert_refused({'0,0':self.write_pair(atlasSha256='A'*64)},'invalid atlas identity')
        self.assert_refused({'0,0':self.write_pair(blocksSha256=self.a['blocksSha256'])},'not the UIB1 tile at 0,0')
        self.assert_refused({'0,0':self.write_pair(geometrySha256=self.a['geometrySha256'])},'not the UIH1 tile at 0,0')
        self.assert_refused({'0,0':self.write_pair(geometrySha256='e'*64)},'missing objects/')
        self.assert_refused({'0,0':self.write_pair(x=0.0)},'covers')
        self.assert_refused({'0,0':self.write_pair(format='UIE_COLLISION_PAIR2')},'canonical')
        loose=json.dumps({k:v for k,v in self.b.items() if k!='pairSha256'}).encode()   # non-canonical spacing
        sha=hashlib.sha256(loose).hexdigest()
        (self.store/'pairs'/f'{sha}.json').write_bytes(loose)
        self.assert_refused({'0,0':sha},'canonical')

    def test_atlas_object_is_not_required_in_the_store(self):
        self.assertFalse(any(self.store.rglob('atlas*')))
        publish_index(self.store,self.good)

    def test_failed_index_write_keeps_previous_pointer(self):
        first=self.published()
        before=(self.store/'current.json').read_bytes()
        real=cb._immutable
        def failing(path,data):
            if path.parent.name=='indexes': raise OSError('simulated full disk')
            return real(path,data)
        with patch.object(cb,'_immutable',side_effect=failing):
            with self.assertRaises(OSError): publish_index(self.store,self.good)
        self.assertEqual((self.store/'current.json').read_bytes(),before)
        self.assertIn(first['indexSha256'],before.decode())

    def test_limits_are_consistent(self):
        too_many={f'{i},0':'a'*64 for i in range(cb.MAX_INDEX_ENTRIES+1)}
        self.assert_refused(too_many,'exceeds')
        # The entry limit always fits the byte limit, even with the longest keys.
        worst={f'-{400000+i%60000},-{400000+i//60000}':'f'*64 for i in range(cb.MAX_INDEX_ENTRIES)}
        self.assertTrue(all(len(k)==15 for k in worst))
        self.assertLessEqual(len(cb.canonical({'format':cb.INDEX_FORMAT,'tileSize':64,'pairs':worst})),cb.MAX_INDEX_BYTES)
        self.assertEqual(cb.MAX_TILE_COORD,468750)

    def test_empty_index_is_allowed_and_missing_store_refused(self):
        result=publish_index(self.store,{})
        self.assertEqual(result['entries'],0)
        with self.assertRaisesRegex(ValueError,'does not exist'): publish_index(self.store/'absent',{})


if __name__=='__main__': unittest.main()
