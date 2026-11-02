import contextlib,copy,hashlib,io,json,tempfile,unittest,zipfile
from pathlib import Path
import modpack_rules as rules
import png_io
from test_reader import level_dat


class RuleTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name).resolve()
        self.mods=self.root/'mods';self.mods.mkdir();self.world=self.root/'world';self.world.mkdir()
        (self.world/'level.dat').write_bytes(level_dat([('\x01sample:stone',51),('\x01other:stone',52)]))
        self.raw=png_io.encode(2,2,4,b'\xff'*16);self.entry='assets/sample/textures/blocks/stone.png'
        self.jar('sample.jar',self.raw)
        self.doc=dict(format=rules.FORMAT,textures={'stone':dict(archive='sample.jar',entry=self.entry,sha256=hashlib.sha256(self.raw).hexdigest())},
                      blocks=[dict(name='sample:stone',metadata=[0,2],model='cube',opacity='solid',faces=['stone']*6,evidence='Synthetic cube fixture')])
        self.path=self.root/'rules.json'

    def jar(self,name,raw,animated=False):
        with zipfile.ZipFile(self.mods/name,'w') as z:
            z.writestr(self.entry,raw)
            if animated:z.writestr(self.entry+'.mcmeta','{}')

    def inspect(self,doc=None):
        self.path.write_text(json.dumps(doc or self.doc),encoding='utf-8')
        return rules.inspect(self.mods,self.world,self.path)

    def test_exact_metadata_registry_ids_and_no_write(self):
        before={p.name:p.read_bytes() for p in self.mods.iterdir()}
        d=self.inspect()
        self.assertEqual([v['metadata'] for v in d['variants']],[0,2]);self.assertEqual(d['variants'][0]['id'],51)
        self.assertEqual(d['unlistedMetadataPolicy'],'fallback')
        self.assertFalse(d['catalogReady']);self.assertFalse(d['modelSemanticsVerified'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.mods.iterdir()})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['mods','rules.json','world'])

    def test_face_order_and_identity_determinism(self):
        second=png_io.encode(2,2,4,b'\x80'*16);self.jar('second.jar',second)
        self.doc['textures']['other']=dict(archive='second.jar',entry=self.entry,sha256=hashlib.sha256(second).hexdigest())
        self.doc['blocks'][0]['faces']=['stone','other','stone','stone','other','other']
        first=self.inspect(); self.doc['blocks'][0]['metadata']=[2,0]
        self.assertEqual(first['bindingSha256'],self.inspect()['bindingSha256'])
        self.assertEqual(first['variants'][0]['faces'],['stone','other','stone','stone','other','other'])
        self.doc['blocks'][0]['faces'].reverse()
        self.assertNotEqual(first['bindingSha256'],self.inspect()['bindingSha256'])

    def test_selected_provider_wins_only_by_explicit_choice(self):
        self.jar('other.jar',png_io.encode(2,2,4,b'\x80'*16))
        self.assertEqual(self.inspect()['textures']['stone']['archive'],'sample.jar')
        self.doc['textures']['stone']['archive']='other.jar'
        with self.assertRaisesRegex(ValueError,'hash changed'):self.inspect()

    def test_overlaps_bools_out_of_range_and_implicit_metadata_rejected(self):
        for metas in ([0,0],[True],[-1],[16],[],['*']):
            d=copy.deepcopy(self.doc);d['blocks'][0]['metadata']=metas
            with self.subTest(metas=metas),self.assertRaises(ValueError):self.inspect(d)
        self.doc['blocks'].append(copy.deepcopy(self.doc['blocks'][0]))
        with self.assertRaisesRegex(ValueError,'Overlapping'):self.inspect()

    def test_unknown_registry_missing_entry_bad_hash_and_animated(self):
        d=copy.deepcopy(self.doc);d['blocks'][0]['name']='sample:typo'
        with self.assertRaisesRegex(ValueError,'absent'):self.inspect(d)
        d=copy.deepcopy(self.doc);d['textures']['stone']['entry']=self.entry.replace('stone','other')
        with self.assertRaisesRegex(ValueError,'absent'):self.inspect(d)
        self.jar('sample.jar',self.raw,True)
        with self.assertRaisesRegex(ValueError,'static square'):self.inspect()

    def test_invalid_png_and_nonsquare_refused(self):
        self.jar('sample.jar',b'bad')
        with self.assertRaisesRegex(ValueError,'not verified'):self.inspect()
        raw=png_io.encode(2,4,4,b'\xff'*32);self.jar('sample.jar',raw)
        self.doc['textures']['stone']['sha256']=hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError,'static square'):self.inspect()

    def test_paths_unknown_fields_and_evidence(self):
        for key,value in [('archive','../outside.jar'),('entry','assets/sample/textures/blocks/../stone.png'),('sha256','a')]:
            d=copy.deepcopy(self.doc);d['textures']['stone'][key]=value
            with self.assertRaises(ValueError):self.inspect(d)
        for key,value in [('extra',True),('faces',['stone']*5),('evidence',' '),('model','stairs'),('opacity','translucent')]:
            d=copy.deepcopy(self.doc);d['blocks'][0][key]=value
            with self.assertRaises(ValueError):self.inspect(d)
        d=copy.deepcopy(self.doc);d['textures']['unused']=d['textures']['stone']
        with self.assertRaisesRegex(ValueError,'Unused'):self.inspect(d)

    def test_duplicate_keys_and_size_limits(self):
        self.path.write_text('{"format":1,"format":2}')
        with self.assertRaisesRegex(ValueError,'duplicate'):rules.load(self.path)
        self.path.write_text(' '*(2*1024*1024+1))
        with self.assertRaisesRegex(ValueError,'larger'):rules.load(self.path)

    def test_cli_and_corrupt_archive_exit(self):
        import uiemap
        self.inspect();out=io.StringIO()
        args=['modpack-rules','--mods',str(self.mods),'--world',str(self.world),'--rules',str(self.path)]
        with contextlib.redirect_stdout(out):code=uiemap.main(args)
        self.assertEqual(code,0);self.assertEqual(json.loads(out.getvalue())['state'],'bindings-validated')
        (self.mods/'sample.jar').write_bytes(b'bad')
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as err:uiemap.main(args)
        self.assertEqual(err.exception.code,2)


if __name__=='__main__':unittest.main()
