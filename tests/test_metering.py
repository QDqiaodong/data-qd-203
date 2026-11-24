"""Acceptance and oracle tests for local metering and reference alignment.

Every input is synthetic; no decoder is invoked by the code under test -- the
tests synthesize single-frame evidence through the existing public
``process`` API the way an upstream publisher would, then bind it into a point
catalogue.  The acceptance stories from the data contract are reproduced
exactly, plus an independent rational oracle, tamper/rejection coverage and a
10**12 microsecond span case proving output size does not grow with span.
"""
import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from telemetry_trellis import (
    AlignmentError,
    MeteringError,
    align_meter_facts,
    build_meter_facts,
    evidence_digest,
    process,
    validate_facts,
)
from telemetry_trellis.code import encode

SRC_PATH = str(Path(__file__).resolve().parents[1] / 'src')

CONSTRAINT = 2
GENERATORS = [3, 1]
BITS = 8
INLET = 'meter-in'
OUTLET = 'meter-out'


# ------------------------------------------------------------- synthetic input
def unique_frame(frame_id, reading, *, bits=BITS):
    """A unique-decode frame result carrying ``reading`` MSB-first."""
    payload = [(reading >> (bits - 1 - k)) & 1 for k in range(bits)]
    received = encode(payload, CONSTRAINT, GENERATORS, True)['symbols']
    data = {'constraint': CONSTRAINT, 'generators': GENERATORS,
            'frames': [{'id': frame_id, 'payload_length': bits,
                        'terminated': True, 'received': received}]}
    frame = process(data)['frames'][0]
    assert frame['optimal_paths'] == 1 and not frame['ambiguous']
    return frame


def ambiguous_frame(frame_id):
    """A genuine tie: fully erased, unterminated, two optimal paths."""
    data = {'constraint': CONSTRAINT, 'generators': GENERATORS,
            'frames': [{'id': frame_id, 'payload_length': 1,
                        'terminated': False, 'received': [None, None]}]}
    frame = process(data)['frames'][0]
    assert frame['optimal_paths'] == 2 and frame['ambiguous']
    return frame


def point(meter_id, side, index, tick, frame):
    frame_id = frame['id']
    return {'meter_id': meter_id, 'tick': tick, 'frame_id': frame_id,
            'evidence_digest': evidence_digest(meter_id, tick, frame_id,
                                               frame)}


def build_batch(inlet_spec, outlet_spec, *, frames_by_side=None,
                skip_frames=()):
    """Build a catalogue plus matching evidence from (tick, reading) specs.

    ``frames_by_side`` optionally replaces a side's synthesized frame at a
    given index (e.g. an ambiguous tie).  ``skip_frames`` drops evidence
    entries while keeping the catalogue points, simulating late evidence.
    """
    points = []
    evidence = []
    frames_by_side = frames_by_side or {}
    for meter_id, spec, side in ((INLET, inlet_spec, 'inlet'),
                                 (OUTLET, outlet_spec, 'outlet')):
        for index, (tick, reading) in enumerate(spec):
            frame_id = f'{side}-{index}'
            override = frames_by_side.get((side, index))
            frame = override if override is not None else unique_frame(
                frame_id, reading)
            points.append(point(meter_id, side, index, tick, frame))
            if frame_id not in skip_frames:
                evidence.append(frame)
    catalog = {'inlet_meter_id': INLET, 'outlet_meter_id': OUTLET,
               'points': points}
    return catalog, evidence


def anchor_list(spec):
    return [{'tick': t, 'reference_time': r, 'interpolate': flag}
            for t, r, flag in spec]


def timing(in_spec, out_spec):
    return {'inlet_meter_id': INLET, 'outlet_meter_id': OUTLET,
            'inlet_anchors': anchor_list(in_spec),
            'outlet_anchors': anchor_list(out_spec)}


def F(node):
    return Fraction(node['num'], node['den'])


INLET_SPEC = [(0, 0), (6, 12), (12, 24)]
OUTLET_SPEC = [(0, 0), (4, 4), (10, 10), (16, 16)]
BASE_IN_ANCHORS = [(0, 0, False), (6, 6, True), (12, 18, True)]
BASE_OUT_ANCHORS = [(0, 2, False), (16, 18, True)]


def base_result(timing_in=BASE_IN_ANCHORS, timing_out=BASE_OUT_ANCHORS):
    catalog, evidence = build_batch(INLET_SPEC, OUTLET_SPEC)
    facts = build_meter_facts(catalog, evidence)
    return facts, align_meter_facts(facts, timing(timing_in, timing_out))


# ----------------------------------------------------------------- acceptance 1
class AcceptanceOneTests(unittest.TestCase):
    def test_paired_ranges_shares_and_provenance(self):
        facts, result = base_result()
        self.assertEqual(
            [(F(r['reference_range']['start']),
              F(r['reference_range']['end'])) for r in result['paired']],
            [(Fraction(2), Fraction(6)), (Fraction(6), Fraction(12)),
             (Fraction(12), Fraction(18))])
        self.assertEqual(
            [F(r['inlet']['share_ml']) for r in result['paired']],
            [Fraction(8), Fraction(6), Fraction(6)])
        self.assertEqual(
            [F(r['outlet']['share_ml']) for r in result['paired']],
            [Fraction(4), Fraction(6), Fraction(6)])
        first = result['paired'][0]
        self.assertEqual(first['inlet']['fact_id'], 'inlet-fact-1')
        self.assertEqual(first['outlet']['fact_id'], 'outlet-fact-1')
        self.assertEqual(
            first['inlet']['endpoint_frame_ids'],
            {'start': 'inlet-0', 'end': 'inlet-1'})
        self.assertEqual(
            first['outlet']['endpoint_frame_ids'],
            {'start': 'outlet-0', 'end': 'outlet-1'})
        self.assertEqual(first['inlet']['meter_id'], INLET)
        self.assertEqual(first['outlet']['meter_id'], OUTLET)
        # Exact occupied local sub-ranges, not just totals.
        self.assertEqual(
            [(F(r['inlet']['local_range']['start']),
              F(r['inlet']['local_range']['end'])) for r in result['paired']],
            [(Fraction(2), Fraction(6)), (Fraction(6), Fraction(9)),
             (Fraction(9), Fraction(12))])
        self.assertEqual(
            [(F(r['outlet']['local_range']['start']),
              F(r['outlet']['local_range']['end'])) for r in result['paired']],
            [(Fraction(0), Fraction(4)), (Fraction(4), Fraction(10)),
             (Fraction(10), Fraction(16))])
        # Anchor provenance: every pair names the segment used by each side.
        for record in result['paired']:
            for side in ('inlet', 'outlet'):
                segment = record[side]['anchor_segment']
                self.assertEqual(segment['side'], side)
                self.assertTrue(segment['end_tick'] > segment['start_tick'])
        # Exactly one unpaired share: the inlet [0,2) head, 4 ml.
        self.assertEqual(len(result['unpaired']), 1)
        head = result['unpaired'][0]
        self.assertEqual(head['type'], 'calibrated_unpaired')
        self.assertEqual(head['side'], 'inlet')
        self.assertEqual((F(head['reference_range']['start']),
                          F(head['reference_range']['end'])),
                         (Fraction(0), Fraction(2)))
        self.assertEqual(F(head['share_ml']), Fraction(4))
        self.assertEqual(result['unknown_facts'], [])

    def test_facts_document_shape_and_order(self):
        facts, _ = base_result()
        self.assertEqual(facts['kind'], 'telemetry_trellis.meter_facts')
        self.assertEqual([f['side'] for f in facts['facts']],
                         ['inlet', 'inlet', 'outlet', 'outlet', 'outlet'])
        self.assertEqual(
            [f['local_range'] for f in facts['facts']],
            [{'start': 0, 'end': 6}, {'start': 6, 'end': 12},
             {'start': 0, 'end': 4}, {'start': 4, 'end': 10},
             {'start': 10, 'end': 16}])
        self.assertTrue(all(f['status'] == 'known' for f in facts['facts']))
        self.assertEqual(
            [f['increment_ml'] for f in facts['facts']],
            [12, 12, 4, 6, 6])


# ----------------------------------------------------------------- acceptance 2
class AcceptanceTwoTests(unittest.TestCase):
    def test_moved_middle_anchor_splits_shares_exactly(self):
        moved_in = [(0, 0, False), (6, 8, True), (12, 18, True)]
        _, result = base_result(moved_in)
        self.assertEqual(
            [(F(r['reference_range']['start']),
              F(r['reference_range']['end'])) for r in result['paired']],
            [(Fraction(2), Fraction(6)), (Fraction(6), Fraction(8)),
             (Fraction(8), Fraction(12)), (Fraction(12), Fraction(18))])
        self.assertEqual(
            [F(r['inlet']['share_ml']) for r in result['paired']],
            [Fraction(6), Fraction(3), Fraction(24, 5), Fraction(36, 5)])
        first = result['paired'][0]
        self.assertEqual(
            (F(first['inlet']['local_range']['start']),
             F(first['inlet']['local_range']['end'])),
            (Fraction(3, 2), Fraction(9, 2)))
        # The 3 ml head is calibrated but never meets outlet coverage.
        heads = [r for r in result['unpaired']
                 if r['type'] == 'calibrated_unpaired']
        self.assertEqual(len(heads), 1)
        self.assertEqual(F(heads[0]['share_ml']), Fraction(3))
        self.assertEqual((F(heads[0]['reference_range']['start']),
                          F(heads[0]['reference_range']['end'])),
                         (Fraction(0), Fraction(2)))
        self.assertEqual((F(heads[0]['local_range']['start']),
                          F(heads[0]['local_range']['end'])),
                         (Fraction(0), Fraction(3, 2)))

    def test_disabled_line_keeps_volume_local_and_breaks_pairs(self):
        disabled_in = [(0, 0, False), (6, 6, True), (12, 18, False)]
        _, result = base_result(disabled_in)
        # Only the still-calibrated first inlet fact can pair.
        self.assertEqual(
            [(F(r['reference_range']['start']),
              F(r['reference_range']['end'])) for r in result['paired']],
            [(Fraction(2), Fraction(6))])
        uncalibrated = [r for r in result['unpaired']
                        if r['type'] == 'uncalibrated']
        self.assertEqual(len(uncalibrated), 1)
        block = uncalibrated[0]
        self.assertEqual(block['side'], 'inlet')
        self.assertEqual(block['fact_id'], 'inlet-fact-2')
        self.assertEqual(block['local_range'],
                         {'start': {'num': 6, 'den': 1},
                          'end': {'num': 12, 'den': 1}})
        self.assertEqual(F(block['share_ml']), Fraction(12))
        self.assertIsNone(block['reference_range'])
        self.assertIsNone(block['anchor_segment'])
        # Outlet [6,18) survives as calibrated but unpaired, not zero flow.
        calibrated = [r for r in result['unpaired']
                      if r['type'] == 'calibrated_unpaired'
                      and r['side'] == 'outlet']
        self.assertEqual(
            [(F(r['reference_range']['start']),
              F(r['reference_range']['end']), F(r['share_ml']))
             for r in calibrated],
            [(Fraction(6), Fraction(12), Fraction(6)),
             (Fraction(12), Fraction(18), Fraction(6))])


# ----------------------------------------------------------------- acceptance 3
class AcceptanceThreeTests(unittest.TestCase):
    def test_missing_evidence_never_bridges_points(self):
        catalog, evidence = build_batch(
            INLET_SPEC, OUTLET_SPEC, skip_frames=('inlet-1',))
        facts = build_meter_facts(catalog, evidence)
        inlet_facts = [f for f in facts['facts'] if f['side'] == 'inlet']
        self.assertTrue(all(f['status'] == 'unknown' for f in inlet_facts))
        self.assertEqual(
            [f['unknown_reason'] for f in inlet_facts],
            ['missing_evidence', 'missing_evidence'])
        result = align_meter_facts(facts, timing(BASE_IN_ANCHORS,
                                                 BASE_OUT_ANCHORS))
        self.assertEqual(result['paired'], [])
        self.assertEqual(
            sorted(f['fact_id'] for f in result['unknown_facts']),
            ['inlet-fact-1', 'inlet-fact-2'])
        outlet = [r for r in result['unpaired']
                  if r['side'] == 'outlet']
        self.assertEqual(
            sum(F(r['share_ml']) for r in outlet), Fraction(16))
        self.assertTrue(all(r['type'] == 'calibrated_unpaired'
                            for r in outlet))
        # Unknown facts are never apportioned.
        referenced = {r['fact_id'] for r in result['unpaired']}
        self.assertFalse(referenced & {'inlet-fact-1', 'inlet-fact-2'})

    def test_ambiguous_evidence_changes_reason_not_increment(self):
        tie = ambiguous_frame('inlet-1')
        catalog, evidence = build_batch(
            INLET_SPEC, OUTLET_SPEC,
            frames_by_side={('inlet', 1): tie})
        facts = build_meter_facts(catalog, evidence)
        inlet_facts = [f for f in facts['facts'] if f['side'] == 'inlet']
        self.assertEqual(
            [f['unknown_reason'] for f in inlet_facts],
            ['ambiguous_evidence', 'ambiguous_evidence'])
        ambiguous_endpoint = inlet_facts[0]['endpoints']['end']
        self.assertEqual(ambiguous_endpoint['status'], 'ambiguous_evidence')
        self.assertIsNone(ambiguous_endpoint['reading'])

    def test_internally_consistent_but_rebound_fields_fail_digest(self):
        tie = ambiguous_frame('inlet-1')
        catalog, evidence = build_batch(
            INLET_SPEC, OUTLET_SPEC,
            frames_by_side={('inlet', 1): tie})
        # Keep the catalogue (its digest binds the ambiguous binding) but
        # mutate the frame into a unique-looking one.
        tampered = copy.deepcopy(evidence)
        for frame in tampered:
            if frame['id'] == 'inlet-1':
                frame['optimal_paths'] = 1
                frame['ambiguous'] = False
        with self.assertRaises(MeteringError):
            build_meter_facts(catalog, tampered)

    def test_wrong_frame_evidence_is_rejected(self):
        catalog, evidence = build_batch(INLET_SPEC, OUTLET_SPEC)
        foreign = unique_frame('inlet-1', 99)  # same id, different payload
        replaced = [foreign if f['id'] == 'inlet-1' else f
                    for f in evidence]
        with self.assertRaises(MeteringError):
            build_meter_facts(catalog, replaced)
        # A stale result for an unknown frame is rejected too, never treated
        # as missing evidence.
        stale = unique_frame('frame-from-another-batch', 1)
        with self.assertRaises(MeteringError):
            build_meter_facts(catalog, evidence + [stale])

    def test_payload_tamper_keeps_frozen_digest_and_is_rejected(self):
        catalog, evidence = build_batch(INLET_SPEC, OUTLET_SPEC)
        tampered = copy.deepcopy(evidence)
        frame = next(f for f in tampered if f['id'] == 'outlet-2')
        frame['payload'] = list(reversed(frame['payload']))
        # The catalogue digest is untouched: rebinding evidence cannot pass.
        with self.assertRaises(MeteringError):
            build_meter_facts(catalog, tampered)

    def test_decreasing_reading_and_true_zero(self):
        spec_in = [(0, 10), (6, 10), (12, 4)]
        catalog, evidence = build_batch(spec_in, OUTLET_SPEC)
        facts = build_meter_facts(catalog, evidence)
        inlet_facts = [f for f in facts['facts'] if f['side'] == 'inlet']
        self.assertEqual(
            [(f['status'], f['increment_ml'], f['unknown_reason'])
             for f in inlet_facts],
            [('known', 0, None), ('unknown', None, 'decreasing_reading')])
        result = align_meter_facts(
            facts, timing([(0, 0, False), (12, 12, True)],
                          BASE_OUT_ANCHORS))

        def fact_id_of(record):
            if record['type'] == 'paired':
                return record['inlet']['fact_id']
            return record['fact_id']

        zero_records = [r for r in result['paired'] + result['unpaired']
                        if fact_id_of(r) == 'inlet-fact-1']
        # The genuine zero increment still follows full coverage rules.
        self.assertTrue(zero_records)

        def share_view(record):
            if record['type'] == 'paired':
                return F(record['inlet']['share_ml'])
            return F(record['share_ml'])

        self.assertTrue(all(share_view(r) == 0 for r in zero_records))

        def local_view(record):
            if record['type'] == 'paired':
                node = record['inlet']['local_range']
            else:
                node = record['local_range']
            return F(node['start']), F(node['end'])

        tiled = sorted(local_view(r) for r in zero_records)
        # Outlet reference coverage starts at 2, so the zero fact tiles into
        # the calibrated unpaired head [0,2) and the paired body [2,6).
        self.assertEqual(tiled[0][0], Fraction(0))
        self.assertEqual(tiled[-1][1], Fraction(6))
        self.assertEqual(
            sum(end - start for start, end in tiled), Fraction(6))


# ------------------------------------------------------------- rejection rules
class CatalogueRejectionTests(unittest.TestCase):
    def setUp(self):
        self.catalog, self.evidence = build_batch(INLET_SPEC, OUTLET_SPEC)

    def assert_rejects(self, catalog=None, evidence=None):
        with self.assertRaises(MeteringError):
            build_meter_facts(
                self.catalog if catalog is None else catalog,
                self.evidence if evidence is None else evidence)

    def test_boolean_tick_and_boolean_payload_bits(self):
        catalog = copy.deepcopy(self.catalog)
        catalog['points'][0]['tick'] = True
        self.assert_rejects(catalog=catalog)
        evidence = copy.deepcopy(self.evidence)
        evidence[0]['payload'][0] = True
        self.assert_rejects(evidence=evidence)

    def test_ambiguous_flag_must_match_path_count(self):
        evidence = copy.deepcopy(self.evidence)
        evidence[0]['ambiguous'] = True  # optimal_paths stays 1
        self.assert_rejects(evidence=evidence)

    def test_path_count_bounds_and_booleans(self):
        evidence = copy.deepcopy(self.evidence)
        evidence[0]['optimal_paths'] = (1 << BITS) + 1
        self.assert_rejects(evidence=evidence)
        evidence = copy.deepcopy(self.evidence)
        evidence[0]['optimal_paths'] = True
        self.assert_rejects(evidence=evidence)

    def test_payload_length_bounds(self):
        evidence = copy.deepcopy(self.evidence)
        evidence[0]['payload'] = []
        self.assert_rejects(evidence=evidence)
        evidence = copy.deepcopy(self.evidence)
        evidence[0]['payload'] = [0] * 65
        self.assert_rejects(evidence=evidence)

    def test_duplicate_frame_evidence_and_duplicate_tick(self):
        self.assert_rejects(evidence=self.evidence + [self.evidence[0]])
        catalog = copy.deepcopy(self.catalog)
        for point in catalog['points']:
            if point['meter_id'] == INLET:
                point['tick'] = 5
        self.assert_rejects(catalog=catalog)

    def test_digest_tampering_is_detected(self):
        catalog = copy.deepcopy(self.catalog)
        catalog['points'][0]['evidence_digest'] = '0' * 64
        self.assert_rejects(catalog=catalog)

    def test_meter_identity_and_size_rules(self):
        catalog = copy.deepcopy(self.catalog)
        catalog['outlet_meter_id'] = INLET
        self.assert_rejects(catalog=catalog)
        catalog = copy.deepcopy(self.catalog)
        catalog['points'] = [p for p in catalog['points']
                             if p['meter_id'] != OUTLET][:1]
        self.assert_rejects(catalog=catalog)

    def test_shuffled_inputs_are_equivalent(self):
        shuffled_catalog = copy.deepcopy(self.catalog)
        random.Random(7).shuffle(shuffled_catalog['points'])
        shuffled_evidence = list(reversed(copy.deepcopy(self.evidence)))
        a = build_meter_facts(self.catalog, self.evidence)
        b = build_meter_facts(shuffled_catalog, shuffled_evidence)
        self.assertEqual(json.dumps(a, sort_keys=True),
                         json.dumps(b, sort_keys=True))

    def test_missing_evidence_is_legal_not_a_format_error(self):
        facts = build_meter_facts(
            self.catalog, self.evidence[2:])  # first two frames absent
        self.assertEqual(len(facts['facts']), 5)


class TimingRejectionTests(unittest.TestCase):
    def setUp(self):
        self.facts, _ = base_result()

    def assert_rejects(self, timing_doc):
        with self.assertRaises(AlignmentError):
            align_meter_facts(self.facts, timing_doc)

    def test_first_anchor_must_not_interpolate(self):
        doc = timing([(0, 0, True), (6, 6, True)], BASE_OUT_ANCHORS)
        self.assert_rejects(doc)

    def test_columns_must_strictly_increase(self):
        self.assert_rejects(timing(
            [(0, 0, False), (6, 6, False), (6, 18, True)],
            BASE_OUT_ANCHORS))
        self.assert_rejects(timing(
            [(0, 0, False), (6, 18, True), (12, 18, True)],
            BASE_OUT_ANCHORS))

    def test_boolean_and_meter_binding_rules(self):
        doc = timing([(0, 0, False), (6, 6, 1)], BASE_OUT_ANCHORS)
        self.assert_rejects(doc)
        doc = copy.deepcopy(timing(BASE_IN_ANCHORS, BASE_OUT_ANCHORS))
        doc['inlet_meter_id'] = 'other-meter'
        self.assert_rejects(doc)

    def test_single_anchor_has_no_coverage(self):
        result = align_meter_facts(
            self.facts, timing([(0, 0, False)],
                               [(0, 2, False), (16, 18, True)]))
        self.assertEqual(result['paired'], [])
        inlet_unpaired = [r for r in result['unpaired']
                          if r['side'] == 'inlet']
        self.assertTrue(all(r['type'] == 'uncalibrated'
                            for r in inlet_unpaired))


# ------------------------------------------------------------- geometry edges
class GeometryEdgeTests(unittest.TestCase):
    def test_anchor_covers_only_part_of_fact_range(self):
        # Inlet calibration ends at tick 4; the [4,6) tail of the first fact
        # and the whole second fact stay uncalibrated.  The [0,2) head is
        # calibrated but never intersects outlet coverage, so [2,4) is the
        # only paired inlet sub-range.
        partial_in = [(0, 0, False), (4, 4, True)]
        _, result = base_result(partial_in)
        paired = result['paired']
        self.assertEqual(
            [(F(r['reference_range']['start']),
              F(r['reference_range']['end'])) for r in paired],
            [(Fraction(2), Fraction(4))])
        self.assertEqual(F(paired[0]['inlet']['share_ml']), Fraction(4))
        uncal = [r for r in result['unpaired'] if r['type'] == 'uncalibrated']
        self.assertEqual(
            sorted((r['fact_id'], F(r['local_range']['start']),
                    F(r['local_range']['end']), F(r['share_ml']))
                   for r in uncal),
            [('inlet-fact-1', Fraction(4), Fraction(6), Fraction(4)),
             ('inlet-fact-2', Fraction(6), Fraction(12), Fraction(12))])
        # The [0,2) head is still calibrated and delivered as such.
        calibrated_head = [r for r in result['unpaired']
                           if r['type'] == 'calibrated_unpaired'
                           and r['fact_id'] == 'inlet-fact-1']
        self.assertEqual(
            [(F(r['local_range']['start']), F(r['local_range']['end']),
              F(r['share_ml'])) for r in calibrated_head],
            [(Fraction(0), Fraction(2), Fraction(4))])

    def test_endpoint_contact_produces_no_pair(self):
        # Inlet maps onto reference [0,6); outlet onto [6,18): they only
        # touch at 6.
        contact_in = [(0, 0, False), (12, 6, True)]
        contact_out = [(0, 6, False), (16, 18, True)]
        _, result = base_result(contact_in, contact_out)
        self.assertEqual(result['paired'], [])
        self.assertTrue(all(r['type'] == 'calibrated_unpaired'
                            for r in result['unpaired']))

    def test_fact_spanning_calibration_gap_re_enters_later_segment(self):
        # Enabled segments [4,8) and [16,24) with a disabled line creating a
        # gap on both sides of tick 8; the two inlet facts [0,8) and [8,24)
        # each contain calibrated and uncalibrated pieces, proving that a
        # later segment is re-entered after the gap instead of being skipped.
        gap_in = [(0, 0, False), (4, 4, False), (8, 8, True),
                  (16, 16, False), (24, 40, True)]
        gap_out = [(0, 0, False), (24, 48, True)]
        catalog, evidence = build_batch(
            [(0, 0), (8, 8), (24, 40)], OUTLET_SPEC)
        facts = build_meter_facts(catalog, evidence)
        result = align_meter_facts(facts, timing(gap_in, gap_out))
        uncal = {(r['fact_id'], F(r['local_range']['start']),
                  F(r['local_range']['end']))
                 for r in result['unpaired'] if r['type'] == 'uncalibrated'}
        # Fact 1 covers [0,8): [0,4) gap, [4,8) calibrated.
        self.assertIn(('inlet-fact-1', Fraction(0), Fraction(4)), uncal)
        # Fact 2 covers [8,24): [8,16) gap, [16,24) calibrated.
        self.assertIn(('inlet-fact-2', Fraction(8), Fraction(16)), uncal)
        # Both calibrated regions still produced reference-mapped output.
        mapped = set()
        for record in result['paired']:
            view = record['inlet']
            mapped.add((F(view['local_range']['start']),
                        F(view['local_range']['end'])))
        for record in result['unpaired']:
            if record['type'] == 'calibrated_unpaired' \
                    and record['side'] == 'inlet':
                mapped.add((F(record['local_range']['start']),
                            F(record['local_range']['end'])))
        self.assertIn((Fraction(4), Fraction(8)), mapped)
        # The second calibrated region is tiled by pair boundaries; its
        # mapped pieces must cover exactly [16,24).
        second = sorted((start, end) for start, end in mapped
                        if start >= 16)
        self.assertEqual(second[0][0], Fraction(16))
        self.assertEqual(second[-1][1], Fraction(24))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(second, second[1:])))

    def test_large_span_keeps_small_exact_output(self):
        big_in = [(0, 0, False), (6, 6 * 10 ** 11, True),
                  (12, 18 * 10 ** 11, True)]
        big_out = [(0, 2 * 10 ** 11, False),
                   (16, 18 * 10 ** 11, True)]
        facts, big = base_result(big_in, big_out)
        _, small = base_result()
        self.assertEqual(len(big['paired']), len(small['paired']))
        self.assertEqual(len(big['unpaired']), len(small['unpaired']))
        # Exact rational ranges at the 10**12 microsecond span.
        self.assertEqual(
            [(F(r['reference_range']['start']),
              F(r['reference_range']['end'])) for r in big['paired']],
            [(Fraction(2 * 10 ** 11), Fraction(6 * 10 ** 11)),
             (Fraction(6 * 10 ** 11), Fraction(12 * 10 ** 11)),
             (Fraction(12 * 10 ** 11), Fraction(18 * 10 ** 11))])
        self.assertEqual(
            [F(r['inlet']['share_ml']) for r in big['paired']],
            [Fraction(8), Fraction(6), Fraction(6)])


# ------------------------------------------------------------- immutability/JSON
class DocumentTests(unittest.TestCase):
    def test_realign_reuses_facts_and_never_mutates_them(self):
        facts, _ = base_result()
        snapshot = json.dumps(facts, sort_keys=True)
        align_meter_facts(facts, timing(
            [(0, 0, False), (6, 8, True), (12, 18, True)],
            BASE_OUT_ANCHORS))
        align_meter_facts(facts, timing(BASE_IN_ANCHORS, BASE_OUT_ANCHORS))
        self.assertEqual(json.dumps(facts, sort_keys=True), snapshot)

    def test_json_round_trip_is_lossless(self):
        facts, result = base_result()
        reloaded_facts = json.loads(json.dumps(facts))
        validate_facts(reloaded_facts)
        realigned = align_meter_facts(
            reloaded_facts, timing(BASE_IN_ANCHORS, BASE_OUT_ANCHORS))
        self.assertEqual(json.dumps(realigned, sort_keys=True),
                         json.dumps(result, sort_keys=True))
        # JSON safety: every rational is an explicit num/den object.
        text = json.dumps(result)
        self.assertNotIn('Infinity', text)
        for record in result['paired']:
            self.assertEqual(
                set(record['reference_range']['start']), {'num', 'den'})

    def test_deterministic_output_order(self):
        _, result = base_result()
        rebuilt_catalog, rebuilt_evidence = build_batch(
            INLET_SPEC, OUTLET_SPEC)
        again = align_meter_facts(
            build_meter_facts(rebuilt_catalog, rebuilt_evidence),
            timing(BASE_IN_ANCHORS, BASE_OUT_ANCHORS))
        self.assertEqual(
            [r['record_id'] for r in again['paired']],
            [r['record_id'] for r in result['paired']])
        self.assertEqual(
            [r['record_id'] for r in again['unpaired']],
            [r['record_id'] for r in result['unpaired']])


# ------------------------------------------------------------- independent oracle
class OracleTests(unittest.TestCase):
    @staticmethod
    def _segments(anchors):
        ordered = sorted(anchors, key=lambda a: a['tick'])
        segments = []
        for prev, anchor in zip(ordered, ordered[1:]):
            if not anchor['interpolate']:
                continue
            slope = (Fraction(anchor['reference_time']
                              - prev['reference_time'])
                     / Fraction(anchor['tick'] - prev['tick']))
            segments.append((Fraction(prev['tick']),
                             Fraction(anchor['tick']),
                             Fraction(prev['reference_time']), slope))
        return segments

    def _oracle_pieces(self, facts, anchors):
        """Independent per-fact partition over segment breakpoints."""
        segments = self._segments(anchors)
        pieces_by_fact = {}
        for fact in facts:
            if fact['status'] != 'known':
                continue
            lo = Fraction(fact['local_range']['start'])
            hi = Fraction(fact['local_range']['end'])
            cuts = {lo, hi}
            for seg_lo, seg_hi, _, _ in segments:
                if lo < seg_hi < hi:
                    cuts.add(seg_hi)
                if lo < seg_lo < hi:
                    cuts.add(seg_lo)
            pieces = []
            for start, end in zip(sorted(cuts), sorted(cuts)[1:]):
                # Calibrated only when the whole piece sits inside one
                # enabled segment; a piece touching a gap stays local-only.
                home = next((segment for segment in segments
                             if segment[0] <= start and end <= segment[1]),
                            None)
                if home is not None:
                    seg_lo, _, ref0, slope = home
                    ref_start = ref0 + slope * (start - seg_lo)
                    ref_end = ref0 + slope * (end - seg_lo)
                    calibrated = True
                else:
                    ref_start = ref_end = None
                    calibrated = False
                pieces.append((start, end, calibrated,
                               ref_start, ref_end))
            pieces_by_fact[fact['fact_id']] = pieces
        return pieces_by_fact

    def test_randomized_oracle(self):
        rng = random.Random(2026)
        for _ in range(40):
            in_ticks = sorted({0, *rng.sample(range(1, 60), 2)})
            out_ticks = sorted({0, *rng.sample(range(1, 60), 3)})
            in_readings = sorted(rng.sample(range(0, 100), len(in_ticks)))
            out_readings = sorted(rng.sample(range(0, 100), len(out_ticks)))
            catalog, evidence = build_batch(
                list(zip(in_ticks, in_readings)),
                list(zip(out_ticks, out_readings)))
            facts = build_meter_facts(catalog, evidence)
            in_anchors = self._random_anchors(rng, 0, 60)
            out_anchors = self._random_anchors(rng, 0, 60)
            result = align_meter_facts(facts, timing(in_anchors, out_anchors))
            side_anchors = {'inlet': anchor_list(in_anchors),
                            'outlet': anchor_list(out_anchors)}
            side_facts = {
                side: [f for f in facts['facts'] if f['side'] == side]
                for side in ('inlet', 'outlet')}
            expected = {
                side: self._oracle_pieces(side_facts[side],
                                          side_anchors[side])
                for side in ('inlet', 'outlet')}
            known = [f for f in facts['facts'] if f['status'] == 'known']
            fact_by_id = {f['fact_id']: f for f in known}
            # Gather emitted local coverage per fact and check tiling.
            covered = {f['fact_id']: [] for f in known}
            shares = {f['fact_id']: Fraction(0) for f in known}
            for record in result['paired']:
                for side in ('inlet', 'outlet'):
                    view = record[side]
                    covered[view['fact_id']].append(
                        (F(view['local_range']['start']),
                         F(view['local_range']['end'])))
                    shares[view['fact_id']] += F(view['share_ml'])
            for record in result['unpaired']:
                covered[record['fact_id']].append(
                    (F(record['local_range']['start']),
                     F(record['local_range']['end'])))
                shares[record['fact_id']] += F(record['share_ml'])
            for fact in known:
                lo = Fraction(fact['local_range']['start'])
                hi = Fraction(fact['local_range']['end'])
                intervals = sorted(covered[fact['fact_id']])
                cursor = lo
                for start, end in intervals:
                    self.assertEqual(start, cursor)
                    self.assertLess(start, end)
                    cursor = end
                self.assertEqual(cursor, hi)
                self.assertEqual(shares[fact['fact_id']],
                                 Fraction(fact['increment_ml']))
            # Independently recompute every paired reference intersection.
            expected_pairs = []
            in_pieces = [(fid, p) for fid, pieces in expected['inlet'].items()
                         for p in pieces if p[2]]
            out_pieces = [(fid, p) for fid, pieces in expected['outlet'].items()
                          for p in pieces if p[2]]
            for fid_a, (_, _, _, a0, a1) in in_pieces:
                for fid_b, (_, _, _, b0, b1) in out_pieces:
                    start, end = max(a0, b0), min(a1, b1)
                    if start < end:
                        expected_pairs.append((start, end))
            self.assertEqual(
                sorted((F(r['reference_range']['start']),
                        F(r['reference_range']['end']))
                       for r in result['paired']),
                sorted(expected_pairs))

    @staticmethod
    def _random_anchors(rng, low, high):
        count = rng.randint(1, 5)
        ticks = sorted({low, *rng.sample(range(low + 1, high + 1),
                                         count - 1)})
        refs = sorted(rng.sample(range(0, 3 * high + 1), len(ticks)))
        return [(tick, ref, index > 0 and rng.random() < 0.7)
                for index, (tick, ref) in enumerate(zip(ticks, refs))]


class HandoffSubprocessTests(unittest.TestCase):
    """A fresh ``python -I`` process aligns from saved JSON facts only."""

    def test_aligns_reloaded_json_without_frame_evidence(self):
        facts, expected = base_result()
        directory = Path(tempfile.mkdtemp(prefix='trellis-meter-'))
        facts_path = directory / 'facts.json'
        timing_path = directory / 'timing.json'
        facts_path.write_text(json.dumps(facts), encoding='utf-8')
        timing_path.write_text(json.dumps(
            timing(BASE_IN_ANCHORS, BASE_OUT_ANCHORS)), encoding='utf-8')
        runner = directory / 'run.py'
        runner.write_text(
            'import json, sys\n'
            f'sys.path.insert(0, {SRC_PATH!r})\n'
            'from telemetry_trellis import align_meter_facts, '
            'validate_facts\n'
            'facts = validate_facts(json.load(open(sys.argv[1])))\n'
            'timing_doc = json.load(open(sys.argv[2]))\n'
            'print(json.dumps(align_meter_facts(facts, timing_doc), '
            'sort_keys=True))\n',
            encoding='utf-8')
        completed = subprocess.run(
            [sys.executable, '-I', str(runner), str(facts_path),
             str(timing_path)],
            capture_output=True, text=True, check=True)
        child_result = json.loads(completed.stdout)
        self.assertEqual(
            json.dumps(child_result, sort_keys=True),
            json.dumps(expected, sort_keys=True))
        # The saved facts document itself is untouched by the child run.
        self.assertEqual(json.loads(facts_path.read_text()), facts)


if __name__ == '__main__':
    unittest.main()
