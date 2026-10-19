import copy,json,tempfile,unittest
from pathlib import Path
from test_offline_renderer import ASSETS,LAYOUT
import catalog_import_plan as plan


class ImportPlanTests(unittest.TestCase):
    def test_valid_export_and_hashes_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a=root/'assets.json';b=root/'layout.json'
            a.write_text(json.dumps(ASSETS));b.write_text(json.dumps(LAYOUT))
            before={p:p.read_bytes() for p in root.iterdir()}
            result=plan.inspect(a,b)
            self.assertTrue(result['readOnly']);self.assertEqual(result['metadata']['blocks'],len(ASSETS['blocks']))
            self.assertEqual(result['metadata']['unregisteredFaceNames'],[])
            self.assertEqual(before,{p:p.read_bytes() for p in root.iterdir()})

    def test_missing_faces_duplicate_meta_and_bad_uv_rejected(self):
        for mode in ('faces','duplicate','uv','color'):
            assets=copy.deepcopy(ASSETS);block=assets['blocks']['minecraft:stone'];row=block['variants'][0]
            if mode=='faces':row['faces'].pop()
            if mode=='duplicate':block['variants'].append(copy.deepcopy(row))
            if mode=='uv':row['faces'][0]['uv']=[0,0,float('nan'),1]
            if mode=='color':row['color']=True
            with self.subTest(mode=mode),self.assertRaises(ValueError):plan.validate(assets,LAYOUT)

    def test_unknown_sprite_reported_without_dropping_block(self):
        assets=copy.deepcopy(ASSETS)
        assets['blocks']['minecraft:stone']['variants'][0]['faces'][0]['name']='other:missing'
        self.assertEqual(plan.validate(assets,LAYOUT)['unregisteredFaceNames'],['other:missing'])

    def test_java_signed_argb_colors_are_valid(self):
        assets=copy.deepcopy(ASSETS)
        for color in (-16767233,31491900,0xffffffff):
            assets['blocks']['minecraft:stone']['variants'][0]['color']=color
            plan.validate(assets,LAYOUT)

    def test_mirrored_texture_coordinates_are_valid(self):
        plan.uv([0.7,0.8,0.2,0.1])
        with self.assertRaises(ValueError):plan.uv([0.2,0.1,0.2,0.8])


if __name__=='__main__':unittest.main()
