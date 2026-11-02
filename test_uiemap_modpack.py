from pathlib import Path
import contextlib,io,json,tempfile,unittest,zipfile,os
from unittest.mock import patch
import uiemap_modpack as modpack

class ModpackTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
    def jar(self,name='a.jar',entries=None):
        path=self.root/name
        with zipfile.ZipFile(path,'w') as out:
            for key,value in (entries or {'mcmod.info':json.dumps([{'modid':'sample','name':'Test','version':'1'}]),
                 'assets/sample/textures/blocks/stone.png':b'not decoded'}).items():
                info=zipfile.ZipInfo();info.filename=key;out.writestr(info,value)
        return path
    def test_inventory_is_read_only_and_does_not_claim_catalog_or_png_validity(self):
        p=self.jar();before=p.read_bytes();names=set(self.root.iterdir())
        report=modpack.inventory(self.root,include_textures=True)
        self.assertEqual(report['state'],'inventory-complete');self.assertFalse(report['catalogReady'])
        self.assertFalse(report['texturePayloadsVerified']);row=report['archives'][0]
        self.assertEqual(row['mods'][0]['modid'],'sample');self.assertEqual(row['blockTextureCandidates'],1)
        self.assertEqual(row['textures'][0]['resource'],'sample:blocks/stone')
        self.assertEqual(p.read_bytes(),before);self.assertEqual(set(self.root.iterdir()),names)
    def test_missing_and_bad_metadata_do_not_hide_texture_candidates(self):
        self.jar(entries={'assets/a/textures/blocks/one.png':b'x','mcmod.info':'not json'})
        row=modpack.inventory(self.root)['archives'][0]
        self.assertEqual(row['mods'],[]);self.assertEqual(row['textureCandidates'],1);self.assertEqual(len(row['warnings']),2)
        self.assertNotIn('textures',row)
    def test_conflicting_resources_have_no_invented_precedence(self):
        self.jar('a.jar');self.jar('b.jar')
        doc=modpack.inventory(self.root)
        self.assertEqual(doc['textureConflicts'],[{'resource':'sample:blocks/stone','archives':['a.jar','b.jar']}])
    def test_corrupt_archive_is_reported_while_other_archives_are_inspected(self):
        self.jar();(self.root/'broken.jar').write_bytes(b'not zip')
        doc=modpack.inventory(self.root);self.assertEqual(doc['state'],'partial');self.assertEqual(doc['archivesInspected'],1)
        self.assertEqual(doc['errors'][0]['file'],'broken.jar')
    def test_paths_duplicates_and_links_refused(self):
        for name in ['../bad','/absolute','a\\b']:
            with self.subTest(name=name):
                self.jar(entries={name:'bad'})
                self.assertEqual(modpack.inventory(self.root)['state'],'partial')
        p=self.jar(entries={'safe':'x'})
        with zipfile.ZipFile(p,'a') as out:
            info=zipfile.ZipInfo('linked');info.create_system=3;info.external_attr=0o120777<<16;out.writestr(info,'target')
        self.assertEqual(modpack.inventory(self.root)['state'],'partial')
    def test_limits_and_nested_jars(self):
        self.jar();(self.root/'nested').mkdir();(self.root/'nested/other.jar').write_bytes(b'ignored')
        self.assertEqual(modpack.inventory(self.root)['archivesFound'],1)
        with patch.object(modpack,'MAX_ENTRIES',1):self.assertEqual(modpack.inventory(self.root)['state'],'partial')
        with patch.object(modpack,'MAX_JARS',0):
            with self.assertRaises(ValueError):modpack.inventory(self.root)
        with patch.object(modpack,'MAX_INFO_BYTES',2):
            self.assertTrue(modpack.inventory(self.root)['archives'][0]['warnings'])
    @unittest.skipUnless(os.name=='posix','POSIX symlink')
    def test_external_archive_symlink_refused(self):
        p=self.jar();(self.root/'linked.jar').symlink_to(p)
        self.assertEqual(modpack.inventory(self.root)['state'],'partial')
    def test_metadata_wrapper(self):
        self.assertEqual(modpack.metadata(b'{"modList":[{"modid":"wrapped"}]}'),[{'modid':'wrapped'}])
    def test_unified_cli_and_failure_exit(self):
        import uiemap
        self.jar()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=uiemap.main(['modpack','--mods',str(self.root)])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['archivesInspected'],1)
        (self.root/'broken.jar').write_bytes(b'broken')
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(uiemap.main(['modpack','--mods',str(self.root)]),1)

if __name__=='__main__':unittest.main()
