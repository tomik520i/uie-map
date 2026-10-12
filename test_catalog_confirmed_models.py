"""Regression tests for the confirmed single-variant cube rules (offline_catalog.CONFIRMED_CUBES). Stdlib only.
    python -m unittest test_catalog_confirmed_models -v      (from work/uie-map)

Fixture records are copied from the production assets.json (qa-20261003/model-assets.json); when that file is
present next to work/uie-map, one test also resolves the real records.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import offline_catalog as oc
from offline_catalog import Catalog

PRODUCTION_ASSETS = Path(__file__).resolve().parent.parent / 'qa-20261003' / 'model-assets.json'
LIVE_AUDIT = Path(__file__).resolve().parent.parent / 'qa-20261008' / 'model-rollout-live.json'
MISSING_UV = (0.9, 0.9, 0.91, 0.91)
LAYOUT = {'registered': {}, 'missing': {'iconName': 'missingno', 'uv': list(MISSING_UV)}}

SIDE = ('tinker:smeltery_side', [0.2695337, 0.85937744, 0.27343506, 0.8632788])
INACTIVE = ('tinker:smeltery_inactive', [0.089846194, 0.85156494, 0.093747556, 0.8554663])
DRAIN_SIDE = ('tinker:drain_side', [0.03515869, 0.57812744, 0.03906006, 0.5820288])
DRAIN_OUT = ('tinker:drain_out', [0.48437744, 0.98437744, 0.4882788, 0.9882788])
BRICK = ('tinker:searedbrick', [0.36718994, 0.6367212, 0.3710913, 0.64062256])
PAVER = ('tinker:searedpaver', [0.097658694, 0.96875244, 0.101560056, 0.9726538])
CREEPER = ('tinker:searedcreeper', [0.3789087, 0.75781494, 0.38281006, 0.7617163])
STEEL = ('hbm:deco_steel', [0.011721191, 0.5664087, 0.015622559, 0.57031006])
# Production records of qa-20261008/model-rollout-live.json (live read-only audit).
RUSTY = ('hbm:deco_rusty_steel', [0.30468994, 0.75781494, 0.3085913, 0.7617163])
TUNGSTEN = ('hbm:deco_tungsten', [0.007814941, 0.6679712, 0.011716309, 0.67187256])
BLANK = ('blank_16', [0.15625244, 0.83593994, 0.1601538, 0.8398413])


def variant(meta, faces):
    return {'meta': meta, 'color': 16777215,
            'faces': [None if f is None else {'name': f[0], 'uv': list(f[1])} for f in faces]}


def record(rt, opaque, cls, variants):
    return {'renderType': rt, 'opaque': opaque, 'class': cls, 'variants': variants}


SMELTERY = record(387, True, 'tconstruct.smeltery.blocks.SmelteryBlock', [
    variant(0, [SIDE, SIDE, SIDE, INACTIVE, SIDE, SIDE]),
    variant(1, [DRAIN_SIDE, DRAIN_SIDE, DRAIN_SIDE, DRAIN_OUT, DRAIN_SIDE, DRAIN_SIDE]),
    variant(2, [BRICK] * 6),
    variant(3, [BRICK] * 6),
    variant(11, [PAVER, PAVER, CREEPER, CREEPER, CREEPER, CREEPER]),
    variant(12, [None] * 6),
])
DECO_STEEL = record(202, True, 'com.hbm.blocks.generic.BlockDecoCT', [variant(m, [STEEL] * 6) for m in range(16)])
DECO_RUSTY = record(202, True, 'com.hbm.blocks.generic.BlockDecoCT', [variant(m, [RUSTY] * 6) for m in range(16)])
DECO_TUNGSTEN = record(202, True, 'com.hbm.blocks.generic.BlockDecoCT', [variant(m, [TUNGSTEN] * 6) for m in range(16)])
HBM = {'hbm:tile.deco_steel': (DECO_STEEL, STEEL), 'hbm:tile.deco_rusty_steel': (DECO_RUSTY, RUSTY),
       'hbm:tile.deco_tungsten': (DECO_TUNGSTEN, TUNGSTEN)}
MULTIPART = record(709, False, 'codechicken.multipart.BlockMultipart', [variant(0, [BLANK] * 6)])


def assets(**extra):
    blocks = {'TConstruct:Smeltery': copy.deepcopy(SMELTERY), 'hbm:tile.deco_steel': copy.deepcopy(DECO_STEEL),
              'hbm:tile.deco_rusty_steel': copy.deepcopy(DECO_RUSTY),
              'hbm:tile.deco_tungsten': copy.deepcopy(DECO_TUNGSTEN),
              'ForgeMultipart:block': copy.deepcopy(MULTIPART)}
    blocks.update(extra)
    return {'blocks': blocks}


def catalog(**extra):
    return Catalog(assets(**extra), copy.deepcopy(LAYOUT))


def uv(face):
    return tuple(face[1])


class ConfirmedCubeTests(unittest.TestCase):
    def assert_confirmed(self, model, face, reason_part):
        self.assertEqual((model.kind, model.tier, model.opacity), ('cube', 'assumed', 'solid'))
        self.assertTrue(model.occludes)
        self.assertEqual(model.faces, [uv(face)] * 6)
        self.assertEqual((model.tint, model.color), ('none', (1.0, 1.0, 1.0)))
        self.assertIn(reason_part, model.reason)

    def assert_fallback(self, model, reason):
        self.assertEqual((model.kind, model.tier), ('fallback', 'fallback'))
        self.assertEqual(model.color, oc.FALLBACK_TINT)
        self.assertEqual(model.reason, reason)

    def test_smeltery_meta2_is_a_seared_brick_cube(self):
        self.assert_confirmed(catalog().model('TConstruct:Smeltery', 2), BRICK, 'SmelteryRender not reproduced')

    def test_other_smeltery_metas_keep_their_previous_models(self):
        cat = catalog()
        for meta in (0, 1, 3, 11):
            with self.subTest(meta=meta):
                self.assert_fallback(cat.model('TConstruct:Smeltery', meta),
                                     'no rule for renderType 387 (SmelteryBlock)')
        self.assertEqual(cat.model('TConstruct:Smeltery', 12).tier, 'invisible')
        self.assert_fallback(cat.model('TConstruct:Smeltery', 7), 'no rule for renderType 387 (SmelteryBlock)')

    def test_hbm_deco_steel_meta0_is_a_base_icon_cube_without_connected_textures(self):
        model = catalog().model('hbm:tile.deco_steel', 0)
        self.assert_confirmed(model, STEEL, 'connected textures')
        self.assertIn('not modelled', model.reason)
        self.assert_fallback(catalog().model('hbm:tile.deco_steel', 1), 'no rule for renderType 202 (BlockDecoCT)')

    def test_hbm_rusty_steel_and_tungsten_meta0_are_base_icon_cubes_only(self):
        cat = catalog()
        for name, (_, icon) in HBM.items():
            with self.subTest(name):
                model = cat.model(name, 0)
                self.assert_confirmed(model, icon, 'connected textures')
                self.assertIn('not modelled', model.reason)
                for meta in (1, 2, 15):                 # same icons on every meta, but only meta 0 is observed
                    self.assert_fallback(cat.model(name, meta), 'no rule for renderType 202 (BlockDecoCT)')

    def test_same_class_and_render_type_under_another_name_is_not_confirmed(self):
        cat = catalog(**{'hbm:tile.deco_titanium': copy.deepcopy(DECO_STEEL),
                         'hbm:tile.deco_steel_rusty': copy.deepcopy(DECO_RUSTY),
                         'hbm:deco_tungsten': copy.deepcopy(DECO_TUNGSTEN),
                         'HBM:tile.deco_tungsten': copy.deepcopy(DECO_TUNGSTEN),
                         'othermod:brick': copy.deepcopy(SMELTERY)})
        for name in ('hbm:tile.deco_titanium', 'hbm:tile.deco_steel_rusty', 'hbm:deco_tungsten', 'HBM:tile.deco_tungsten'):
            with self.subTest(name):
                self.assert_fallback(cat.model(name, 0), 'no rule for renderType 202 (BlockDecoCT)')
        self.assert_fallback(cat.model('othermod:brick', 2), 'no rule for renderType 387 (SmelteryBlock)')

    def test_changed_hbm_records_fall_back_instead_of_trusting_the_name(self):
        changes = {
            'renderType': lambda r: r.update(renderType=203),
            'class': lambda r: r.update({'class': 'com.hbm.blocks.generic.BlockGeneric'}),
            'not opaque': lambda r: r.update(opaque=False),
            'opaque not boolean': lambda r: r.update(opaque=1),
            'missing face': lambda r: r['variants'][0]['faces'].__setitem__(2, None),
            'meta 0 missing': lambda r: r.__setitem__('variants', r['variants'][1:]),
        }
        for name, (base, _) in HBM.items():
            for label, change in changes.items():
                with self.subTest(name=name, change=label):
                    changed = copy.deepcopy(base)
                    change(changed)
                    model = catalog(**{name: changed}).model(name, 0)
                    self.assertNotEqual(model.reason, oc.CONFIRMED_CUBES[(name, base['class'], 202)][1])
                    self.assertNotEqual((model.kind, model.tier), ('cube', 'assumed'))

    def test_render_type_numbers_are_not_globally_allowlisted(self):
        for rt in (387, 202, 709):
            self.assertNotIn(rt, oc.CUBE_RENDER_TYPES)
        cat = catalog(**{'mod:a': record(387, True, 'mod.A', [variant(0, [BRICK] * 6)]),
                         'mod:b': record(202, True, 'mod.B', [variant(0, [STEEL] * 6)])})
        self.assert_fallback(cat.model('mod:a', 0), 'no rule for renderType 387 (A)')
        self.assert_fallback(cat.model('mod:b', 0), 'no rule for renderType 202 (B)')

    def test_changed_record_falls_back_instead_of_trusting_the_name(self):
        changes = {
            'renderType': lambda r: r.update(renderType=388),
            'class': lambda r: r.update({'class': 'tconstruct.smeltery.blocks.SearedBlock'}),
            'not opaque': lambda r: r.update(opaque=False),
            'missing face': lambda r: r['variants'][2]['faces'].__setitem__(4, None),
        }
        for label, change in changes.items():
            with self.subTest(label):
                smeltery = copy.deepcopy(SMELTERY)
                change(smeltery)
                model = catalog(**{'TConstruct:Smeltery': smeltery}).model('TConstruct:Smeltery', 2)
                self.assertEqual(model.tier, 'fallback')
                self.assertEqual(model.color, oc.FALLBACK_TINT)

    def test_missing_meta_variant_does_not_borrow_another_variant(self):
        smeltery = copy.deepcopy(SMELTERY)
        smeltery['variants'] = [v for v in smeltery['variants'] if v['meta'] != 2]
        model = catalog(**{'TConstruct:Smeltery': smeltery}).model('TConstruct:Smeltery', 2)
        self.assertEqual(model.tier, 'fallback')

    def test_multipart_stays_an_unsupported_magenta_fallback(self):
        self.assert_fallback(catalog().model('ForgeMultipart:block', 0),
                             'no rule for renderType 709 (BlockMultipart)')

    def test_compiled_catalog_resolves_the_same_models(self):
        source = catalog()
        with tempfile.TemporaryDirectory() as tmp:
            assets_path, layout_path, out = Path(tmp, 'assets.json'), Path(tmp, 'layout.json'), Path(tmp, 'c.json')
            assets_path.write_text(json.dumps(assets()), encoding='utf-8')
            layout_path.write_text(json.dumps(LAYOUT), encoding='utf-8')
            Catalog.compile(assets_path, layout_path, out)
            compiled = Catalog.load_compiled(out)
        for name, meta in (('TConstruct:Smeltery', 2), ('TConstruct:Smeltery', 0), ('hbm:tile.deco_steel', 0),
                           ('hbm:tile.deco_steel', 1), ('hbm:tile.deco_rusty_steel', 0), ('hbm:tile.deco_rusty_steel', 1),
                           ('hbm:tile.deco_tungsten', 0), ('hbm:tile.deco_tungsten', 1), ('ForgeMultipart:block', 0)):
            with self.subTest(name=name, meta=meta):
                self.assertEqual(compiled.model(name, meta), source.model(name, meta))

    @unittest.skipUnless(PRODUCTION_ASSETS.is_file(), 'qa-20261003/model-assets.json not present')
    def test_production_records(self):
        doc = json.loads(PRODUCTION_ASSETS.read_text(encoding='utf-8'))
        cat = Catalog({'blocks': doc['blocks']}, copy.deepcopy(LAYOUT))
        self.assert_confirmed(cat.model('TConstruct:Smeltery', 2), BRICK, 'SmelteryRender')
        self.assert_confirmed(cat.model('hbm:tile.deco_steel', 0), STEEL, 'connected textures')
        self.assertEqual(cat.model('TConstruct:Smeltery', 0).tier, 'fallback')
        self.assertEqual(cat.model('ForgeMultipart:block', 0).tier, 'fallback')
        self.assertEqual(cat.model('mobdropcrops:Creeper Pod', 0).tier, 'fallback')

    @unittest.skipUnless(LIVE_AUDIT.is_file(), 'qa-20261008/model-rollout-live.json not present')
    def test_live_audit_records_match_fixtures_and_resolve(self):
        doc = json.loads(LIVE_AUDIT.read_text(encoding='utf-8'))
        blocks = doc['blocks']
        for name, (fixture, icon) in HBM.items():
            with self.subTest(name):
                live = blocks[name]
                # Guard: the rule was written against exactly these records; any drift must fail here first.
                self.assertEqual((live['renderType'], live['opaque'], live['class']),
                                 (fixture['renderType'], fixture['opaque'], fixture['class']))
                meta0 = next(v for v in live['variants'] if v['meta'] == 0)
                self.assertEqual(meta0['faces'], fixture['variants'][0]['faces'])
        cat = Catalog({'blocks': blocks}, copy.deepcopy(LAYOUT))
        for name, (_, icon) in HBM.items():
            with self.subTest(resolve=name):
                self.assert_confirmed(cat.model(name, 0), icon, 'connected textures')
                self.assertEqual(cat.model(name, 1).tier, 'fallback')
        self.assert_confirmed(cat.model('TConstruct:Smeltery', 2), BRICK, 'SmelteryRender')
        self.assertEqual(cat.model('TConstruct:Smeltery', 3).tier, 'fallback')


if __name__ == '__main__':
    unittest.main()
