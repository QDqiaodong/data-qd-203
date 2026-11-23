"""Exhaustive oracle checks for the multi-copy merge decoder."""
import itertools
import unittest

from telemetry_trellis.code import encode, transition
from telemetry_trellis.merge import copy_discrepancies, merge_decode


def path_metric(received_copies, word):
    distance = 0
    for received in received_copies:
        for observed, expected in zip(received, word):
            if observed is not None and observed != expected:
                distance += 1
    return distance


def brute_force(descriptor, copies):
    """Enumerate every state path (not just payload) exhaustively."""
    constraint = descriptor['constraint']
    generators = descriptor['generators']
    width = len(generators)
    steps = descriptor['length'] + (
        constraint - 1 if descriptor['terminated'] else 0)

    def walk(state, step, prefix, word):
        if step == steps:
            if descriptor['terminated'] and state != 0:
                return
            distance = path_metric(copies, word)
            yield distance, prefix, list(word)
            return
        for bit in (0, 1):
            target, output = transition(state, bit, constraint, generators)
            yield from walk(target, step + 1, prefix + (bit,),
                            word + list(output))

    paths = list(walk(0, 0, (), []))
    best = min(distance for distance, _, _ in paths)
    winners = [(prefix, word) for distance, prefix, word in paths
               if distance == best]
    payloads = sorted(list(prefix[:descriptor['length']])
                      for prefix, _ in winners)
    representative = payloads[0]
    return best, len(winners), representative


class MergeOracleTests(unittest.TestCase):
    def test_unterminated_two_bit_exhaustive(self):
        descriptor = {'constraint': 2, 'generators': [3, 1],
                      'length': 2, 'terminated': False}
        symbols = 4
        for first in itertools.product((0, 1, None), repeat=symbols):
            for second in itertools.product((0, 1, None), repeat=symbols):
                copies = [list(first), list(second)]
                result = merge_decode(descriptor, copies)
                best, count, representative = brute_force(descriptor, copies)
                self.assertEqual(result['distance'], best)
                self.assertEqual(result['optimal_paths'], count)
                self.assertEqual(result['payload'], representative)
                self.assertEqual(result['ambiguous'], count != 1)

    def test_conflicting_observations_both_count(self):
        descriptor = {'constraint': 2, 'generators': [3, 1],
                      'length': 1, 'terminated': False}
        # Copy 1 observed 0 at index 0; copy 2 observed 1 at the same index.
        # Payload [0] -> word [0,1]: copy1 matches fully, copy2 mismatches
        # index 0 (cost 1).  Payload [1] -> word [1,1]: the mirror image
        # (cost 1).  Voting would hide the conflict; the merge keeps both.
        copies = [[0, 1], [1, 1]]
        result = merge_decode(descriptor, copies)
        self.assertEqual(result['distance'], 1)
        self.assertEqual(result['optimal_paths'], 1)
        self.assertFalse(result['ambiguous'])
        best, count, representative = brute_force(descriptor, copies)
        self.assertEqual((result['distance'], result['optimal_paths'],
                          result['payload']), (best, count, representative))

        # A symmetric conflict over the whole word yields a genuine tie:
        # word [0,1] and word [1,1] each disagree with exactly one copy once.
        tie_copies = [[0, 1], [1, 1], [1, 0]]
        result = merge_decode(descriptor, tie_copies)
        best, count, representative = brute_force(descriptor, tie_copies)
        self.assertEqual(result['distance'], best)
        self.assertEqual(result['optimal_paths'], count)
        self.assertEqual(result['ambiguous'], count != 1)
        self.assertEqual(result['payload'], representative)
        self.assertGreaterEqual(count, 1)

    def test_conflicting_hard_bits_never_voted(self):
        descriptor = {'constraint': 2, 'generators': [3, 1],
                      'length': 2, 'terminated': False}
        # Each copy is itself a legal codeword (single-copy distance 0), but
        # they conflict at indices 2 and 3.  A voting pre-step would collapse
        # them into one hard-decision array and hide one side of the conflict;
        # the merge must charge both disagreements instead.
        first = [0, 0, 0, 0]
        second = [0, 0, 1, 1]
        self.assertEqual(merge_decode(descriptor, [first])['distance'], 0)
        self.assertEqual(merge_decode(descriptor, [second])['distance'], 0)
        merged = merge_decode(descriptor, [first, second])
        self.assertEqual(merged['distance'], 2)
        best, count, representative = brute_force(
            descriptor, [first, second])
        self.assertEqual(merged['optimal_paths'], count)
        self.assertEqual(merged['payload'], representative)
        # The same property holds for an erasure-heavy conflicting pair.
        copies = [[0, 1, 1, 0], [1, 1, 1, 0]]
        result = merge_decode(descriptor, copies)
        best, count, representative = brute_force(descriptor, copies)
        self.assertEqual((result['distance'], result['optimal_paths'],
                          result['payload']), (best, count, representative))
        self.assertGreaterEqual(result['distance'], 1)

    def test_three_copies_exact_distance(self):
        descriptor = {'constraint': 2, 'generators': [3, 1],
                      'length': 1, 'terminated': False}
        copies = [[0, 1], [None, 1], [None, 1]]
        result = merge_decode(descriptor, copies)
        best, count, representative = brute_force(descriptor, copies)
        self.assertEqual((result['distance'], result['optimal_paths'],
                          result['payload']), (best, count, representative))
        self.assertEqual(result['distance'], 1)
        self.assertEqual(result['payload'], [1])

    def test_terminated_exhaustive_small(self):
        descriptor = {'constraint': 3, 'generators': [7, 5],
                      'length': 2, 'terminated': True}
        length = (2 + 2) * 2
        for received in itertools.product((0, 1, None), repeat=length):
            copies = [list(received)]
            result = merge_decode(descriptor, copies)
            best, count, representative = brute_force(descriptor, copies)
            self.assertEqual(result['distance'], best)
            self.assertEqual(result['optimal_paths'], count)
            self.assertEqual(result['payload'], representative)
            self.assertEqual(result['state_path'][-1], 0)

    def test_no_valid_copy_is_fully_erased(self):
        descriptor = {'constraint': 2, 'generators': [3, 1],
                      'length': 2, 'terminated': False}
        result = merge_decode(descriptor, [])
        self.assertEqual(result['distance'], 0)
        self.assertEqual(result['optimal_paths'], 4)
        self.assertEqual(result['payload'], [0, 0])
        self.assertEqual(result['reencoded'], encode([0, 0], 2, [3, 1],
                                                     False)['symbols'])

    def test_copy_discrepancy_indices(self):
        mismatches, erasures = copy_discrepancies(
            [0, None, 1, 0], [1, 1, 1, 0])
        self.assertEqual(mismatches, [0])
        self.assertEqual(erasures, [1])

    def test_large_frame_does_not_enumerate_payloads(self):
        descriptor = {'constraint': 3, 'generators': [7, 5],
                      'length': 500, 'terminated': False}
        received = [None] * (500 * 2)
        result = merge_decode(descriptor, [received])
        self.assertEqual(result['optimal_paths'], 2 ** 500)
        self.assertEqual(len(result['payload']), 500)
        self.assertEqual(len(result['state_path']), 501)


if __name__ == '__main__':
    unittest.main()
