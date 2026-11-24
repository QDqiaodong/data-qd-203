"""Acceptance and oracle tests for offline readback scheduling/settlement.

The scenarios come from the readback data contract: no device is involved, all
captures are synthetic.  One test additionally performs settlement in a fresh
``python -I`` subprocess to prove the continuation document is self-contained.
"""
import itertools
import json
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from telemetry_trellis import (
    ReadbackError,
    load_continuation,
    plan_readback,
    save_continuation,
    save_order,
    settle_readback,
)
from telemetry_trellis.code import encode
from telemetry_trellis.readback import _optimal_payloads, _validate_request

SRC_PATH = str(Path(__file__).resolve().parents[1] / 'src')

BASE_REQUEST = {
    'capture_id': 'cap-1', 'constraint': 2, 'generators': [1, 2],
    'payload_length': 3, 'terminated': True,
    'received': [None, 0, 0, None, 0, None, 0, 0],
}
ORIGINAL_WINDOWS = [
    {'id': 'w0', 'start': 0, 'end': 4, 'fee': 1},
    {'id': 'w1', 'start': 0, 'end': 1, 'fee': 3},
    {'id': 'w2', 'start': 3, 'end': 6, 'fee': 2},
]


def windows_with(w0=1, w1=3, w2=2):
    return [
        {'id': 'w0', 'start': 0, 'end': 4, 'fee': w0},
        {'id': 'w1', 'start': 0, 'end': 1, 'fee': w1},
        {'id': 'w2', 'start': 3, 'end': 6, 'fee': w2},
    ]


def plan(quota=3, windows=None, **overrides):
    request = {**BASE_REQUEST, 'quota': quota,
               'windows': ORIGINAL_WINDOWS if windows is None else windows}
    request.update(overrides)
    return plan_readback(request)


def reply_for(bundle, entries):
    return {'schedule_id': bundle['schedule_id'], 'windows': entries}


class ReadbackPlanningTests(unittest.TestCase):
    # --------------------------------------------------------- acceptance 1
    def test_quota_two_has_no_schedule_even_though_w0_covers_both_coords(self):
        result = plan(2)
        self.assertEqual(result['status'], 'unschedulable')
        self.assertEqual(result['distance'], 0)
        self.assertEqual(result['optimal_paths'], 2)
        self.assertIsNone(result['schedule_id'])
        self.assertEqual(result['selected_windows'], [])

    def test_quota_three_picks_w0_w2_with_exact_requested_coordinates(self):
        result = plan(3)
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(result['selected_windows'], ['w0', 'w2'])
        self.assertEqual(result['total_fee'], 3)
        order = result['readback_order']
        self.assertEqual(order['capture_id'], 'cap-1')
        self.assertEqual(order['schedule_id'], result['schedule_id'])
        by_id = {w['window_id']: w for w in order['windows']}
        self.assertEqual(by_id['w0']['requested_coordinates'], [0, 3])
        self.assertEqual(by_id['w2']['requested_coordinates'], [3, 5])
        # The receiver order never contains the capture or candidate payloads.
        serialized = json.dumps(order)
        self.assertNotIn('received', serialized)
        self.assertNotIn('payload', serialized)

    def test_fee_tie_breaks_on_lexicographic_window_id_list(self):
        result = plan(3, windows_with(w1=1))
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(result['total_fee'], 2)
        self.assertEqual(result['selected_windows'], ['w0', 'w1'])

        result = plan(3, windows_with(w1=2))
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(result['total_fee'], 3)
        # w0+w1 and w0+w2 both cost 3; the smaller ID list wins.
        self.assertEqual(result['selected_windows'], ['w0', 'w1'])

    def test_schedule_id_is_stable_under_field_and_catalogue_reordering(self):
        first = plan(3)
        shuffled = {
            'windows': list(reversed(ORIGINAL_WINDOWS)),
            'quota': 3,
            'received': list(BASE_REQUEST['received']),
            'terminated': True,
            'payload_length': 3,
            'generators': [1, 2],
            'constraint': 2,
            'capture_id': 'cap-1',
        }
        second = plan_readback(shuffled)
        self.assertEqual(second['schedule_id'], first['schedule_id'])

        changed = plan(5, windows_with(w0=5))
        self.assertNotEqual(changed['schedule_id'], first['schedule_id'])

    def test_unique_and_too_many_statuses_are_distinguishable(self):
        unique = plan_readback({
            'capture_id': 'cap-u', 'constraint': 2, 'generators': [1, 2],
            'payload_length': 3, 'terminated': True,
            'received': encode([0, 0, 0], 2, [1, 2], True)['symbols'],
            'quota': 3, 'windows': ORIGINAL_WINDOWS,
        })
        self.assertEqual(unique['status'], 'unique')
        self.assertEqual(unique['optimal_paths'], 1)
        self.assertIsNone(unique['readback_order'])

        length = 500
        constraint = 3
        too_many = plan_readback({
            'capture_id': 'cap-big', 'constraint': constraint,
            'generators': [7, 5], 'payload_length': length,
            'terminated': True,
            'received': [None] * ((length + constraint - 1) * 2),
            'quota': 16000,
            'windows': [{'id': 'w0', 'start': 0, 'end': 1, 'fee': 1}],
        })
        self.assertEqual(too_many['status'], 'too_many_candidates')
        self.assertEqual(too_many['optimal_paths'], 2 ** 500)
        self.assertEqual(too_many['distance'], 0)
        self.assertIsNone(too_many['readback_order'])

    def test_request_validation_rejects_booleans_ranges_and_known_bits(self):
        bad_cases = [
            {'quota': True},
            {'quota': -1},
            {'windows': [{'id': 'w0', 'start': 0, 'end': 9, 'fee': 1}]},
            {'windows': [{'id': 'w0', 'start': 2, 'end': 2, 'fee': 1}]},
            {'windows': [{'id': 'w0', 'start': 0, 'end': 1, 'fee': 0}]},
            {'windows': [{'id': 'w0', 'start': 0, 'end': 1, 'fee': True}]},
        ]
        for override in bad_cases:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    plan(**override)
        duplicate_id = ORIGINAL_WINDOWS + [
            {'id': 'w0', 'start': 6, 'end': 7, 'fee': 1}]
        with self.assertRaises(ValueError):
            plan(3, duplicate_id)
        request = {**BASE_REQUEST, 'quota': 3, 'windows': ORIGINAL_WINDOWS}
        request['received'] = list(request['received'])
        request['received'][1] = True
        with self.assertRaises(ValueError):
            plan_readback(request)

    def test_window_with_only_known_symbols_requests_nothing(self):
        # Index 1 is a known 0; the window is valid but cannot distinguish
        # anything, so it must never appear in a feasible schedule.
        windows = ORIGINAL_WINDOWS + [
            {'id': 'w9', 'start': 1, 'end': 2, 'fee': 1}]
        result = plan(3, windows)
        self.assertEqual(result['selected_windows'], ['w0', 'w2'])
        self.assertEqual(
            result['readback_order']['windows'][0]['requested_coordinates'],
            [0, 3])

    def test_identical_reencoded_codewords_are_unschedulable(self):
        # constraint 3, generators (2,4): payloads [0] and [1] produce the
        # same one-step word (0,0) on an unterminated one-bit frame.  There is
        # no erased coordinate that could ever distinguish them.
        request = {
            'capture_id': 'cap-same', 'constraint': 3, 'generators': [2, 4],
            'payload_length': 1, 'terminated': False,
            'received': [None, None], 'quota': 100,
            'windows': [
                {'id': 'wA', 'start': 0, 'end': 2, 'fee': 1},
                {'id': 'wB', 'start': 0, 'end': 1, 'fee': 1},
                {'id': 'wC', 'start': 1, 'end': 2, 'fee': 1},
            ],
        }
        result = plan_readback(request)
        self.assertEqual(result['status'], 'unschedulable')
        self.assertEqual(result['optimal_paths'], 2)
        self.assertEqual(result['distance'], 0)

    def test_optimal_enumeration_matches_brute_force_oracle(self):
        descriptor = {'constraint': 2, 'generators': [3, 1],
                      'payload_length': 3, 'terminated': True}
        steps = descriptor['payload_length'] + 1
        for received in itertools.product((0, 1, None), repeat=steps * 2):
            request = {
                'capture_id': 'cap-o', **descriptor,
                'received': list(received), 'quota': 100,
                'windows': [{'id': 'w0', 'start': 0,
                             'end': steps * 2, 'fee': 1}],
            }
            req = _validate_request(request)
            scored = []
            for bits in itertools.product((0, 1), repeat=steps):
                if bits[descriptor['payload_length']:] != (0,):
                    continue
                word = encode(bits, 2, [3, 1], True)['symbols']
                cost = sum(a is not None and a != b
                           for a, b in zip(received, word))
                scored.append((cost, bits[:descriptor['payload_length']]))
            best = min(cost for cost, _ in scored)
            expected = sorted({payload for cost, payload in scored
                               if cost == best})
            if len(expected) <= 32:
                payloads = _optimal_payloads(req, best)
                self.assertEqual([list(p) for p in payloads],
                                 [list(p) for p in expected])

    def test_window_selection_matches_brute_force_subset_oracle(self):
        # Four-symbol unterminated frame, three overlapping windows.  Every
        # 0/1/null received pattern is checked against an independent oracle
        # that enumerates all 2**3 window subsets and applies the same rule:
        # each optimal-payload pair must be distinguished by >= 2 selected
        # windows, minimum total fee, lexicographic ID-list tie-break.
        descriptor = {'constraint': 2, 'generators': [3, 1],
                      'payload_length': 2, 'terminated': False}
        steps = 2
        quota = 4
        catalogue = [
            {'id': 'w0', 'start': 0, 'end': 2, 'fee': 2},
            {'id': 'w1', 'start': 1, 'end': 4, 'fee': 1},
            {'id': 'w2', 'start': 0, 'end': 4, 'fee': 3},
        ]
        codewords = {
            bits: encode(bits, 2, [3, 1], False)['symbols']
            for bits in itertools.product((0, 1), repeat=steps)}
        for received in itertools.product((0, 1, None), repeat=steps * 2):
            request = {
                'capture_id': 'cap-o', **descriptor,
                'received': list(received), 'quota': quota,
                'windows': catalogue,
            }
            result = plan_readback(request)
            scored = []
            for bits, word in codewords.items():
                cost = sum(a is not None and a != b
                           for a, b in zip(received, word))
                scored.append((cost, bits))
            best = min(cost for cost, _ in scored)
            winners = sorted({bits for cost, bits in scored if cost == best})
            if len(winners) == 1:
                self.assertEqual(result['status'], 'unique')
                continue
            # Oracle distinction structure on erased coordinates only.
            erased = [i for i, symbol in enumerate(received) if symbol is None]
            window_bits = {
                w['id']: [i for i in erased
                          if w['start'] <= i < w['end']]
                for w in catalogue}
            pair_masks = []
            for a in range(len(winners)):
                for b in range(a + 1, len(winners)):
                    mask = 0
                    for bit_index, window in enumerate(catalogue):
                        coords = window_bits[window['id']]
                        if any(codewords[winners[a]][i]
                               != codewords[winners[b]][i] for i in coords):
                            mask |= 1 << bit_index
                    pair_masks.append(mask)
            oracle_choice = None
            for subset in range(1, 1 << len(catalogue)):
                if not all((mask & subset).bit_count() >= 2
                           for mask in pair_masks):
                    continue
                fee = sum(catalogue[i]['fee'] for i in range(len(catalogue))
                          if subset & (1 << i))
                if fee > quota:
                    continue
                ids = tuple(catalogue[i]['id']
                            for i in range(len(catalogue))
                            if subset & (1 << i))
                candidate = (fee, ids)
                if oracle_choice is None or candidate < oracle_choice:
                    oracle_choice = candidate
            if oracle_choice is None:
                self.assertEqual(result['status'], 'unschedulable')
            else:
                self.assertEqual(result['status'], 'scheduled')
                self.assertEqual(tuple(result['selected_windows']),
                                 oracle_choice[1])
                self.assertEqual(result['total_fee'], oracle_choice[0])
                # Catalogue order has no business meaning.
                shuffled = plan_readback({
                    **request, 'windows': list(reversed(catalogue))})
                self.assertEqual(shuffled['selected_windows'],
                                 result['selected_windows'])
                self.assertEqual(shuffled['schedule_id'],
                                 result['schedule_id'])

    def test_candidate_limit_boundary_is_inclusive_at_32(self):
        # All-erased unterminated frame: every state path is optimal, so five
        # payload bits give exactly 32 optimal payloads (still scheduled) and
        # six give 64 (too many, no enumeration attempted).
        windows = [{'id': f'w{i}', 'start': i, 'end': i + 1, 'fee': 1}
                   for i in range(12)]
        at_limit = plan_readback({
            'capture_id': 'cap-32', 'constraint': 2, 'generators': [3, 1],
            'payload_length': 5, 'terminated': False,
            'received': [None] * 10, 'quota': 16000, 'windows': windows[:10],
        })
        self.assertEqual(at_limit['optimal_paths'], 32)
        self.assertEqual(at_limit['status'], 'scheduled')
        self.assertIsNotNone(at_limit['continuation'])

        over_limit = plan_readback({
            'capture_id': 'cap-64', 'constraint': 2, 'generators': [3, 1],
            'payload_length': 6, 'terminated': False,
            'received': [None] * 12, 'quota': 16000, 'windows': windows,
        })
        self.assertEqual(over_limit['optimal_paths'], 64)
        self.assertEqual(over_limit['status'], 'too_many_candidates')
        self.assertIsNone(over_limit['continuation'])


class ReadbackSettlementTests(unittest.TestCase):
    def setUp(self):
        self.result = plan(3)
        self.bundle = self.result['continuation']
        self.schedule_id = self.bundle['schedule_id']

    # --------------------------------------------------------- acceptance 2
    def test_unavailable_window_still_allows_unique_decode(self):
        result = settle_readback(self.bundle, reply_for(self.bundle, [
            {'window_id': 'w0', 'available': False},
            {'window_id': 'w2', 'available': True,
             'bits': {'3': 1, '5': 0}},
        ]))
        decode = result['decode']
        self.assertEqual(result['unavailable_windows'], ['w0'])
        self.assertEqual(result['filled_indices'], [3, 5])
        self.assertEqual(decode['payload'], [1, 0, 0])
        self.assertEqual(decode['distance'], 0)
        self.assertEqual(decode['optimal_paths'], 1)
        self.assertEqual(decode['erasure_indices'], [0])

    def test_successful_windows_can_promote_a_previously_worse_path(self):
        result = settle_readback(self.bundle, reply_for(self.bundle, [
            {'window_id': 'w0', 'available': True,
             'bits': {'0': 1, '3': 1}},
            {'window_id': 'w2', 'available': True,
             'bits': {'3': 1, '5': 1}},
        ]))
        decode = result['decode']
        self.assertEqual(decode['distance'], 1)
        self.assertEqual(decode['optimal_paths'], 2)
        self.assertTrue(decode['ambiguous'])
        self.assertEqual(decode['payload'], [1, 0, 0])
        self.assertEqual(decode['corrected_indices'], [5])
        self.assertEqual(decode['erasure_indices'], [])
        self.assertEqual(result['filled_indices'], [0, 3, 5])

    def test_shared_coordinate_counts_as_one_observation_and_tie_remains(self):
        result = settle_readback(self.bundle, reply_for(self.bundle, [
            {'window_id': 'w0', 'available': True,
             'bits': {'0': 0, '3': 1}},
            {'window_id': 'w2', 'available': True,
             'bits': {'3': 1, '5': 0}},
        ]))
        decode = result['decode']
        self.assertEqual(decode['optimal_paths'], 2)
        self.assertTrue(decode['ambiguous'])
        self.assertEqual(decode['payload'], [0, 0, 0])
        self.assertEqual(result['filled_indices'], [0, 3, 5])

    def test_conflicting_values_reject_the_whole_settlement(self):
        with self.assertRaises(ReadbackError):
            settle_readback(self.bundle, reply_for(self.bundle, [
                {'window_id': 'w0', 'available': True,
                 'bits': {'0': 0, '3': 1}},
                {'window_id': 'w2', 'available': True,
                 'bits': {'3': 0, '5': 0}},
            ]))

    def test_every_illegal_reply_shape_is_rejected(self):
        legal_bits = {'3': 1, '5': 0}
        illegal = [
            # missing selected window
            [{'window_id': 'w2', 'available': True, 'bits': legal_bits}],
            # extra / unselected window
            [{'window_id': 'w0', 'available': False},
             {'window_id': 'w1', 'available': False},
             {'window_id': 'w2', 'available': True, 'bits': legal_bits}],
            # duplicate entry
            [{'window_id': 'w0', 'available': False},
             {'window_id': 'w0', 'available': False},
             {'window_id': 'w2', 'available': True, 'bits': legal_bits}],
            # unavailable window carrying bits
            [{'window_id': 'w0', 'available': False, 'bits': {'0': 1}},
             {'window_id': 'w2', 'available': True, 'bits': legal_bits}],
            # coordinate outside the scheduled request (index 1 was known)
            [{'window_id': 'w0', 'available': True,
              'bits': {'0': 1, '1': 0}},
             {'window_id': 'w2', 'available': True, 'bits': legal_bits}],
            # missing one requested coordinate
            [{'window_id': 'w0', 'available': True, 'bits': {'0': 1}},
             {'window_id': 'w2', 'available': True, 'bits': legal_bits}],
            # non-strict bit value
            [{'window_id': 'w0', 'available': True,
              'bits': {'0': True, '3': 1}},
             {'window_id': 'w2', 'available': True, 'bits': legal_bits}],
        ]
        for entries in illegal:
            with self.subTest(entries=entries):
                with self.assertRaises(ReadbackError):
                    settle_readback(self.bundle,
                                    reply_for(self.bundle, entries))
        # foreign schedule id even though window data is otherwise valid
        foreign = dict(reply_for(self.bundle, [
            {'window_id': 'w0', 'available': False},
            {'window_id': 'w2', 'available': True, 'bits': legal_bits}]))
        foreign['schedule_id'] = 'rb-' + '0' * 32
        with self.assertRaises(ReadbackError):
            settle_readback(self.bundle, foreign)

    def test_malformed_continuation_documents_are_rejected(self):
        for mutate in (
                lambda doc: doc.update(kind='other'),
                lambda doc: doc.update(version=99),
                lambda doc: doc.pop('request'),
                lambda doc: doc.update(schedule_id='rb-' + 'f' * 32),
                lambda doc: doc.update(selected_window_ids=['w9']),
        ):
            bad = copy.deepcopy(self.bundle)
            mutate(bad)
            with self.subTest(mutate=mutate.__name__):
                with self.assertRaises(ReadbackError):
                    settle_readback(bad, reply_for(bad, []))
        with self.assertRaises(ReadbackError):
            settle_readback(['not', 'a', 'dict'], {})

    def test_rejected_settlement_leaves_inputs_and_continuation_untouched(self):
        snapshot = copy.deepcopy(self.bundle)
        original_received = list(BASE_REQUEST['received'])
        with self.assertRaises(ReadbackError):
            settle_readback(self.bundle, reply_for(self.bundle, [
                {'window_id': 'w0', 'available': True,
                 'bits': {'0': 0, '3': 1}},
                {'window_id': 'w2', 'available': True,
                 'bits': {'3': 0, '5': 0}},
            ]))
        self.assertEqual(self.bundle, snapshot)
        self.assertEqual(BASE_REQUEST['received'], original_received)

    def test_reply_window_order_is_irrelevant_and_json_round_trips(self):
        entries = [
            {'window_id': 'w2', 'available': True,
             'bits': {'3': 1, '5': 0}},
            {'window_id': 'w0', 'available': True,
             'bits': {'0': 1, '3': 1}},
        ]
        first = settle_readback(self.bundle,
                                reply_for(self.bundle, entries))
        restored = json.loads(json.dumps(self.bundle))
        second = settle_readback(restored,
                                 reply_for(restored, list(reversed(entries))))
        self.assertEqual(second, first)
        self.assertEqual(second['filled_indices'], [0, 3, 5])

    def test_multiple_unavailable_windows_still_settle(self):
        result = settle_readback(self.bundle, reply_for(self.bundle, [
            {'window_id': 'w0', 'available': False},
            {'window_id': 'w2', 'available': False},
        ]))
        self.assertEqual(result['filled_indices'], [])
        self.assertEqual(sorted(result['unavailable_windows']),
                         ['w0', 'w2'])
        decode = result['decode']
        self.assertEqual(decode['optimal_paths'], 2)
        self.assertEqual(decode['erasure_indices'], [0, 3, 5])

    def test_original_request_binding_and_continuation_tampering(self):
        settle_readback(self.bundle, reply_for(self.bundle, [
            {'window_id': 'w0', 'available': False},
            {'window_id': 'w2', 'available': True, 'bits': {'3': 1, '5': 0}},
        ]), original_request={**BASE_REQUEST, 'quota': 3,
                              'windows': ORIGINAL_WINDOWS})
        changed = {**BASE_REQUEST, 'quota': 9,
                   'windows': ORIGINAL_WINDOWS}
        with self.assertRaises(ReadbackError):
            settle_readback(self.bundle,
                            reply_for(self.bundle, []),
                            original_request=changed)
        tampered = json.loads(json.dumps(self.bundle))
        tampered['request']['received'][1] = 1
        with self.assertRaises(ReadbackError):
            settle_readback(tampered, reply_for(tampered, []))

    # --------------------------------------------------------- acceptance 3
    def test_two_schedules_for_one_capture_are_independent(self):
        other = plan(5, windows_with(w0=5))
        self.assertEqual(other['selected_windows'], ['w1', 'w2'])
        self.assertNotEqual(other['schedule_id'], self.schedule_id)
        bundle_b = other['continuation']

        # A reply that is perfectly valid for schedule B must be rejected by A.
        cross_reply = reply_for(bundle_b, [
            {'window_id': 'w1', 'available': True, 'bits': {'0': 1}},
            {'window_id': 'w2', 'available': True,
             'bits': {'3': 1, '5': 0}},
        ])
        with self.assertRaises(ReadbackError):
            settle_readback(self.bundle, cross_reply)

        # Even a window shared by both schedules with identical coordinates and
        # values (w2 -> {3: 1, 5: 0}) cannot be settled under the wrong order,
        # whether the mismatch is a foreign schedule id or a missing window.
        shared_w2 = [
            {'window_id': 'w2', 'available': True,
             'bits': {'3': 1, '5': 0}}]
        with self.assertRaises(ReadbackError):
            settle_readback(bundle_b,
                            reply_for(self.bundle, shared_w2))
        with self.assertRaises(ReadbackError):
            settle_readback(bundle_b, reply_for(bundle_b, shared_w2))

        # Each schedule still settles correctly with its own material.
        settled_a = settle_readback(self.bundle, reply_for(self.bundle, [
            {'window_id': 'w0', 'available': False},
            {'window_id': 'w2', 'available': True,
             'bits': {'3': 1, '5': 0}},
        ]))
        settled_b = settle_readback(bundle_b, cross_reply)
        self.assertEqual(settled_a['decode']['optimal_paths'], 1)
        self.assertEqual(settled_b['decode']['payload'], [1, 0, 0])
        self.assertEqual(settled_b['filled_indices'], [0, 3, 5])

    def test_reloaded_continuation_settles_in_a_fresh_process(self):
        runner_dir = Path(tempfile.mkdtemp(prefix='readback-runner-'))
        work_dir = Path(tempfile.mkdtemp(prefix='readback-data-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(runner_dir,
                                                            ignore_errors=True))
        self.addCleanup(lambda: __import__('shutil').rmtree(work_dir,
                                                            ignore_errors=True))
        bundle_path = work_dir / 'continuation.json'
        order_path = work_dir / 'order.json'
        output_path = work_dir / 'settled.json'
        save_continuation(bundle_path, self.bundle)
        save_order(order_path, self.result['readback_order'])

        runner = runner_dir / 'runner.py'
        runner.write_text(
            'import json, sys\n'
            f"sys.path.insert(0, {SRC_PATH!r})\n"
            'from telemetry_trellis import load_continuation, settle_readback\n'
            'bundle = load_continuation(sys.argv[1])\n'
            "reply = {'schedule_id': bundle['schedule_id'], 'windows': [\n"
            "    {'window_id': 'w0', 'available': False},\n"
            "    {'window_id': 'w2', 'available': True,\n"
            "     'bits': {'3': 1, '5': 0}}]}\n"
            'result = settle_readback(bundle, reply)\n'
            "with open(sys.argv[2], 'w', encoding='utf-8') as handle:\n"
            '    json.dump(result, handle)\n'
            "print('fresh-process-marker')\n")
        # -I ignores PYTHONPATH; the runner inserts the source tree itself.
        completed = subprocess.run(
            [sys.executable, '-I', str(runner), str(bundle_path),
             str(output_path)],
            capture_output=True, text=True, check=True)
        self.assertIn('fresh-process-marker', completed.stdout)
        settled = json.loads(output_path.read_text(encoding='utf-8'))
        self.assertEqual(settled['schedule_id'], self.schedule_id)
        self.assertEqual(settled['decode']['payload'], [1, 0, 0])
        self.assertEqual(settled['decode']['optimal_paths'], 1)
        # Reloading again yields the same binding and the order stayed intact.
        reloaded = load_continuation(bundle_path)
        self.assertEqual(reloaded['schedule_id'], self.schedule_id)
        order = json.loads(order_path.read_text(encoding='utf-8'))
        self.assertEqual([w['window_id'] for w in order['windows']],
                         ['w0', 'w2'])

    def test_long_frame_small_tie_uses_real_schedule_and_redecode(self):
        length, constraint = 500, 2
        symbol_count = (length + constraint - 1) * 2
        first = 2 * (length - 1)
        second = first + 3
        received = [0] * symbol_count
        received[first] = None
        received[second] = None
        request = {
            'capture_id': 'cap-long', 'constraint': constraint,
            'generators': [1, 2], 'payload_length': length,
            'terminated': True, 'received': received, 'quota': 10,
            'windows': [
                {'id': 'wA', 'start': first, 'end': first + 1, 'fee': 1},
                {'id': 'wB', 'start': second, 'end': second + 1, 'fee': 1},
                {'id': 'wC', 'start': first, 'end': second + 1, 'fee': 5},
            ],
        }
        result = plan_readback(request)
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(result['optimal_paths'], 2)
        self.assertEqual(result['selected_windows'], ['wA', 'wB'])

        # Filling only one of the two distinguishing coordinates resolves the
        # original zero-distance tie: the whole legal path set is re-judged
        # rather than filtering the old two winners.
        partial = settle_readback(result['continuation'], {
            'schedule_id': result['schedule_id'], 'windows': [
                {'window_id': 'wA', 'available': False},
                {'window_id': 'wB', 'available': True,
                 'bits': {str(second): 0}},
            ]})
        self.assertEqual(partial['filled_indices'], [second])
        self.assertEqual(partial['unavailable_windows'], ['wA'])
        self.assertFalse(partial['decode']['ambiguous'])
        self.assertEqual(partial['decode']['optimal_paths'], 1)
        self.assertEqual(partial['decode']['erasure_indices'], [first])

        full = settle_readback(result['continuation'], {
            'schedule_id': result['schedule_id'], 'windows': [
                {'window_id': 'wA', 'available': True,
                 'bits': {str(first): 1}},
                {'window_id': 'wB', 'available': True,
                 'bits': {str(second): 1}},
            ]})
        self.assertEqual(full['filled_indices'], [first, second])
        self.assertEqual(full['decode']['optimal_paths'], 1)
        self.assertEqual(full['decode']['distance'], 0)
        self.assertEqual(full['decode']['payload'][-1], 1)


if __name__ == '__main__':
    unittest.main()
