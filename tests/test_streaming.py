"""Acceptance and oracle tests for incremental telemetry delivery.

Every scenario is synthetic; no receiving equipment is involved.  The four
acceptance stories from the data contract are reproduced directly, plus
randomized oracles that compare early delivery and final evidence against brute
force / the existing one-shot ``process`` decoder.  Handoff tests run in a fresh
``python -I`` subprocess that only reads the saved JSON state.
"""
import itertools
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from telemetry_trellis import (
    StreamingError,
    append,
    confirm,
    finish,
    load_state,
    new_state,
    progress,
    process,
    replace,
    save_state,
)
from telemetry_trellis.code import transition

SRC_PATH = str(Path(__file__).resolve().parents[1] / 'src')


def start(frame_id='rec', constraint=2, generators=(1, 2, 3),
          payload_length=4, terminated=False):
    return new_state(frame_id, constraint, list(generators), payload_length,
                     terminated)


def feed(state, fragments):
    for fragment in fragments:
        state = append(state, fragment)['state']
    return state


def confirmed_state(state, position):
    return confirm(state, position)['state']


# ----------------------------------------------------------------- case 1
class CaseOneTests(unittest.TestCase):
    def build(self):
        state = start()
        # [0,null,null | 0,0,0 | 0,0,0], split inside the first group.
        return feed(state, [[0, None], [None, 0, 0], [0, 0], [0, 0]])

    def test_confirm_four_delivers_nothing_and_rejects_old_replace(self):
        state = self.build()
        result = confirm(state, 4)
        self.assertIsNone(result['delivery'])
        self.assertEqual(result['delivered'], 0)
        self.assertEqual(result['confirmed'], 4)
        # The first group alone is globally cheapest at bit 0, but a single
        # fully confirmed step is not enough to be certain downstream.
        with self.assertRaises(ValueError):
            replace(result['state'], 3, [1, 1])
        # Replacing inside the unconfirmed part is legal and still delivers
        # nothing.
        moved = replace(result['state'], 4, [1, 1])
        self.assertIsNone(moved['delivery'])
        self.assertEqual(moved['delivered'], 0)

    def test_confirm_nine_delivers_one_zero_from_zero(self):
        state = self.build()
        state = confirmed_state(state, 4)
        state = replace(state, 4, [1, 1])['state']
        result = confirm(state, 9)
        self.assertEqual(result['delivery'],
                         {'start': 0, 'bits': [1, 0], 'end': 2})
        self.assertEqual(result['delivered'], 2)
        # Delivered bits and confirmation are irrevocable: the retired blocks
        # covering expanded coordinates [0, 6) are gone from the input.
        self.assertEqual(result['state']['retired'][0]['step'], 0)
        self.assertEqual(list(result['state']['pending']), ['2'])
        with self.assertRaises(ValueError):
            replace(result['state'], 0, [1, 1])
        with self.assertRaises(ValueError):
            confirm(result['state'], 8)

    def test_retired_state_carries_no_raw_block(self):
        state = self.build()
        state = confirmed_state(state, 4)
        state = replace(state, 4, [1, 1])['state']
        state = confirmed_state(state, 9)
        for block in state['retired']:
            self.assertEqual(set(block), {'step', 'digest', 'status', 'cost'})
            self.assertNotIn('symbols', block)

    def test_three_unconfirmed_steps_would_wrongly_deliver_zero_zero(self):
        # With the same three groups but without the confirmation at 4, a naive
        # rule that used every received step would deliver 00 too early.
        state = feed(start(), [[0, None, None], [0, 0, 0], [0, 0, 0]])
        premature = confirm(state, 9)
        self.assertEqual(premature['delivery']['bits'], [0, 0])
        # Only the first group is confirmed here, so the honest session holds
        # back: confirming to 9 but with the corrected second group yields 10.
        honest = feed(start(), [[0, None], [None, 0, 0], [0, 0], [0, 0]])
        honest = confirmed_state(honest, 4)
        honest = replace(honest, 4, [1, 1])['state']
        self.assertEqual(confirm(honest, 9)['delivery']['bits'], [1, 0])


# ----------------------------------------------------------------- case 2
class CaseTwoTests(unittest.TestCase):
    def build(self):
        state = start()
        state = feed(state, [[0, None], [None, 0, 0], [0, 0], [0, 0]])
        state = confirmed_state(state, 4)
        state = replace(state, 4, [1, 1])['state']
        return confirmed_state(state, 9)

    def test_append_confirm_and_finish_after_delivery(self):
        state = self.build()
        self.assertEqual(state['delivered'], 2)
        state = append(state, [1, 0, 1])['state']
        result = confirm(state, 12)
        self.assertEqual(result['delivery'],
                         {'start': 2, 'bits': [0], 'end': 3})
        state = result['state']
        final = finish(state)
        self.assertEqual(final['delivery'],
                         {'start': 3, 'bits': [1], 'end': 4})
        evidence = final['evidence']
        self.assertEqual(evidence['payload'], [1, 0, 0, 1])
        self.assertEqual(evidence['distance'], 1)
        self.assertEqual(evidence['optimal_paths'], 1)
        self.assertEqual(evidence['corrected_indices'], [0])
        self.assertEqual(evidence['erasure_indices'], [1, 2])
        # Finish evidence equals one-shot process on the final array.
        one_shot = process({
            'constraint': 2, 'generators': [1, 2, 3],
            'frames': [{'id': 'rec', 'payload_length': 4,
                        'terminated': False,
                        'received': [0, None, None, 0, 1, 1,
                                     0, 0, 0, 1, 0, 1]}]})['frames'][0]
        self.assertEqual(evidence, one_shot)

    def test_last_group_all_null_keeps_tie(self):
        state = self.build()
        state = append(state, [None, None, None])['state']
        result = confirm(state, 12)
        self.assertEqual(result['delivery'],
                         {'start': 2, 'bits': [0], 'end': 3})
        final = finish(result['state'])
        # The delivered prefix is not retracted; the final optimum is a tie.
        self.assertIsNone(final['delivery'])
        evidence = final['evidence']
        self.assertEqual(evidence['payload'], [1, 0, 0, 0])
        self.assertEqual(evidence['optimal_paths'], 2)
        self.assertTrue(evidence['ambiguous'])
        # Diagnostics complete old and new erasure coordinates.
        self.assertEqual(evidence['erasure_indices'], [1, 2, 9, 10, 11])
        self.assertEqual(evidence['corrected_indices'], [0])

    def test_handoff_at_confirm_four_freezes_only_confirmed_prefix(self):
        state = feed(start(), [[0, None], [None, 0, 0], [0, 0], [0, 0]])
        state = confirmed_state(state, 4)
        directory = Path(tempfile.mkdtemp(prefix='trellis-stream-'))
        path = directory / 'state.json'
        save_state(path, state)
        resumed = load_state(path)
        with self.assertRaises(ValueError):
            replace(resumed, 3, [1, 1])
        self.assertEqual(replace(resumed, 4, [1, 1])['state']['pending']['1'],
                         [0, 1, 1])


class HandoffSubprocessTests(unittest.TestCase):
    """A fresh interpreter resumes from the saved JSON only."""

    def run_child(self, path, mode):
        runner = Path(tempfile.mkdtemp(prefix='trellis-runner-')) / 'run.py'
        runner.write_text(
            'import json, sys\n'
            f'sys.path.insert(0, {SRC_PATH!r})\n'
            'from telemetry_trellis import append, confirm, finish, load_state\n'
            'state = load_state(sys.argv[1])\n'
            'mode = sys.argv[2]\n'
            'state = append(state, [1, 0, 1])["state"]\n'
            'state = confirm(state, 12)["state"]\n'
            'final = finish(state)\n'
            'print(json.dumps({"delivery": final["delivery"],\n'
            '                  "evidence": final["evidence"]}))\n',
            encoding='utf-8')
        completed = subprocess.run(
            [sys.executable, '-I', str(runner), str(path), mode],
            capture_output=True, text=True, check=True)
        return json.loads(completed.stdout)

    def saved_state(self):
        state = feed(start(), [[0, None], [None, 0, 0], [0, 0], [0, 0]])
        state = confirmed_state(state, 4)
        state = replace(state, 4, [1, 1])['state']
        state = confirmed_state(state, 9)
        directory = Path(tempfile.mkdtemp(prefix='trellis-state-'))
        path = directory / 'state.json'
        save_state(path, state)
        return path

    def test_fresh_process_continues_without_replaying_input(self):
        output = self.run_child(self.saved_state(), 'finish')
        self.assertEqual(output['delivery'],
                         {'start': 3, 'bits': [1], 'end': 4})
        self.assertEqual(output['evidence']['payload'], [1, 0, 0, 1])
        self.assertEqual(output['evidence']['distance'], 1)
        self.assertEqual(output['evidence']['optimal_paths'], 1)
        self.assertEqual(output['evidence']['erasure_indices'], [1, 2])
        self.assertEqual(output['evidence']['corrected_indices'], [0])


# ----------------------------------------------------------------- case 3
class CaseThreeTests(unittest.TestCase):
    def build(self):
        state = start(payload_length=3)
        # [0,null,null | 0,0,0 | 0,1,0]
        return feed(state, [[0, None, None], [0, 0, 0], [0, 1, 0]])

    def test_only_first_bit_certain_then_rest_at_finish(self):
        state = self.build()
        result = confirm(state, 9)
        self.assertEqual(result['delivery'],
                         {'start': 0, 'bits': [0], 'end': 1})
        state = result['state']
        final = finish(state)
        self.assertEqual(final['delivery'],
                         {'start': 1, 'bits': [0, 0], 'end': 3})
        self.assertEqual(final['evidence']['payload'], [0, 0, 0])
        self.assertEqual(final['evidence']['optimal_paths'], 1)

    def test_state_zero_and_one_reach_different_common_prefixes(self):
        # Brute check of the documented reasoning: state 0 optimal prefix 000
        # and state 1 optimal prefixes 001 and 011 share only the first bit.
        # For constraint 2 reachable states at the two-group boundary: state 0
        # costs 0 and state 1 costs 2 for the documented observations.
        observed = [0, None, None, 0, 0, 0]
        forward = {0: {0: 0}}
        for step in range(2):
            observation = observed[step * 3:(step + 1) * 3]
            following = {}
            for state, cost in forward[step].items():
                for bit in (0, 1):
                    target, word = transition(state, bit, 2, [1, 2, 3])
                    metric = cost + sum(
                        seen is not None and seen != expected
                        for seen, expected in zip(observation, word))
                    if target not in following or metric < following[target]:
                        following[target] = metric
            forward[step + 1] = following
        self.assertEqual(sorted(forward[2]), [0, 1])
        self.assertEqual(forward[2], {0: 0, 1: 2})

    def test_illegal_offsets_and_recovery(self):
        state = self.build()
        with self.assertRaises(ValueError):
            confirm(state, 10)
        with self.assertRaises(ValueError):
            confirm(state, -1)
        with self.assertRaises(ValueError):
            append(state, [0])
        with self.assertRaises(ValueError):
            replace(state, 9, [0])
        with self.assertRaises(ValueError):
            replace(state, 2, [0, 0, 0, 0, 0, 0, 0, 0])
        with self.assertRaises(ValueError):
            replace(confirm(state, 9)['state'], 0, [0])
        # A failed operation leaves the original state usable.
        self.assertEqual(progress(state)['received_count'], 9)
        result = confirm(state, 9)
        self.assertEqual(result['delivery']['bits'], [0])

    def test_early_finish_is_rejected_without_change(self):
        state = self.build()
        with self.assertRaises(ValueError):
            finish(state)
        self.assertFalse(progress(state)['finished'])
        self.assertEqual(state['delivered'], 0)


# ----------------------------------------------------------------- case 4
class TerminatedTests(unittest.TestCase):
    def test_terminated_tail_confirm_cut_and_process_parity(self):
        # constraint 2, generators [1,2,3], payload 3, terminated: 4 steps.
        received = [0, 0, 1, 0, 1, 1, None, 0, 1, 0, 1, 1]
        state = start(frame_id='term', payload_length=3, terminated=True)
        state = append(state, received[:7])['state']  # cut inside tail step
        result = confirm(state, 7)
        self.assertIsNone(result['delivery'])
        state = append(result['state'], received[7:])['state']
        result = confirm(state, 12)
        self.assertEqual(result['delivery'],
                         {'start': 0, 'bits': [1, 0, 1], 'end': 3})
        evidence = finish(result['state'])['evidence']
        one_shot = process({
            'constraint': 2, 'generators': [1, 2, 3],
            'frames': [{'id': 'term', 'payload_length': 3,
                        'terminated': True, 'received': received}]})['frames'][0]
        self.assertEqual(evidence, one_shot)

    def test_tail_step_never_delivers_payload(self):
        from telemetry_trellis.code import encode
        payload = [1, 0, 1]
        received = encode(payload, 2, [1, 2, 3], True)['symbols']
        state = start(frame_id='term', payload_length=3, terminated=True)
        state = append(state, received)['state']
        result = confirm(state, len(received))
        self.assertLessEqual(result['delivered'], 3)
        final = finish(result['state'])
        self.assertEqual(final['delivered'], 3)
        self.assertEqual(final['evidence']['payload'], payload)

    def test_full_payload_early_keeps_tail_pending_and_handoff(self):
        from telemetry_trellis.code import encode
        payload = [1, 0, 1]
        received = encode(payload, 2, [1, 2, 3], True)['symbols']
        state = start(frame_id='term', payload_length=3, terminated=True)
        state = append(state, received)['state']
        state = confirm(state, len(received))['state']
        # All payload bits delivered; the zero tail's raw block is still kept
        # because no payload bit depends on it.
        self.assertEqual(state['delivered'], 3)
        self.assertEqual(list(state['pending']), ['3'])
        directory = Path(tempfile.mkdtemp(prefix='trellis-tail-'))
        path = directory / 's.json'
        save_state(path, state)
        final = finish(load_state(path))
        self.assertIsNone(final['delivery'])
        self.assertEqual(final['evidence']['payload'], payload)
        self.assertEqual(final['evidence']['distance'], 0)

    def test_500_bit_all_erased_counts_two_to_500_without_delivery(self):
        state = start(frame_id='big', payload_length=500)
        for _ in range(6):
            state = append(state, [None] * 250)['state']
        result = confirm(state, 1500)
        self.assertIsNone(result['delivery'])
        final = finish(result['state'])
        self.assertIsNone(final['delivery'])
        self.assertEqual(final['evidence']['optimal_paths'], 2 ** 500)
        self.assertEqual(final['evidence']['distance'], 0)
        self.assertEqual(final['state']['delivered'], 0)

    def test_long_fragmented_record_delivers_incrementally(self):
        from telemetry_trellis.code import encode
        payload = [((i * 7) ^ (i >> 1)) & 1 for i in range(500)]
        received = encode(payload, 2, [1, 2, 3], False)['symbols']
        state = start(frame_id='long', payload_length=500)
        # Fragment of 3..9 symbols per append.
        index = 0
        step = 0
        piece = []
        pieces = []
        while index < len(received):
            size = 3 + (step * 5) % 7
            pieces.append(received[index:index + size])
            index += size
            step += 1
        for piece in pieces:
            state = append(state, piece)['state']
        # Confirm in halves with a mid-way handoff.
        half = len(received) // 2
        state = confirmed_state(state, half)
        self.assertGreater(state['delivered'], 0)
        directory = Path(tempfile.mkdtemp(prefix='trellis-long-'))
        path = directory / 'long.json'
        save_state(path, state)
        resumed = load_state(path)
        final_result = confirm(resumed, len(received))
        final = finish(final_result['state'])
        self.assertEqual(final['evidence']['payload'], payload)
        self.assertEqual(final['evidence']['distance'], 0)
        self.assertEqual(final['delivered'], 500)


# --------------------------------------------------------------- oracles
def brute_common_prefix(observations, width, constraint, generators,
                        steps, payload_length):
    """Longest payload prefix shared by every minimum-cost prefix."""
    forward = {0: {0: 0}}
    for step in range(steps):
        observation = observations[step * width:(step + 1) * width]
        following = {}
        for state, cost in forward[step].items():
            for bit in (0, 1):
                target, word = transition(state, bit, constraint, generators)
                metric = cost + sum(
                    observed is not None and observed != expected
                    for observed, expected in zip(observation, word))
                if target not in following or metric < following[target]:
                    following[target] = metric
        forward[step + 1] = following
    sequences = []
    for bits in itertools.product((0, 1), repeat=steps):
        state = 0
        cost = 0
        for step, bit in enumerate(bits):
            state, word = transition(state, bit, constraint, generators)
            observation = observations[step * width:(step + 1) * width]
            cost += sum(observed is not None and observed != expected
                        for observed, expected in zip(observation, word))
        if cost == forward[steps].get(state):
            sequences.append(bits[:payload_length])
    shortest = min(len(item) for item in sequences)
    for position in range(shortest):
        if len({item[position] for item in sequences}) > 1:
            return position
    return shortest


class OracleTests(unittest.TestCase):
    def test_early_delivery_matches_per_state_brute_force(self):
        rng = random.Random(2024)
        for _ in range(400):
            constraint = rng.choice([2, 3])
            width = rng.randint(2, 3)
            generators = rng.sample(range(1, 1 << constraint), width)
            steps = rng.randint(1, 5)
            observations = [rng.choice([0, 1, None])
                            for _ in range(steps * width)]
            state = start(constraint=constraint, generators=generators,
                          payload_length=steps)
            index = 0
            while index < len(observations):
                size = rng.randint(1, 3)
                state = append(state, observations[index:index + size])['state']
                index += size
            for step in range(1, steps + 1):
                result = confirm(state, min(step * width, steps * width))
                state = result['state']
                expected = brute_common_prefix(
                    observations, width, constraint, generators,
                    min(step, steps), steps)
                self.assertEqual(state['delivered'], expected)
            final = finish(state)
            one_shot = process({
                'constraint': constraint, 'generators': list(generators),
                'frames': [{'id': 'rec', 'payload_length': steps,
                            'terminated': False,
                            'received': observations}]})['frames'][0]
            self.assertEqual(final['evidence'], one_shot)

    def test_terminated_evidence_matches_process(self):
        rng = random.Random(99)
        for _ in range(400):
            constraint = rng.choice([2, 3])
            width = rng.randint(2, 3)
            generators = rng.sample(range(1, 1 << constraint), width)
            payload_length = rng.randint(1, 4)
            steps = payload_length + constraint - 1
            observations = [rng.choice([0, 1, None])
                            for _ in range(steps * width)]
            state = start(constraint=constraint, generators=generators,
                          payload_length=payload_length, terminated=True)
            index = 0
            while index < len(observations):
                size = rng.randint(1, 4)
                state = append(state, observations[index:index + size])['state']
                index += size
            position = 0
            while position < len(observations):
                position = min(len(observations),
                               position + rng.randint(1, 2 * width))
                state = confirm(state, position)['state']
            final = finish(state)
            one_shot = process({
                'constraint': constraint, 'generators': list(generators),
                'frames': [{'id': 'rec', 'payload_length': payload_length,
                            'terminated': True,
                            'received': observations}]})['frames'][0]
            self.assertEqual(final['evidence'], one_shot)


class ValidationTests(unittest.TestCase):
    def test_tampered_state_is_rejected_on_load(self):
        import copy
        state = feed(start(), [[0, None, None, 0, 0, 0, 0, 0, 0, 0, 0, 0]])
        directory = Path(tempfile.mkdtemp(prefix='trellis-bad-'))
        path = directory / 'bad.json'
        # A delivered length inconsistent with the payload cannot be loaded.
        broken = copy.deepcopy(state)
        broken['delivered'] = 1
        path.write_text(json.dumps(broken), encoding='utf-8')
        with self.assertRaises(StreamingError):
            load_state(path)
        # Out-of-range symbols and wrong layout are rejected too.
        for field, value in (('received_count', 99), ('width', 9),
                             ('confirmed', -1), ('symbol_count', 1)):
            broken = copy.deepcopy(state)
            broken[field] = value
            path.write_text(json.dumps(broken), encoding='utf-8')
            with self.assertRaises(StreamingError):
                load_state(path)


class FinalStateTests(unittest.TestCase):
    def test_finished_state_is_closed(self):
        state = feed(start(), [[0, None, None, 0, 0, 0, 0, 0, 0, 0, 0, 0]])
        result = finish(confirm(state, 12)['state'])
        done = result['state']
        with self.assertRaises(StreamingError):
            append(done, [0])
        with self.assertRaises(StreamingError):
            replace(done, 0, [1])
        with self.assertRaises(StreamingError):
            confirm(done, 2)
        with self.assertRaises(StreamingError):
            finish(done)

    def test_progress_reports_three_lengths(self):
        state = feed(start(), [[0, None, None]])
        result = confirm(state, 3)
        report = progress(result['state'])
        self.assertEqual(report['received_count'], 3)
        self.assertEqual(report['confirmed'], 3)
        self.assertEqual(report['delivered'], result['delivered'])

    def test_state_json_is_round_trippable(self):
        state = feed(start(), [[0, None, None]])
        directory = Path(tempfile.mkdtemp(prefix='trellis-json-'))
        path = directory / 's.json'
        save_state(path, state)
        self.assertEqual(load_state(path), json.loads(path.read_text()))
        with self.assertRaises(FileExistsError):
            save_state(path, state)  # refuses to overwrite


if __name__ == '__main__':
    unittest.main()
