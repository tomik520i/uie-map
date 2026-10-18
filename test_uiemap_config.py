import copy,json,tempfile,unittest
from pathlib import Path
import uiemap_config as c


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()
        for name in ('world/region','world/DIM-1/region','world/TROPICS/region','catalog'):(self.base/name).mkdir(parents=True)
        self.doc=dict(format=c.FORMAT,world=dict(id='my-world',path='world'),catalog='catalog',out='output',
                      dimensions=[dict(key='0',title='Hlavní svět',mode='surface'),dict(key='-1',title='Nether',mode='caves')])
    def check(self,doc=None):return c.validate(doc or self.doc,base=self.base)
    def test_distinct_namespaces_and_shared_registry_no_writes(self):
        before=set(self.base.rglob('*'));result=self.check();a,b=result['dimensions']
        self.assertNotEqual(a['outputs'],b['outputs']);self.assertEqual(a['levelDat'],b['levelDat'])
        self.assertEqual(a['regionPath'],str(self.base/'world/region'));self.assertEqual(b['regionPath'],str(self.base/'world/DIM-1/region'))
        self.assertFalse(result['renderReady']);self.assertTrue(result['readOnly']);self.assertEqual(before,set(self.base.rglob('*')))
        other=copy.deepcopy(self.doc);other['world']['id']='another-world'
        self.assertNotEqual(a['outputs'],self.check(other)['dimensions'][0]['outputs'])
    def test_explicit_selection_does_not_include_all_discovered(self):
        self.doc['dimensions']=[self.doc['dimensions'][0]]
        self.assertEqual([d['key'] for d in self.check()['dimensions']],['0'])
    def test_custom_path_and_title_changes_keep_namespace(self):
        self.doc['dimensions']=[dict(key='x:tropics',title='Tropy',mode='surface',path='TROPICS')]
        result=self.check()['dimensions'][0];self.doc['dimensions'][0]['title']='Jiný popisek'
        self.assertEqual(result['outputs'],self.check()['dimensions'][0]['outputs'])
        del self.doc['dimensions'][0]['path']
        with self.assertRaises(ValueError):self.check()
    def test_overlapping_output_refused(self):
        for out in ('.','world','world/render','catalog','catalog/render'):
            self.doc['out']=out
            with self.subTest(out=out),self.assertRaises(ValueError):self.check()
    def test_unsafe_world_ids_and_duplicate_dimensions(self):
        for value in ('../world','x/y','x:y','with spaces',''):
            self.doc['world']['id']=value
            with self.assertRaises(ValueError):self.check()
        self.doc['world']['id']='valid';self.doc['dimensions']*=2
        with self.assertRaises(ValueError):self.check()
    def test_limit_types_ranges_and_unknown_fields(self):
        for limits in ({'workers':True},{'workers':0},{'memoryMiB':127},{'minFreeGiB':19},{'unlimited':True}):
            self.doc['limits']=limits
            with self.assertRaises(ValueError):self.check()
        self.doc.pop('limits');self.doc['unknown']=1
        with self.assertRaises(ValueError):self.check()
    def test_web_settings_and_mode_validation(self):
        for web in ({'port':False},{'port':65536},{'host':'https://example.test'},{'host':'x;echo'}):
            self.doc['web']=web
            with self.assertRaises(ValueError):self.check()
        self.doc.pop('web');self.doc['dimensions'][1]['mode']='surface'
        self.assertTrue(any('Nether' in w['reason'] for w in self.check()['warnings']))
        self.doc['dimensions'][1]['mode']='ceiling'
        with self.assertRaises(ValueError):self.check()
    def test_json_relative_paths_duplicate_keys_size_and_nonfinite(self):
        p=self.base/'config.json';p.write_text(json.dumps(self.doc),encoding='utf-8')
        self.assertEqual(c.load(p)['world']['path'],str(self.base/'world'))
        for raw in ('{"world":1,"world":2}','{"x":NaN}',' '*65537):
            p.write_text(raw,encoding='utf-8')
            with self.assertRaises(ValueError):c.load(p)
    def test_absent_input_and_file_output(self):
        self.doc['catalog']='missing'
        with self.assertRaises(ValueError):self.check()
        self.doc['catalog']='catalog';(self.base/'output').write_bytes(b'not a directory')
        with self.assertRaises(ValueError):self.check()


if __name__=='__main__':unittest.main()
