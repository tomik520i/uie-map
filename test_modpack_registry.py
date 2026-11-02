import contextlib,io,json,os,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import modpack_registry as reg
import uiemap_modpack
from test_reader import level_dat

class RegistryTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name).resolve()
        self.world=self.root/'world';self.world.mkdir();self.mods=self.root/'mods';self.mods.mkdir()
        self.level=self.world/'level.dat';self.level.write_bytes(level_dat([('\x01sample:stone',1),('\x01other:machine',42),('\x02sample:tool',99)]))
        with zipfile.ZipFile(self.mods/'sample.jar','w') as z:
            z.writestr('mcmod.info','[{"modid":"sample"}]')
            z.writestr('assets/sample/textures/blocks/stone.png',b'candidate')
    def test_real_registry_filters_items_and_includes_ids_without_claiming_mapping(self):
        before=self.level.read_bytes()
        d=uiemap_modpack.inventory(self.mods,world=self.world,include_blocks=True)['registry']
        self.assertEqual(d['registeredBlocks'],2);self.assertEqual(d['blocksWithNameCandidates'],1)
        self.assertFalse(d['mappingVerified']);self.assertTrue(d['archiveScanComplete'])
        row=next(r for r in d['namespaces'] if r['namespace']=='sample')
        self.assertEqual(row['blocks'][0]['id'],1);self.assertFalse(row['blocks'][0]['mappingVerified'])
        self.assertEqual(row['blocks'][0]['nameCandidates'][0]['archives'],['sample.jar'])
        self.assertEqual(self.level.read_bytes(),before)
    def test_namespace_aliases_and_case_do_not_assign_false_matches(self):
        d=reg.compare({1:'UPPER:stone',2:'another:stone'},[{'file':'a.jar','mods':[{'modid':'another'}]}],{'upper:blocks/stone':{'a.jar'}})
        rows={r['namespace']:r for r in d['namespaces']}
        self.assertEqual(rows['UPPER']['caseVariantTextureNamespaces'],['upper'])
        self.assertEqual(d['blocksWithNameCandidates'],0)
        self.assertTrue(rows['another']['reviewNeeded']);self.assertEqual(rows['another']['declaredModArchives'],['a.jar'])
        self.assertNotIn('blocks',rows['UPPER'])
    def test_multiple_providers_are_kept_and_incomplete_inventory_marked(self):
        d=reg.compare({1:'x:a'},[],{'x:blocks/a':{'one.jar','two.jar'}},include_blocks=True,archive_scan_complete=False)
        self.assertFalse(d['archiveScanComplete'])
        self.assertEqual(d['namespaces'][0]['blocks'][0]['nameCandidates'][0]['archives'],['one.jar','two.jar'])
    def test_registry_hash_does_not_depend_on_iteration_order(self):
        self.assertEqual(reg.compare({1:'a:b',2:'c:d'},[],{})['registrySha256'],reg.compare({2:'c:d',1:'a:b'},[],{})['registrySha256'])
    def test_explicit_alias_preserves_registry_names_and_is_not_a_verified_mapping(self):
        d=reg.compare({4:'Mod|Core:stone'},[],{'modassets:blocks/stone':{'one.jar'}},namespace_map={'Mod|Core':'modassets'},include_blocks=True)
        row=d['namespaces'][0];self.assertTrue(row['namespaceMappingExplicit']);self.assertEqual(row['textureNamespace'],'modassets')
        self.assertEqual(row['blocks'][0]['registryName'],'Mod|Core:stone');self.assertEqual(d['blocksWithNameCandidates'],1)
        self.assertFalse(d['mappingVerified'])
        with self.assertRaisesRegex(ValueError,'absent'):reg.compare({4:'Mod:stone'},[],{},namespace_map={'typo':'mod'})
    def test_namespace_map_limits_and_duplicate_keys(self):
        p=self.root/'namespaces.json'
        for raw in ['[]','{"Mod":"../outside"}','{"Mod":12}','{"Mod":"a","Mod":"b"}',' '*65537]:
            p.write_text(raw)
            with self.assertRaises(ValueError):reg.load_namespace_map(p)
        p.write_text('{"Mod|Core":"assets_domain"}')
        self.assertEqual(reg.load_namespace_map(p),{'Mod|Core':'assets_domain'})
    def test_cli_explicit_alias(self):
        import uiemap
        p=self.root/'aliases.json';p.write_text('{"other":"sample"}')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=uiemap.main(['modpack','--mods',str(self.mods),'--world',str(self.world),'--namespace-map',str(p)])
        self.assertEqual(code,0)
        row=next(v for v in json.loads(output.getvalue())['registry']['namespaces'] if v['namespace']=='other')
        self.assertTrue(row['namespaceMappingExplicit']);self.assertEqual(row['textureArchives'],['sample.jar'])
    def test_level_limits_corruption_and_changed_read_fail(self):
        with patch.object(reg,'MAX_LEVEL_BYTES',1):
            with self.assertRaises(ValueError):reg.read_registry(self.world)
        s=self.level.stat();fields={k:getattr(s,k) for k in ['st_mode','st_dev','st_ino','st_size','st_mtime_ns']}
        changed=SimpleNamespace(**dict(fields,st_mtime_ns=s.st_mtime_ns+1))
        with patch.object(reg.os,'fstat',side_effect=[s,changed]):
            with self.assertRaisesRegex(ValueError,'changed'):reg.read_registry(self.world)
        self.level.write_bytes(b'bad level')
        with self.assertRaises(ValueError):reg.read_registry(self.world)
    @unittest.skipUnless(os.name=='posix','POSIX symlink')
    def test_linked_level_is_not_followed(self):
        saved=self.world/'saved';self.level.rename(saved);self.level.symlink_to(saved)
        with self.assertRaisesRegex(ValueError,'linked'):reg.read_registry(self.world)
    def test_cli_requires_world_for_block_details(self):
        import uiemap
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:uiemap.main(['modpack','--mods',str(self.mods),'--include-blocks'])
        self.assertEqual(error.exception.code,2)
        out=io.StringIO()
        with contextlib.redirect_stdout(out):code=uiemap.main(['modpack','--mods',str(self.mods),'--world',str(self.world),'--include-blocks'])
        self.assertEqual(code,0);self.assertEqual(json.loads(out.getvalue())['registry']['registeredBlocks'],2)

if __name__=='__main__':unittest.main()
