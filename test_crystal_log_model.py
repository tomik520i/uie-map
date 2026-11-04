import copy
import json
from pathlib import Path
import tempfile
import unittest
from offline_catalog import Catalog

NAME='OreSpawn:OreSpawn_CrystalTreeLog'
TOP={'name':'OreSpawn:crystaltreelog_top','uv':[0.1,0.1,0.2,0.2]}
SIDE={'name':'OreSpawn:crystaltreelog','uv':[0.3,0.3,0.4,0.4]}
LAYOUT={'registered':{f['name']:{'uv':f['uv']} for f in (TOP,SIDE)},'missing':{'iconName':'missingno','uv':[0.9,0.9,1,1]}}
def assets():
    return {'blocks':{NAME:{'class':'danger.orespawn.BlockCrystalTreeLog','renderType':31,'opaque':False,
        'variants':[{'meta':m,'color':0xffffff,'faces':copy.deepcopy([TOP,TOP,SIDE,SIDE,SIDE,SIDE])} for m in (0,4,8,12)]}}}

class CrystalLogTests(unittest.TestCase):
    def test_upright_pillar_uses_real_textures_without_occluding_neighbours(self):
        model=Catalog(assets(),LAYOUT).model(NAME,0)
        self.assertEqual((model.kind,model.tier,model.opacity),('cube','assumed','cutout'))
        self.assertFalse(model.occludes)
        self.assertEqual(model.faces,[tuple(f['uv']) for f in (TOP,TOP,SIDE,SIDE,SIDE,SIDE)])
        self.assertEqual(model.color,(1,1,1))

    def test_unverified_or_changed_records_do_not_receive_the_rule(self):
        for change in (
            lambda b:b.update({'class':'other.BlockCrystalTreeLog'}),
            lambda b:b.update(renderType=0),
            lambda b:b['variants'][0]['faces'].__setitem__(0,None),
            lambda b:b['variants'][0]['faces'][0].update(name='other:top'),
            lambda b:b['variants'].pop(0),
        ):
            a=assets();change(a['blocks'][NAME]);model=Catalog(a,LAYOUT).model(NAME,0)
            self.assertNotIn('audited upright',model.reason)
        a=assets();a['blocks']['other:log']=a['blocks'].pop(NAME)
        self.assertEqual(Catalog(a,LAYOUT).model('other:log',0).tier,'fallback')

    def test_horizontal_variants_are_not_silently_promoted(self):
        cat=Catalog(assets(),LAYOUT)
        for meta in (4,8,12):self.assertEqual(cat.model(NAME,meta).tier,'fallback')

    def test_missing_registered_textures_are_not_promoted(self):
        layout=copy.deepcopy(LAYOUT);layout['registered'].clear()
        self.assertEqual(Catalog(assets(),layout).model(NAME,0).tier,'fallback')

    def test_compilation_preserves_the_same_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'assets.json').write_text(json.dumps(assets()));(p/'layout.json').write_text(json.dumps(LAYOUT))
            Catalog.compile(p/'assets.json',p/'layout.json',p/'catalog.json')
            cat=Catalog.load_compiled(p/'catalog.json')
            self.assertEqual(cat.model(NAME,0),Catalog(assets(),LAYOUT).model(NAME,0))

    def test_cutout_log_keeps_the_stone_face_below_it(self):
        from test_offline_renderer import volume,ASSETS,OfflineRendererTests
        from offline_mesher import Mesher
        a=assets();a['blocks']['minecraft:stone']=copy.deepcopy(ASSETS['blocks']['minecraft:stone'])
        v=volume({(1,5,1):(1,0),(1,6,1):(700,0)})
        v.registry[700]=NAME
        mesh=Mesher(Catalog(a,LAYOUT),None,caves=True).mesh_batch(v)
        face=tuple(sorted([(1,6,1),(2,6,1),(2,6,2),(1,6,2)]))
        self.assertIn(face,OfflineRendererTests.quads(mesh['chunks'][(0,0)]))
        self.assertNotIn((NAME,0),mesh['stats']['fallbackBlocks'])

if __name__=='__main__':unittest.main()
