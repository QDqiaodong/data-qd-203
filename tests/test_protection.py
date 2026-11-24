"""Acceptance, oracle and memory tests for offline XOR protection packages.

Two device-free entry points are exercised end to end:

* ``build_protection_package`` projects a trusted XOR catalogue onto the
  working frames and emits a canonical JSON package;
* ``complete_frames`` joins a reloaded package with trusted local decode
  evidence in another process and fills every forced payload bit.

The acceptance geometry (working frames A/B/C/D plus blind frame H) and the
32768-bit, 512-record memory scenario reproduce the data contract; an
independent brute-force oracle over every bit assignment checks both the
projection semantics and the per-bit certainty rules instead of trusting the
two new functions to validate each other.
"""
import copy
import functools
import itertools
import json
import random
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from telemetry_trellis import (
    ProtectionError,
    SOURCE_OBSERVED,
    SOURCE_PROTECTED,
    SOURCE_UNKNOWN,
    build_protection_package,
    complete_frames,
    decode_session,
    load_protection_package,
    save_protection_package,
)
from telemetry_trellis.protection import _gauss_jordan_reduce

SRC_PATH = str(Path(__file__).resolve().parents[1] / 'src')


def acceptance_catalog():
    """A,B,C one bit, D two bits, blind H one bit; four catalogue records."""
    return {
        'frames': [
            {'session_id': 's1', 'frame_id': 'A', 'payload_length': 1},
            {'session_id': 's1', 'frame_id': 'B', 'payload_length': 1},
            {'session_id': 's1', 'frame_id': 'C', 'payload_length': 1},
            {'session_id': 's1', 'frame_id': 'D', 'payload_length': 2},
            {'session_id': 's1', 'frame_id': 'H', 'payload_length': 1},
        ],
        'selection': [['s1', 'A'], ['s1', 'B'], ['s1', 'C'], ['s1', 'D']],
        'records': [
            {'coordinates': [['s1', 'A', 0], ['s1', 'H', 0]], 'value': 0},
            {'coordinates': [['s1', 'H', 0], ['s1', 'B', 0]], 'value': 1},
            {'coordinates': [['s1', 'B', 0], ['s1', 'C', 0]], 'value': 0},
            {'coordinates': [['s1', 'C', 0], ['s1', 'D', 0]], 'value': 1},
        ],
    }


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class GaussJordanOracleTests(unittest.TestCase):
    """The incremental reducer must match textbook Gauss-Jordan RREF."""

    @staticmethod
    def _textbook(rows, width, affine):
        matrix = [[(mask >> col) & 1 for col in range(width)]
                  for mask in rows]
        rank = 0
        limit = width - 1 if affine else width
        for col in range(limit):
            pivot = next((i for i in range(rank, len(matrix))
                          if matrix[i][col]), None)
            if pivot is None:
                continue
            matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
            for i in range(len(matrix)):
                if i != rank and matrix[i][col]:
                    matrix[i] = [a ^ b for a, b in
                                 zip(matrix[i], matrix[rank])]
            rank += 1
        kept = [sum(bit << i for i, bit in enumerate(row))
                for row in matrix if any(row)]
        bare = 1 << (width - 1)
        inconsistent = affine and bare in kept
        return sorted(x for x in kept if not (affine and x == bare)), \
            inconsistent

    @staticmethod
    def _feasible(rows, nvars):
        coefficient_mask = (1 << nvars) - 1
        for assignment in range(1 << nvars):
            if all(((row & coefficient_mask) & assignment).bit_count() % 2
                   == (row >> nvars) & 1 for row in rows):
                return True
        return False

    def test_matches_textbook_rref_on_random_systems(self):
        random.seed(42)
        errors = 0
        consistent = 0
        for _ in range(6000):
            width = random.randint(2, 10)
            nrows = random.randint(1, 10)
            rows = [random.randrange(1, 1 << width) for _ in range(nrows)]
            for affine in (False, True):
                pivot_mask = (1 << (width - (1 if affine else 0))) - 1
                forward, backward = {}, {}
                residual_f = [_gauss_jordan_reduce(forward, row, pivot_mask)
                              for row in rows]
                residual_b = [_gauss_jordan_reduce(backward, row, pivot_mask)
                              for row in reversed(rows)]
                wanted, inconsistent = self._textbook(rows, width, affine)
                if affine and (not self._feasible(rows, width - 1)) \
                        != inconsistent:
                    errors += 1
                if any(residual_f) != inconsistent:
                    errors += 1
                if inconsistent:
                    continue
                consistent += 1
                got = sorted(forward.values())
                if got != wanted or sorted(backward.values()) != got:
                    errors += 1
        self.assertEqual(errors, 0)
        self.assertGreater(consistent, 8000)


class BuildProjectionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = acceptance_catalog()

    def test_projects_to_three_working_equations_without_blind_leakage(self):
        package = build_protection_package(self.catalog)
        self.assertEqual(package['kind'],
                         'telemetry_trellis.protection_package')
        self.assertEqual(package['version'], 1)
        self.assertEqual([(f['session_id'], f['frame_id'])
                          for f in package['frames']],
                         [('s1', 'A'), ('s1', 'B'), ('s1', 'C'),
                          ('s1', 'D')])
        # No blind frame identity, coordinate or value survives.
        blob = canonical_json(package)
        self.assertNotIn('H', blob)
        self.assertEqual(len(package['constraints']), 3)
        # Rows are fixed-width hex covering five work bits plus the RHS bit.
        self.assertTrue(all(len(c['row']) == 2
                            for c in package['constraints']))
        # Pivots ascend and every row carries at least one coefficient.
        masks = [int(c['row'], 16) for c in package['constraints']]
        pivots = [(m & 31) & -(m & 31) for m in masks]
        self.assertEqual(pivots, sorted(pivots))
        self.assertTrue(all(m & 31 for m in masks))

    def test_package_is_invariant_under_order_derived_and_blind_changes(self):
        baseline = canonical_json(build_protection_package(self.catalog))

        shuffled = copy.deepcopy(self.catalog)
        shuffled['frames'] = list(reversed(shuffled['frames']))
        shuffled['selection'] = list(reversed(shuffled['selection']))
        shuffled['records'] = list(reversed(shuffled['records']))
        self.assertEqual(canonical_json(build_protection_package(shuffled)),
                         baseline)

        # A xor B xor C xor D0 = 0 is the xor of all four catalogue rows
        # (H appears twice and cancels; RHS xor is 0).
        derivable = copy.deepcopy(self.catalog)
        derivable['records'].append({'coordinates': [
            ['s1', 'A', 0], ['s1', 'B', 0], ['s1', 'C', 0],
            ['s1', 'D', 0]], 'value': 0})
        self.assertEqual(
            canonical_json(build_protection_package(derivable)), baseline)

        # Flipping the first two RHS values is equivalent to choosing the
        # other value of blind H; the projected working set is unchanged.
        flipped = copy.deepcopy(self.catalog)
        flipped['records'][0]['value'] = 1
        flipped['records'][1]['value'] = 0
        self.assertEqual(
            canonical_json(build_protection_package(flipped)), baseline)

    def test_duplicate_and_derived_records_leave_package_unchanged(self):
        baseline = canonical_json(build_protection_package(self.catalog))
        duplicated = copy.deepcopy(self.catalog)
        duplicated['records'].append(copy.deepcopy(
            self.catalog['records'][2]))
        self.assertEqual(
            canonical_json(build_protection_package(duplicated)), baseline)

    def test_blind_only_contradiction_is_rejected(self):
        catalog = {
            'frames': [
                {'session_id': 's', 'frame_id': 'W', 'payload_length': 1},
                {'session_id': 's', 'frame_id': 'G', 'payload_length': 1},
            ],
            'selection': [['s', 'W']],
            'records': [
                {'coordinates': [['s', 'G', 0]], 'value': 0},
                {'coordinates': [['s', 'G', 0]], 'value': 1},
            ],
        }
        with self.assertRaises(ProtectionError):
            build_protection_package(catalog)

    def test_working_contradiction_is_rejected(self):
        catalog = {
            'frames': [
                {'session_id': 's', 'frame_id': 'W', 'payload_length': 1}],
            'selection': [['s', 'W']],
            'records': [
                {'coordinates': [['s', 'W', 0]], 'value': 0},
                {'coordinates': [['s', 'W', 0]], 'value': 1},
            ],
        }
        with self.assertRaises(ProtectionError):
            build_protection_package(catalog)

    def test_legal_empty_constraint_package(self):
        # The only record is confined to the blind frame, so no working
        # constraint survives projection; the package is legally empty.
        package = build_protection_package({
            'frames': [
                {'session_id': 's', 'frame_id': 'W', 'payload_length': 1},
                {'session_id': 's', 'frame_id': 'G', 'payload_length': 1},
            ],
            'selection': [['s', 'W']],
            'records': [
                {'coordinates': [['s', 'G', 0]], 'value': 0},
            ],
        })
        self.assertEqual(package['constraints'], [])
        out = complete_frames(package, [])
        self.assertEqual(out[0]['values'], [None])
        self.assertEqual(out[0]['sources'], [SOURCE_UNKNOWN])

    def test_blind_bit_is_never_assumed_zero(self):
        # A xor H = 0 alone projects to nothing: H can always match A.
        package = build_protection_package({
            'frames': [
                {'session_id': 's', 'frame_id': 'A', 'payload_length': 1},
                {'session_id': 's', 'frame_id': 'H', 'payload_length': 1},
            ],
            'selection': [['s', 'A']],
            'records': [
                {'coordinates': [['s', 'A', 0], ['s', 'H', 0]], 'value': 0},
            ],
        })
        self.assertEqual(package['constraints'], [])

    def test_catalog_input_validation(self):
        bad_cases = [
            {'frames': [], 'selection': [['s', 'W']], 'records': []},
            {'frames': [{'session_id': 's', 'frame_id': 'W',
                         'payload_length': 1}],
             'selection': [], 'records': []},
            {'frames': [{'session_id': 's', 'frame_id': 'W',
                         'payload_length': 1}],
             'selection': [['s', 'G']], 'records': []},
        ]
        for catalog in bad_cases:
            with self.assertRaises(ProtectionError):
                build_protection_package(catalog)
        duplicate_key = copy.deepcopy(self.catalog)
        duplicate_key['frames'].append(
            {'session_id': 's1', 'frame_id': 'A', 'payload_length': 1})
        with self.assertRaises(ProtectionError):
            build_protection_package(duplicate_key)
        duplicate_selection = copy.deepcopy(self.catalog)
        duplicate_selection['selection'].append(['s1', 'A'])
        with self.assertRaises(ProtectionError):
            build_protection_package(duplicate_selection)
        boolean_rhs = copy.deepcopy(self.catalog)
        boolean_rhs['records'][0]['value'] = True
        with self.assertRaises(ProtectionError):
            build_protection_package(boolean_rhs)
        bad_coord = copy.deepcopy(self.catalog)
        bad_coord['records'][0]['coordinates'][0][2] = 7
        with self.assertRaises(ProtectionError):
            build_protection_package(bad_coord)
        dup_coord = copy.deepcopy(self.catalog)
        dup_coord['records'][0]['coordinates'].append(
            ['s1', 'A', 0])
        with self.assertRaises(ProtectionError):
            build_protection_package(dup_coord)
        unknown_frame = copy.deepcopy(self.catalog)
        unknown_frame['records'][0]['coordinates'][0][1] = 'Z'
        with self.assertRaises(ProtectionError):
            build_protection_package(unknown_frame)

    def test_does_not_read_payloads_or_mutate_inputs(self):
        snapshot = canonical_json(self.catalog)
        build_protection_package(self.catalog)
        self.assertEqual(canonical_json(self.catalog), snapshot)


class CompletionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.package = build_protection_package(acceptance_catalog())
        # B and D are frames of one non-terminated constraint-2 session.
        self.session_request = {
            'session_id': 's1', 'constraint': 2,
            'generators': [1, 2], 'terminated': False,
            'tail_received': [],
            'frames': [
                {'id': 'B', 'payload_length': 1,
                 'received': [0, None]},
                {'id': 'D', 'payload_length': 2,
                 'received': [None, None, None, None]},
            ],
        }

    def _session_evidence(self):
        result = decode_session(self.session_request)
        self.assertEqual(result['session_id'], 's1')
        evidence = []
        for row in result['frames']:
            evidence.append({
                'session_id': result['session_id'], 'id': row['id'],
                'payload': row['payload'], 'ambiguous': row['ambiguous'],
            })
        return evidence

    def test_joint_decode_then_protection_completion_story(self):
        evidence = self._session_evidence()
        # B is uniquely 0; the D frame stays ambiguous even though it carries
        # a lexicographic representative payload.
        b_row = next(e for e in evidence if e['id'] == 'B')
        d_row = next(e for e in evidence if e['id'] == 'D')
        self.assertEqual(b_row['payload'], [0])
        self.assertFalse(b_row['ambiguous'])
        self.assertTrue(d_row['ambiguous'])

        out = complete_frames(copy.deepcopy(self.package), evidence)
        by_key = {tuple(row['frame_key']): row for row in out}
        self.assertEqual(by_key[('s1', 'A')]['values'], [1])
        self.assertEqual(by_key[('s1', 'A')]['sources'],
                         [SOURCE_PROTECTED])
        self.assertEqual(by_key[('s1', 'B')]['values'], [0])
        self.assertEqual(by_key[('s1', 'B')]['sources'],
                         [SOURCE_OBSERVED])
        self.assertEqual(by_key[('s1', 'C')]['values'], [0])
        self.assertEqual(by_key[('s1', 'C')]['sources'],
                         [SOURCE_PROTECTED])
        self.assertEqual(by_key[('s1', 'D')]['values'], [1, None])
        self.assertEqual(by_key[('s1', 'D')]['sources'],
                         [SOURCE_PROTECTED, SOURCE_UNKNOWN])
        # D still carries a null and therefore must not be marked complete.
        self.assertFalse(by_key[('s1', 'D')]['complete'])
        self.assertTrue(by_key[('s1', 'A')]['complete'])
        self.assertTrue(by_key[('s1', 'B')]['complete'])
        self.assertTrue(by_key[('s1', 'C')]['complete'])
        # Output ordering follows the frame key.
        self.assertEqual([tuple(r['frame_key']) for r in out],
                         [('s1', 'A'), ('s1', 'B'), ('s1', 'C'),
                          ('s1', 'D')])

    def test_removing_second_record_frees_a_but_keeps_c_and_d0(self):
        catalog = acceptance_catalog()
        del catalog['records'][1]
        package = build_protection_package(catalog)
        evidence = self._session_evidence()
        out = complete_frames(package, evidence)
        by_key = {tuple(row['frame_key']): row for row in out}
        self.assertEqual(by_key[('s1', 'A')]['values'], [None])
        self.assertEqual(by_key[('s1', 'A')]['sources'],
                         [SOURCE_UNKNOWN])
        self.assertEqual(by_key[('s1', 'C')]['values'], [0])
        self.assertEqual(by_key[('s1', 'D')]['values'], [1, None])

    def test_authoritative_observation_contradiction_rejects_batch(self):
        evidence = self._session_evidence()
        evidence.append({'session_id': 's1', 'id': 'A',
                         'payload': [0], 'ambiguous': False})
        with self.assertRaises(ProtectionError):
            complete_frames(self.package, evidence)

    def test_ambiguous_representative_does_not_contradict(self):
        evidence = self._session_evidence()
        evidence.append({'session_id': 's1', 'id': 'A',
                         'payload': [0], 'ambiguous': True})
        out = complete_frames(self.package, evidence)
        by_key = {tuple(row['frame_key']): row for row in out}
        # The representative is ignored; A is still fixed by protection.
        self.assertEqual(by_key[('s1', 'A')]['values'], [1])
        self.assertEqual(by_key[('s1', 'A')]['sources'],
                         [SOURCE_PROTECTED])

    def test_joint_constraints_can_fix_bits_without_any_evidence(self):
        package = build_protection_package({
            'frames': [
                {'session_id': 's', 'frame_id': 'F', 'payload_length': 3}],
            'selection': [['s', 'F']],
            'records': [
                {'coordinates': [['s', 'F', 0]], 'value': 1},
                {'coordinates': [['s', 'F', 0], ['s', 'F', 1]],
                 'value': 1},
            ],
        })
        out = complete_frames(package, [])[0]
        self.assertEqual(out['values'], [1, 0, None])
        self.assertEqual(out['sources'], [
            SOURCE_PROTECTED, SOURCE_PROTECTED, SOURCE_UNKNOWN])

    def test_evidence_validation_rejects_foreign_duplicate_and_bad_values(self):
        good = self._session_evidence()
        cases = [
            good + [{'session_id': 'zz', 'id': 'B', 'payload': [0],
                     'ambiguous': False}],
            good + [good[0]],
            [{'session_id': 's1', 'id': 'B', 'payload': [0, 0],
              'ambiguous': False}],
            [{'session_id': 's1', 'id': 'B', 'payload': [True],
              'ambiguous': False}],
            [{'session_id': 's1', 'id': 'B', 'payload': [0],
              'ambiguous': 0}],
            [{'session_id': 's1', 'id': 'B', 'payload': [2],
              'ambiguous': False}],
        ]
        for evidence in cases:
            with self.assertRaises(ProtectionError):
                complete_frames(self.package, evidence)

    def test_cross_session_same_frame_id_is_keyed_by_pair(self):
        package = build_protection_package({
            'frames': [
                {'session_id': 'sx', 'frame_id': 'X', 'payload_length': 1},
                {'session_id': 'sy', 'frame_id': 'X', 'payload_length': 1},
            ],
            'selection': [['sx', 'X'], ['sy', 'X']],
            'records': [
                {'coordinates': [['sx', 'X', 0], ['sy', 'X', 0]],
                 'value': 1},
            ],
        })
        out = complete_frames(package, [
            {'session_id': 'sx', 'id': 'X', 'payload': [0],
             'ambiguous': False}])
        self.assertEqual([tuple(r['frame_key']) for r in out],
                         [('sx', 'X'), ('sy', 'X')])
        self.assertEqual(out[0]['values'], [0])
        self.assertEqual(out[1]['values'], [1])
        with self.assertRaises(ProtectionError):
            complete_frames(package, [
                {'session_id': 'sz', 'id': 'X', 'payload': [0],
                 'ambiguous': False}])

    def test_inputs_are_not_modified(self):
        evidence = self._session_evidence()
        package_snapshot = canonical_json(self.package)
        evidence_snapshot = canonical_json(evidence)
        complete_frames(self.package, evidence)
        self.assertEqual(canonical_json(self.package), package_snapshot)
        self.assertEqual(canonical_json(evidence), evidence_snapshot)

    def test_tampered_package_is_rejected(self):
        package = copy.deepcopy(self.package)
        package['constraints'][0]['row'] = 'zz'
        with self.assertRaises(ProtectionError):
            complete_frames(package, [])
        package = copy.deepcopy(self.package)
        package['frames'][0]['frame_id'] = 'Z'
        with self.assertRaises(ProtectionError):
            complete_frames(package, [])
        # Non-RREF rows (pivot column repeats) are refused.
        package = copy.deepcopy(self.package)
        first = int(package['constraints'][0]['row'], 16)
        second = int(package['constraints'][1]['row'], 16)
        package['constraints'][1]['row'] = f'{first | second:02x}'
        with self.assertRaises(ProtectionError):
            complete_frames(package, [])


class ExhaustiveOracleTests(unittest.TestCase):
    """Independent brute-force oracle over every bit assignment."""

    @staticmethod
    def _build_random_catalog(rng):
        nwork = rng.randint(1, 5)
        nblind = rng.randint(1, 4)
        frames = [
            {'session_id': 's', 'frame_id': 'W%d' % i, 'payload_length': 1}
            for i in range(nwork)] + [
            {'session_id': 's', 'frame_id': 'B%d' % i, 'payload_length': 1}
            for i in range(nblind)]
        selection = [['s', 'W%d' % i] for i in range(nwork)]
        index_of = {(f['session_id'], f['frame_id']): j
                    for j, f in enumerate(frames)}
        records = []
        for _ in range(rng.randint(0, 7)):
            count = rng.randint(1, min(5, nwork + nblind))
            cols = rng.sample(range(nwork + nblind), count)
            coords = [[frames[c]['session_id'], frames[c]['frame_id'], 0]
                      for c in cols]
            records.append({'coordinates': coords,
                            'value': rng.randint(0, 1)})
        return ({'frames': frames, 'selection': selection,
                 'records': records}, index_of, nwork, nblind)

    def test_package_accept_set_matches_extendable_assignments(self):
        rng = random.Random(2026)
        satisfiable = 0
        for _ in range(600):
            catalog, index_of, nwork, nblind = self._build_random_catalog(rng)
            acceptable = set()
            for work in range(1 << nwork):
                for blind in range(1 << nblind):
                    full = work | (blind << nwork)
                    if all(sum((full >> index_of[tuple(co[:2])]) & 1
                               for co in record['coordinates']) % 2
                           == record['value']
                           for record in catalog['records']):
                        acceptable.add(work)
                        break
            try:
                package = build_protection_package(catalog)
            except ProtectionError:
                self.assertFalse(acceptable)
                continue
            satisfiable += 1
            rows = [(int(c['row'], 16) & ((1 << nwork) - 1),
                     (int(c['row'], 16) >> nwork) & 1)
                    for c in package['constraints']]
            package_accept = {
                work for work in range(1 << nwork)
                if all((work & coef).bit_count() % 2 == rhs
                       for coef, rhs in rows)}
            self.assertEqual(package_accept, acceptable)
        self.assertGreater(satisfiable, 400)

    def test_completion_certainty_matches_allowed_assignment_oracle(self):
        rng = random.Random(909)
        checked = 0
        for _ in range(300):
            catalog, _, nwork, nblind = self._build_random_catalog(rng)
            try:
                package = build_protection_package(catalog)
            except ProtectionError:
                continue
            # Random partial, always-consistent observations.
            work_keys = [('s', 'W%d' % i) for i in range(nwork)]
            observed = {}
            evidence_items = {}
            for col in range(nwork):
                if rng.random() < 0.5:
                    observed[col] = rng.randint(0, 1)
            # Keep only observations that leave at least one completion.
            rows = [(int(c['row'], 16) & ((1 << nwork) - 1),
                     (int(c['row'], 16) >> nwork) & 1)
                    for c in package['constraints']]
            allowed = [
                work for work in range(1 << nwork)
                if all((work & coef).bit_count() % 2 == rhs
                       for coef, rhs in rows)
                and all(((work >> col) & 1) == value
                        for col, value in observed.items())]
            if not allowed:
                continue
            # Group observed bits back into one evidence item per frame.
            lengths = {(f['session_id'], f['frame_id']):
                       f['payload_length'] for f in package['frames']}
            offsets = {}
            cursor = 0
            for f in package['frames']:
                offsets[(f['session_id'], f['frame_id'])] = cursor
                cursor += f['payload_length']
            per_frame = {}
            for col, value in observed.items():
                per_frame.setdefault(work_keys[col], {})[
                    col - offsets[work_keys[col]]] = value
            evidence = []
            for key, cells in per_frame.items():
                payload = [cells.get(i) for i in range(lengths[key])]
                # Every slot of an observed frame is filled in this oracle.
                self.assertNotIn(None, payload)
                evidence.append({'session_id': key[0], 'id': key[1],
                                 'payload': payload, 'ambiguous': False})
            out = complete_frames(package, evidence)
            flat = [value for frame in out for value in frame['values']]
            sources = [s for frame in out for s in frame['sources']]
            for col in range(nwork):
                values = {(work >> col) & 1 for work in allowed}
                if col in observed:
                    self.assertEqual(sources[col], SOURCE_OBSERVED)
                    self.assertEqual(flat[col], observed[col])
                elif len(values) == 1:
                    self.assertEqual(sources[col], SOURCE_PROTECTED)
                    self.assertEqual(flat[col], values.pop())
                else:
                    self.assertEqual(sources[col], SOURCE_UNKNOWN)
                    self.assertIsNone(flat[col])
            checked += 1
        self.assertGreater(checked, 150)


class PersistenceAndProcessTests(unittest.TestCase):
    def test_json_roundtrip_and_fresh_process_completion(self):
        work_dir = Path(tempfile.mkdtemp(prefix='protection-data-'))
        runner_dir = Path(tempfile.mkdtemp(prefix='protection-runner-'))
        self.addCleanup(lambda: shutil.rmtree(work_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(runner_dir, ignore_errors=True))
        package = build_protection_package(acceptance_catalog())
        package_path = work_dir / 'package.json'
        evidence_path = work_dir / 'evidence.json'
        output_path = work_dir / 'completed.json'
        save_protection_package(package_path, package)
        reloaded = load_protection_package(package_path)
        self.assertEqual(canonical_json(reloaded), canonical_json(package))
        session = decode_session({
            'session_id': 's1', 'constraint': 2, 'generators': [1, 2],
            'terminated': False, 'tail_received': [],
            'frames': [
                {'id': 'B', 'payload_length': 1, 'received': [0, None]},
                {'id': 'D', 'payload_length': 2,
                 'received': [None] * 4},
            ],
        })
        evidence = [{'session_id': session['session_id'],
                     'id': row['id'], 'payload': row['payload'],
                     'ambiguous': row['ambiguous']}
                    for row in session['frames']]
        evidence_path.write_text(json.dumps(evidence), encoding='utf-8')

        runner = runner_dir / 'runner.py'
        runner.write_text(
            'import json, sys\n'
            f"sys.path.insert(0, {SRC_PATH!r})\n"
            'from telemetry_trellis import (\n'
            '    load_protection_package, complete_frames)\n'
            'package = load_protection_package(sys.argv[1])\n'
            'evidence = json.load(open(sys.argv[2], encoding="utf-8"))\n'
            'result = complete_frames(package, evidence)\n'
            'with open(sys.argv[3], "w", encoding="utf-8") as handle:\n'
            '    json.dump(result, handle)\n'
            "print('fresh-process-marker')\n")
        completed = subprocess.run(
            [sys.executable, '-I', str(runner), str(package_path),
             str(evidence_path), str(output_path)],
            capture_output=True, text=True, check=True)
        self.assertIn('fresh-process-marker', completed.stdout)
        out = json.loads(output_path.read_text(encoding='utf-8'))
        by_key = {tuple(row['frame_key']): row for row in out}
        self.assertEqual(by_key[('s1', 'A')]['values'], [1])
        self.assertEqual(by_key[('s1', 'D')]['values'], [1, None])

    def test_publish_refuses_to_overwrite(self):
        work_dir = Path(tempfile.mkdtemp(prefix='protection-overwrite-'))
        self.addCleanup(lambda: shutil.rmtree(work_dir, ignore_errors=True))
        target = work_dir / 'package.json'
        package = build_protection_package(acceptance_catalog())
        save_protection_package(target, package)
        with self.assertRaises(FileExistsError):
            save_protection_package(target, package)


class LargeScaleMemoryTests(unittest.TestCase):
    """The 128-frame / 32768-bit / 512-record offline scenario."""

    @staticmethod
    def _catalog():
        frames = [
            {'session_id': 's', 'frame_id': 'F%03d' % i,
             'payload_length': 256}
            for i in range(128)]
        selection = [['s', 'F%03d' % i] for i in range(127)]

        def coordinate(bit):
            return ['s', 'F%03d' % (bit // 256), bit % 256]

        records = [{'coordinates': [coordinate(0)], 'value': 1}]
        for i in range(1, 512):
            start = 63 * (i - 1)
            records.append({
                'coordinates': [coordinate(start + k) for k in range(64)],
                'value': 0})
        return {'frames': frames, 'selection': selection,
                'records': records}

    def test_peak_allocation_under_48_mib_and_only_x0_is_fixed(self):
        catalog = self._catalog()
        tracemalloc.start()
        try:
            package = build_protection_package(catalog)
            blob = json.dumps(package, sort_keys=True,
                               separators=(',', ':'))
            reloaded = json.loads(blob)
            completed = complete_frames(reloaded, [])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLessEqual(peak, 48 * 1024 * 1024)
        self.assertEqual(len(package['constraints']), 512)
        known = [value for frame in completed for value in frame['values']
                 if value is not None]
        unknown = [value for frame in completed for value in frame['values']
                   if value is None]
        self.assertEqual(known, [1])
        self.assertEqual(len(unknown), 127 * 256 - 1)
        first = completed[0]
        self.assertEqual(first['values'][0], 1)
        self.assertEqual(first['sources'][0], SOURCE_PROTECTED)
        self.assertTrue(all(v is None for v in first['values'][1:]))
        # No frame is complete and the last (blind) frame is absent.
        self.assertTrue(all(not frame['complete'] for frame in completed))
        self.assertEqual(len(completed), 127)


if __name__ == '__main__':
    unittest.main()
