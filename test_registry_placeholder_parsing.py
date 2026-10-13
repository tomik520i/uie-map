"""Regression: world_hires._batch must add placeholders ONLY for the block ids that are really missing.

capture_batch.from_chunks reports missing ids as "<id> at (<x>, <y>, <z>)" joined by ", " (see FORMAT_SOURCE). The
coordinate tuple itself contains ", ", so splitting the list on ", " yields bare coordinate parts; a y value is all
digits and was taken as a block id, overwriting a VALID registry entry (e.g. 90 = minecraft:portal at y=90) with
"uie:unknown_90" for the rest of the worker's life. That makes output depend on worker history.

These tests use a stub batch module that raises the REAL message format once and then succeeds; nothing is rendered.
The frozen pre-fix world_hires._batch fails three of the first six tests; the fix (offline_volume.missing_registry_ids,
strict whole-payload grammar, bounded ids, setdefault) must pass all. GrammarTests exercise the helper directly.
    python3 -m unittest test_registry_placeholder_parsing -v
"""
from pathlib import Path
from types import SimpleNamespace
import unittest

import capture_batch
import offline_volume
import world_hires as wh

FORMAT_SOURCE = 'f"{i} at {first_seen[i]}" for i in unknown[:10]'
SENTINEL = object()


def missing_message(key, first_seen):
    """Exactly capture_batch.from_chunks' message for the ids of first_seen that are missing."""
    unknown = sorted(first_seen)
    return (f"chunk {key} uses block ids missing from the registry: "
            + ", ".join(f"{i} at {first_seen[i]}" for i in unknown[:10]))


class StubBatch:
    """from_chunks raises BatchError for ids not yet in the registry it receives, then returns SENTINEL."""

    BatchError = capture_batch.BatchError

    def __init__(self, first_seen, key=(0, -2)):
        self.first_seen, self.key, self.calls = dict(first_seen), key, []

    def from_chunks(self, area, core, registry, window):
        self.calls.append(dict(registry))
        missing = {i: at for i, at in self.first_seen.items() if i not in registry}
        if missing:
            raise capture_batch.BatchError(missing_message(self.key, missing))
        return SENTINEL


class PlaceholderParsingTests(unittest.TestCase):
    def setUp(self):
        self.saved = wh._W.get('registry', None)
        self.addCleanup(self.restore)

    def restore(self):
        if self.saved is None:
            wh._W.pop('registry', None)
        else:
            wh._W['registry'] = self.saved

    def run_batch(self, registry, first_seen):
        wh._W['registry'] = dict(registry)
        stub = StubBatch(first_seen)
        result = wh._batch(stub, None, None, {})
        return result, wh._W['registry'], stub

    def test_message_format_matches_capture_batch(self):
        source = Path(capture_batch.__file__).read_text(encoding='utf-8')
        self.assertIn(FORMAT_SOURCE, source, 'stub must use the real from_chunks message format')
        self.assertIn('missing from the registry: ', source)

    def test_y_coordinate_never_overwrites_a_valid_registry_entry(self):
        registry = {1: 'minecraft:stone', 90: 'minecraft:portal'}
        result, after, stub = self.run_batch(registry, {999: (1, 90, 2)})
        self.assertIs(result, SENTINEL)
        self.assertEqual(after[90], 'minecraft:portal', 'y=90 must not be read as block id 90')
        self.assertEqual(after[999], 'uie:unknown_999')
        self.assertEqual(set(after) - set(registry), {999}, 'no placeholder for coordinate values')
        self.assertEqual(len(stub.calls), 2)

    def test_several_missing_ids_with_negative_coordinates(self):
        registry = {5: 'minecraft:planks', 64: 'minecraft:wooden_door', 90: 'minecraft:portal'}
        first_seen = {999: (-5, 64, -7), 1000: (12, 5, -300), 4095: (-1, 90, 0)}
        result, after, _ = self.run_batch(registry, first_seen)
        self.assertIs(result, SENTINEL)
        for block_id, name in registry.items():
            self.assertEqual(after[block_id], name, f'valid id {block_id} preserved')
        self.assertEqual({k: v for k, v in after.items() if k not in registry},
                         {999: 'uie:unknown_999', 1000: 'uie:unknown_1000', 4095: 'uie:unknown_4095'})

    def test_coordinates_absent_from_the_registry_are_not_added(self):
        registry = {90: 'minecraft:portal'}
        _, after, _ = self.run_batch(registry, {999: (200, 37, 41)})
        self.assertEqual(set(after), {90, 999}, 'x/y/z values must not become placeholder ids')

    def test_missing_id_equal_to_a_coordinate_is_still_added_once(self):
        _, after, stub = self.run_batch({}, {64: (3, 64, 64)})
        self.assertEqual(after, {64: 'uie:unknown_64'})
        self.assertEqual(len(stub.calls), 2)

    def test_unrelated_batch_errors_still_raise(self):
        wh._W['registry'] = {}
        stub = SimpleNamespace(BatchError=capture_batch.BatchError,
                               from_chunks=lambda *a: (_ for _ in ()).throw(capture_batch.BatchError('other problem')))
        with self.assertRaises(capture_batch.BatchError):
            wh._batch(stub, None, None, {})
        self.assertEqual(wh._W['registry'], {})

    # ------------------------------------------------------------------ task 221

    def raising(self, message, *, calls):
        def from_chunks(*args):
            calls.append(1)
            raise capture_batch.BatchError(message)
        return SimpleNamespace(BatchError=capture_batch.BatchError, from_chunks=from_chunks)

    def test_message_naming_only_known_ids_raises_instead_of_looping(self):
        wh._W['registry'] = {999: 'mod:real_block'}
        calls = []
        stub = self.raising(missing_message((0, 0), {999: (1, 2, 3)}), calls=calls)
        with self.assertRaises(capture_batch.BatchError):
            wh._batch(stub, None, None, {})
        self.assertEqual(calls, [1], 'no repair possible: raised after one attempt')
        self.assertEqual(wh._W['registry'], {999: 'mod:real_block'}, 'a known id is never replaced')

    def test_known_id_listed_with_a_missing_one_keeps_its_name(self):
        registry = {999: 'mod:real_block', 90: 'minecraft:portal'}
        wh._W['registry'] = dict(registry)
        first = missing_message((0, 0), {999: (1, 90, 3), 1000: (4, 5, 6)})
        seen = []

        def from_chunks(area, core, reg, window):
            seen.append(dict(reg))
            if 1000 not in reg:
                raise capture_batch.BatchError(first)
            return SENTINEL
        result = wh._batch(SimpleNamespace(BatchError=capture_batch.BatchError, from_chunks=from_chunks),
                           None, None, {})
        self.assertIs(result, SENTINEL)
        self.assertEqual(wh._W['registry'], {**registry, 1000: 'uie:unknown_1000'})

    def test_malformed_or_out_of_range_payload_is_never_partially_applied(self):
        for message in ('chunk (0, 0) uses block ids missing from the registry: 999 at (1, 2, 3), 65536 at (1, 2, 3)',
                        'chunk (0, 0) uses block ids missing from the registry: 999 at (1, 2, 3) and more',
                        'column (1,2) uses block ids missing from the registry: [999, 1000]'):
            with self.subTest(message=message):
                wh._W['registry'] = {90: 'minecraft:portal'}
                calls = []
                with self.assertRaises(capture_batch.BatchError):
                    wh._batch(self.raising(message, calls=calls), None, None, {})
                self.assertEqual(wh._W['registry'], {90: 'minecraft:portal'}, 'nothing added from a rejected message')

    def test_repair_rounds_are_bounded(self):
        wh._W['registry'] = {90: 'minecraft:portal'}
        counter = iter(range(5000, 5100))

        def from_chunks(*args):
            raise capture_batch.BatchError(missing_message((0, 0), {next(counter): (1, 90, 2)}))
        with self.assertRaises(wh.HiresError):
            wh._batch(SimpleNamespace(BatchError=capture_batch.BatchError, from_chunks=from_chunks), None, None, {})
        added = {k: v for k, v in wh._W['registry'].items() if k != 90}
        self.assertEqual(sorted(added), list(range(5000, 5008)), 'exactly eight repair rounds')
        self.assertEqual(wh._W['registry'][90], 'minecraft:portal')


class GrammarTests(unittest.TestCase):
    """offline_volume.missing_registry_ids: whole-payload grammar, bounded ids, known ids filtered."""

    MARK = 'chunk (0, -2) uses block ids missing from the registry: '

    def ids(self, payload, registry=None):
        return offline_volume.missing_registry_ids(self.MARK + payload, registry or {})

    def test_valid_payloads(self):
        self.assertEqual(self.ids('999 at (1, 90, 2)'), (999,))
        self.assertEqual(self.ids('5 at (-1, 0, -2), 70 at (0, 255, 0), 65535 at (-30000000, 1, 29999999)'),
                         (5, 70, 65535))
        self.assertEqual(self.ids('0 at (0, 0, 0)'), (0,), 'lower bound')
        self.assertEqual(self.ids('7 at (1, 2, 3), 7 at (4, 5, 6)'), (7,), 'duplicates once')
        self.assertEqual(self.ids('7 at (1, 2, 3), 8 at (4, 5, 6)', {7: 'known'}), (8,), 'known ids filtered')

    def test_malformed_payloads_return_nothing(self):
        bad = ['', '999', '999 at (1, 2)', '999 at (1, 2, 3, 4)', '999 at 1, 2, 3', '999 at (1,2,3)',
               '999 at (1, 2, 3),', '999 at (1, 2, 3), ', ' 999 at (1, 2, 3)', '999 at (1, 2, 3) ',
               'x999 at (1, 2, 3)', '-5 at (1, 2, 3)', '9.5 at (1, 2, 3)', '999 at (1.5, 2, 3)',
               '999 at (1, 2, 3); 1000 at (1, 2, 3)', '999 at (1, 2, 3)\n1000 at (1, 2, 3)']
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self.ids(payload), ())

    def test_out_of_range_rejects_the_whole_message(self):
        self.assertEqual(self.ids('65536 at (1, 2, 3)'), ())
        self.assertEqual(self.ids('999 at (1, 2, 3), 70000 at (1, 2, 3)'), (), 'no partial result')

    def test_unrelated_messages_and_other_formats(self):
        self.assertEqual(offline_volume.missing_registry_ids('chunk (0, 0) has no Biomes array', {}), ())
        self.assertEqual(offline_volume.missing_registry_ids(
            'column (1,2) uses block ids missing from the registry: [999]', {}), ())

    def test_helper_lives_in_the_identity_hashed_module(self):
        source = Path(wh.__file__).read_text(encoding='utf-8')
        self.assertIn('from offline_volume import missing_registry_ids', source)
        self.assertIn('setdefault(block_id', source)
        self.assertNotIn('split(", ")', source, 'no ad-hoc splitting of the payload in world_hires')


if __name__ == '__main__':
    unittest.main()
