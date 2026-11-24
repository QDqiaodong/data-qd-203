import copy
import itertools
import random
import unittest

from telemetry_trellis import SessionError, decode_session, process
from telemetry_trellis.code import transition
from telemetry_trellis.decode import decode as decode_frame


def _two_frame_session(terminated, tail):
    return {
        'session_id': 'acceptance', 'constraint': 2, 'generators': [2, 3],
        'terminated': terminated, 'tail_received': tail,
        'frames': [
            {'id': 'A', 'payload_length': 1, 'received': [0, 1]},
            {'id': 'B', 'payload_length': 1, 'received': [1, 1]},
        ],
    }


class SessionAcceptanceTests(unittest.TestCase):
    def test_joint_decoding_beats_per_frame_stitching(self):
        result = decode_session(_two_frame_session(False, []))
        self.assertEqual(result['distance'], 0)
        self.assertEqual(result['optimal_paths'], 1)
        self.assertFalse(result['ambiguous'])
        self.assertEqual(result['payload'], [1, 0])
        self.assertEqual(result['frames'], [
            {'id': 'A', 'range': [0, 1], 'payload': [1], 'ambiguous': False},
            {'id': 'B', 'range': [1, 2], 'payload': [0], 'ambiguous': False},
        ])
        # B decoded independently from the zero state prefers bit 1, so the
        # joint answer [0] can only come from state continuation.
        alone = decode_frame(
            {'id': 'B', 'length': 1, 'terminated': False,
             'received': [1, 1]}, 2, [2, 3])
        self.assertEqual(alone['payload'], [1])
        self.assertEqual(result['frames'][1]['payload'], [0])

    def test_single_erased_step_is_globally_ambiguous(self):
        result = decode_session({
            'session_id': 'erased', 'constraint': 2, 'generators': [2, 3],
            'terminated': False, 'tail_received': [],
            'frames': [{'id': 'X', 'payload_length': 1,
                        'received': [None, None]}],
        })
        self.assertEqual(result['optimal_paths'], 2)
        self.assertEqual(result['payload'], [0])
        self.assertTrue(result['ambiguous'])
        self.assertTrue(result['frames'][0]['ambiguous'])
        # The representative must not be presented as a confirmed reading.
        self.assertIsNone(result['frames'][0].get('effective_payload'))

    def test_frame_array_order_drives_state_continuation(self):
        swapped = {
            'session_id': 'swapped', 'constraint': 2, 'generators': [2, 3],
            'terminated': False, 'tail_received': [],
            'frames': [
                {'id': 'B', 'payload_length': 1, 'received': [1, 1]},
                {'id': 'A', 'payload_length': 1, 'received': [0, 1]},
            ],
        }
        result = decode_session(swapped)
        self.assertEqual([frame['id'] for frame in result['frames']],
                         ['B', 'A'])
        self.assertEqual([frame['range'] for frame in result['frames']],
                         [[0, 1], [1, 2]])
        self.assertEqual(result['frames'][0]['payload'],
                         result['payload'][0:1])
        self.assertEqual(result['frames'][1]['payload'],
                         result['payload'][1:2])
        # Reordering the array changes the continuous decoding, proving ids
        # never determine encoding order.
        canonical = decode_session(_two_frame_session(False, []))
        self.assertNotEqual(result['payload'], canonical['payload'])

    def test_termination_tail_participates_once_after_all_frames(self):
        result = decode_session(_two_frame_session(True, [0, 0]))
        self.assertEqual(result['payload'], [1, 0])
        self.assertEqual(result['distance'], 0)
        self.assertEqual(result['optimal_paths'], 1)
        self.assertFalse(result['ambiguous'])
        # Tail positions are not attributed to any frame: the frame ranges
        # cover exactly the two payload bits and nothing else.
        ranges = [frame['range'] for frame in result['frames']]
        self.assertEqual(ranges, [[0, 1], [1, 2]])
        covered = [bit for frame in result['frames']
                   for bit in frame['payload']]
        self.assertEqual(covered, result['payload'])

    def test_all_erased_500_bit_terminated_session_count(self):
        result = decode_session({
            'session_id': 'erased-500', 'constraint': 2, 'generators': [2, 3],
            'terminated': True, 'tail_received': [None, None],
            'frames': [
                {'id': 'P', 'payload_length': 250, 'received': [None] * 500},
                {'id': 'Q', 'payload_length': 250, 'received': [None] * 500},
            ],
        })
        self.assertEqual(result['optimal_paths'], 2 ** 500)
        self.assertEqual(result['distance'], 0)
        self.assertEqual(result['payload'], [0] * 500)
        self.assertTrue(result['ambiguous'])
        self.assertTrue(all(frame['ambiguous'] for frame in result['frames']))
        self.assertEqual(result['frames'][0]['range'], [0, 250])
        self.assertEqual(result['frames'][1]['range'], [250, 500])

    def test_ambiguity_can_be_localized_to_one_frame(self):
        result = decode_session({
            'session_id': 'local', 'constraint': 2, 'generators': [2, 3],
            'terminated': False, 'tail_received': [],
            'frames': [
                {'id': 'A', 'payload_length': 1, 'received': [0, 1]},
                {'id': 'B', 'payload_length': 1, 'received': [None, None]},
            ],
        })
        self.assertEqual(result['optimal_paths'], 2)
        self.assertFalse(result['frames'][0]['ambiguous'])
        self.assertTrue(result['frames'][1]['ambiguous'])
        self.assertEqual(result['frames'][0]['payload'], [1])


class SessionRejectionTests(unittest.TestCase):
    def setUp(self):
        self.base = {
            'session_id': 's', 'constraint': 2, 'generators': [2, 3],
            'terminated': False, 'tail_received': [],
            'frames': [{'id': 'A', 'payload_length': 1,
                        'received': [0, 1]}],
        }

    def assert_rejected(self, mutate):
        request = copy.deepcopy(self.base)
        mutate(request)
        mutated_snapshot = copy.deepcopy(request)
        with self.assertRaises(SessionError):
            decode_session(request)
        # Rejection leaves the caller input untouched and never returns a
        # partial result.
        self.assertEqual(request, mutated_snapshot)

    def test_non_empty_tail_rejected_for_unterminated_session(self):
        self.assert_rejected(lambda r: r.update(tail_received=[0, 0]))

    def test_wrong_tail_length_rejected_for_terminated_session(self):
        self.assert_rejected(
            lambda r: r.update(terminated=True, tail_received=[0]))

    def test_duplicate_frame_ids_rejected(self):
        self.assert_rejected(lambda r: r['frames'].append(
            {'id': 'A', 'payload_length': 1, 'received': [0, 0]}))

    def test_illegal_symbols_rejected(self):
        self.assert_rejected(
            lambda r: r['frames'][0]['received'].__setitem__(0, True))
        self.assert_rejected(
            lambda r: r['frames'][0]['received'].__setitem__(0, 2))

    def test_length_identity_and_total_bounds_rejected(self):
        self.assert_rejected(
            lambda r: r['frames'][0].update(received=[0]))
        self.assert_rejected(
            lambda r: r['frames'][0].update(payload_length=0))
        self.assert_rejected(lambda r: r.update(frames=[
            {'id': f'F{i}', 'payload_length': 500, 'received': [0] * 1000}
            for i in range(41)]))
        self.assert_rejected(lambda r: r.update(frames=[]))
        self.assert_rejected(lambda r: r.update(frames=[
            {'id': f'F{i}', 'payload_length': 1, 'received': [0, 0]}
            for i in range(65)]))

    def test_parameter_and_shape_errors_rejected(self):
        self.assert_rejected(lambda r: r.update(terminated=1))
        self.assert_rejected(lambda r: r.update(extra=1))
        self.assert_rejected(lambda r: r.pop('session_id'))
        self.assert_rejected(lambda r: r.update(constraint=8))
        self.assert_rejected(lambda r: r.update(generators=[3, 3]))
        self.assert_rejected(lambda r: r.update(generators=[2, 4]))
        self.assert_rejected(lambda r: r.update(session_id='bad id!'))
        self.assert_rejected(
            lambda r: r['frames'][0].update(payload_length=True))
        self.assert_rejected(
            lambda r: r.update(terminated=True,
                               tail_received=[1, False]))

    def test_successful_call_does_not_mutate_input(self):
        request = copy.deepcopy(self.base)
        snapshot = copy.deepcopy(request)
        decode_session(request)
        self.assertEqual(request, snapshot)

    def test_size_boundaries(self):
        sixty_four = [
            {'id': f'F{i:02d}', 'payload_length': 250,
             'received': [None] * 500} for i in range(64)]
        result = decode_session(dict(self.base, frames=sixty_four))
        self.assertEqual(result['optimal_paths'], 2 ** 16000)
        self.assertEqual(result['frames'][-1]['range'], [15750, 16000])
        forty = [
            {'id': f'G{i:02d}', 'payload_length': 500,
             'received': [None] * 1000} for i in range(40)]
        result = decode_session(dict(self.base, frames=forty))
        self.assertEqual(result['optimal_paths'], 2 ** 20000)
        with self.assertRaises(SessionError):
            decode_session(dict(self.base, frames=forty + [
                {'id': 'extra', 'payload_length': 1,
                 'received': [None, None]}]))


class SessionOracleTests(unittest.TestCase):
    def _oracle(self, request):
        """Enumerate every legal whole-session payload as ground truth."""
        constraint = request['constraint']
        generators = request['generators']
        width = len(generators)
        lengths = [frame['payload_length'] for frame in request['frames']]
        total = sum(lengths)
        observations = []
        for frame in request['frames']:
            observations += [frame['received'][i:i + width]
                             for i in range(0, len(frame['received']), width)]
        tail_steps = constraint - 1 if request['terminated'] else 0
        observations += [request['tail_received'][i:i + width]
                         for i in range(0, len(request['tail_received']),
                                        width)]
        best = None
        winners = []
        for payload in itertools.product((0, 1), repeat=total):
            bits = list(payload) + [0] * tail_steps
            state = 0
            distance = 0
            for step, bit in enumerate(bits):
                state, word = transition(state, bit, constraint, generators)
                distance += sum(observed is not None and observed != expected
                                for observed, expected
                                in zip(observations[step], word))
            if request['terminated'] and state != 0:
                continue
            if best is None or distance < best:
                best, winners = distance, [tuple(payload)]
            elif distance == best:
                winners.append(tuple(payload))
        representative = list(min(winners))
        frame_ambiguity = []
        start = 0
        for length in lengths:
            slices = {winner[start:start + length] for winner in winners}
            frame_ambiguity.append(len(slices) > 1)
            start += length
        return best, len(winners), representative, frame_ambiguity

    def _random_request(self, rng, terminated):
        constraint = rng.choice([2, 3, 4])
        generator_count = rng.choice([2, 3])
        generators = rng.sample(range(1, 1 << constraint), generator_count)
        frame_count = rng.randint(1, 3)
        lengths = [rng.randint(1, 4) for _ in range(frame_count)]
        frames = []
        for index, length in enumerate(lengths):
            frames.append({
                'id': f'F{index}', 'payload_length': length,
                'received': [rng.choice([0, 1, None])
                             for _ in range(length * generator_count)],
            })
        tail = [rng.choice([0, 1, None]) for _ in range(
            (constraint - 1 if terminated else 0) * generator_count)]
        return {'session_id': 'oracle', 'constraint': constraint,
                'generators': generators, 'terminated': terminated,
                'tail_received': tail, 'frames': frames}

    def test_random_sessions_against_brute_force(self):
        rng = random.Random(20261124)
        for terminated in (False, True):
            for _ in range(60):
                request = self._random_request(rng, terminated)
                result = decode_session(request)
                distance, count, representative, frame_ambiguity = \
                    self._oracle(request)
                self.assertEqual(result['distance'], distance)
                self.assertEqual(result['optimal_paths'], count)
                self.assertEqual(result['payload'], representative)
                self.assertEqual(result['ambiguous'], count != 1)
                start = 0
                for frame, length, ambiguous in zip(
                        result['frames'],
                        [f['payload_length'] for f in request['frames']],
                        frame_ambiguity):
                    self.assertEqual(frame['range'],
                                     [start, start + length])
                    self.assertEqual(frame['payload'],
                                     representative[start:start + length])
                    self.assertEqual(frame['ambiguous'], ambiguous)
                    start += length

    def test_exhaustive_observations_on_fixed_terminated_geometry(self):
        # Two frames, constraint 3, two generators, 3 payload + 2 tail steps:
        # every one of the 3**10 observation patterns is scored, so the
        # frame-level ambiguity rule is checked exhaustively without relying
        # on random sampling.
        constraint, generators, widths = 3, [7, 5], 2
        lengths = [2, 1]
        symbol_count = (sum(lengths) + constraint - 1) * widths
        self.assertEqual(symbol_count, 10)
        for pattern in itertools.product((0, 1, None), repeat=symbol_count):
            pattern = list(pattern)
            frames = []
            cursor = 0
            for index, length in enumerate(lengths):
                size = length * widths
                frames.append({'id': f'F{index}', 'payload_length': length,
                               'received': pattern[cursor:cursor + size]})
                cursor += size
            request = {'session_id': 'exhaustive',
                       'constraint': constraint, 'generators': generators,
                       'terminated': True,
                       'tail_received': pattern[cursor:], 'frames': frames}
            result = decode_session(request)
            distance, count, representative, frame_ambiguity = \
                self._oracle(request)
            self.assertEqual(result['distance'], distance)
            self.assertEqual(result['optimal_paths'], count)
            self.assertEqual(result['payload'], representative)
            self.assertEqual([f['ambiguous'] for f in result['frames']],
                             frame_ambiguity)


class LegacyCompatibilityTests(unittest.TestCase):
    def test_single_frame_process_contract_unchanged(self):
        # The legacy service still treats each frame as an independent
        # zero-state record; the session API never alters that behavior.
        legacy = process({
            'constraint': 2, 'generators': [2, 3],
            'frames': [
                {'id': 'A', 'payload_length': 1, 'terminated': False,
                 'received': [0, 1]},
                {'id': 'B', 'payload_length': 1, 'terminated': False,
                 'received': [1, 1]},
            ],
        })
        self.assertEqual([frame['payload'] for frame in legacy['frames']],
                         [[1], [1]])
        self.assertEqual(legacy['summary']['frames'], 2)

    def test_frame_ranges_partition_the_whole_payload(self):
        request = {
            'session_id': 'partition', 'constraint': 3, 'generators': [7, 5, 3],
            'terminated': True, 'tail_received': [0, 0, 0, 0, 0, 0],
            'frames': [
                {'id': 'a', 'payload_length': 3, 'received': [None] * 9},
                {'id': 'b', 'payload_length': 1, 'received': [None] * 3},
                {'id': 'c', 'payload_length': 2, 'received': [None] * 6},
            ],
        }
        result = decode_session(request)
        ranges = [frame['range'] for frame in result['frames']]
        self.assertEqual(ranges, [[0, 3], [3, 4], [4, 6]])
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], 6)
        for earlier, later in zip(ranges, ranges[1:]):
            self.assertEqual(earlier[1], later[0])
        # Only payloads whose forced two-zero tail returns the register to
        # state zero stay eligible: 2**(payload - (constraint-1)) here.
        self.assertEqual(result['optimal_paths'], 2 ** 4)

    def test_unsorted_ids_keep_array_order_and_ranges(self):
        request = {
            'session_id': 'ids', 'constraint': 2, 'generators': [2, 3],
            'terminated': False, 'tail_received': [],
            'frames': [
                {'id': 'zeta', 'payload_length': 2,
                 'received': [0, 1, 0, 0]},
                {'id': 'alpha', 'payload_length': 1,
                 'received': [None, None]},
                {'id': 'mid', 'payload_length': 1,
                 'received': [1, 1]},
            ],
        }
        result = decode_session(request)
        self.assertEqual([frame['id'] for frame in result['frames']],
                         ['zeta', 'alpha', 'mid'])
        self.assertEqual([frame['range'] for frame in result['frames']],
                         [[0, 2], [2, 3], [3, 4]])
        self.assertEqual(len(result['payload']), 4)


if __name__ == '__main__':
    unittest.main()
