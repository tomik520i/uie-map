import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

import modpack_textures as validation
import png_io
import uiemap_modpack


class TextureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.good = png_io.encode(2, 2, 4, b'\xff' * 16)

    def jar(self, entries, name='one.jar'):
        path = self.root / name
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for key, raw in entries.items(): archive.writestr(key, raw)
        return path

    def inventory(self):
        return uiemap_modpack.inventory(self.root, verify_block_textures=True, include_textures=True)

    def test_verified_payload_hash_and_dimensions_no_writes(self):
        path = self.jar({'assets/mod/textures/blocks/stone.png': self.good})
        before = path.read_bytes(); doc = self.inventory()
        texture = doc['archives'][0]['textures'][0]['validation']
        self.assertEqual(texture['state'], 'verified')
        self.assertEqual(texture['sha256'], hashlib.sha256(self.good).hexdigest())
        self.assertEqual((texture['width'], texture['height']), (2, 2))
        self.assertTrue(texture['staticSquareCandidate'])
        self.assertTrue(doc['blockTextureValidation']['complete'])
        self.assertFalse(doc['texturePayloadsVerified']); self.assertFalse(doc['catalogReady'])
        self.assertEqual(path.read_bytes(), before); self.assertEqual(list(self.root.iterdir()), [path])

    def test_inventory_default_still_does_not_decode_and_nonblock_not_checked(self):
        self.jar({'assets/mod/textures/blocks/bad.png': b'bad', 'assets/mod/textures/items/tool.png': b'bad'})
        plain = uiemap_modpack.inventory(self.root)
        self.assertEqual(plain['state'], 'inventory-complete')
        doc = self.inventory(); self.assertEqual(doc['state'], 'partial')
        self.assertEqual(doc['blockTextureValidation']['checked'], 1)
        self.assertEqual(doc['blockTextureValidation']['invalid'], 1)
        self.assertNotIn('validation', doc['archives'][0]['textures'][1])

    def test_animation_marker_and_nonsquare_are_not_static_candidates(self):
        self.jar({'assets/mod/textures/blocks/animated.png': self.good,
                  'assets/mod/textures/blocks/animated.png.mcmeta': b'{}',
                  'assets/mod/textures/blocks/strip.png': png_io.encode(2, 4, 4, b'\xff' * 32)})
        counts = self.inventory()['blockTextureValidation']
        self.assertEqual(counts['verified'], 2); self.assertEqual(counts['staticSquareCandidates'], 0)
        self.assertEqual(counts['animationMetadataPresent'], 1)

    def test_crc_and_truncated_stream_fail(self):
        bad = bytearray(self.good); bad[-5] ^= 1
        self.jar({'assets/mod/textures/blocks/crc.png': bytes(bad),
                  'assets/mod/textures/blocks/short.png': self.good[:-6]})
        self.assertEqual(self.inventory()['blockTextureValidation']['invalid'], 2)

    def test_palette_supported_but_interlace_is_separate_from_invalid(self):
        raw = png_io.SIGNATURE + png_io._chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 3, 0, 0, 0))
        raw += png_io._chunk(b'PLTE', b'\xff\x00\x00') + png_io._chunk(b'IDAT', zlib.compress(b'\0'*6)) + png_io._chunk(b'IEND', b'')
        interlaced = png_io.SIGNATURE + png_io._chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 6, 0, 0, 1))
        self.jar({'assets/mod/textures/blocks/palette.png': raw, 'assets/mod/textures/blocks/interlaced.png': interlaced})
        counts = self.inventory()['blockTextureValidation']
        self.assertEqual(counts['unsupported'], 1); self.assertEqual(counts['invalid'], 0)
        self.assertEqual(counts['verified'], 1)
        self.assertFalse(counts['complete'])

    def test_shared_budget_across_archives_and_entry_limit(self):
        for name in ['a.jar','b.jar']: self.jar({'assets/mod/textures/blocks/a.png': self.good}, name)
        with patch.object(validation, 'MAX_TOTAL_TEXTURE_BYTES', len(self.good)):
            counts = self.inventory()['blockTextureValidation']
        self.assertEqual(counts['verified'], 1); self.assertEqual(counts['limited'], 1)
        with patch.object(validation, 'MAX_TEXTURE_BYTES', 1):
            self.assertEqual(self.inventory()['blockTextureValidation']['limited'], 2)
        with patch.object(validation, 'MAX_TEXTURE_PIXELS', 1):
            self.assertEqual(self.inventory()['blockTextureValidation']['limited'], 2)

    def test_conflict_providers_are_both_validated_without_picking_precedence(self):
        for name in ['a.jar', 'b.jar']: self.jar({'assets/mod/textures/blocks/a.png': self.good}, name)
        doc = self.inventory()
        self.assertEqual(doc['textureConflicts'][0]['archives'], ['a.jar', 'b.jar'])
        self.assertEqual(doc['blockTextureValidation']['verified'], 2)

    def test_incomplete_archive_scan_cannot_claim_complete(self):
        self.jar({'assets/mod/textures/blocks/a.png': self.good})
        (self.root/'broken.jar').write_bytes(b'bad zip')
        doc = self.inventory()
        self.assertFalse(doc['blockTextureValidation']['complete']); self.assertEqual(doc['state'], 'partial')

    def test_cli_reports_partial_with_exit_one_and_summary_without_full_texture_list(self):
        import uiemap
        self.jar({'assets/mod/textures/blocks/a.png': b'bad'})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = uiemap.main(['modpack', '--mods', str(self.root), '--verify-block-textures'])
        doc = json.loads(output.getvalue())
        self.assertEqual(code, 1); self.assertNotIn('textures', doc['archives'][0])
        self.assertEqual(doc['archives'][0]['blockTextureValidation']['issues'][0]['entry'], 'assets/mod/textures/blocks/a.png')

    def test_zip_payload_crc_rejected(self):
        path = self.root/'badcrc.jar'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('assets/mod/textures/blocks/a.png', self.good)
        raw = bytearray(path.read_bytes()); start = raw.index(self.good); raw[start+40] ^= 1; path.write_bytes(raw)
        self.assertEqual(self.inventory()['blockTextureValidation']['invalid'], 1)


if __name__ == '__main__': unittest.main()
