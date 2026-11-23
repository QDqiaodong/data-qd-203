import json
import shutil
import tempfile
import unittest
from pathlib import Path

from telemetry_trellis import Archive, ArchiveError, CrashSimulated, process
from telemetry_trellis.code import encode, transition

DESC = {'constraint': 2, 'generators': [3, 1],
        'payload_length': 1, 'terminated': False}


def legal_path_count(constraint, generators, length, terminated):
    """Independent state-transition DP oracle for all-erased path counts."""
    states = {0: 1}
    steps = length + (constraint - 1 if terminated else 0)
    for _ in range(steps):
        nxt = {}
        for state, count in states.items():
            for bit in (0, 1):
                target, _ = transition(state, bit, constraint, generators)
                nxt[target] = nxt.get(target, 0) + count
        states = nxt
    return states[0] if terminated else sum(states.values())


def update(capture, revision, action, received=None, frame='f'):
    item = {'frame_id': frame, 'capture_id': capture, 'revision': revision,
            'action': action, **DESC}
    if received is not None:
        item['received'] = received
    return item


class ArchiveAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='trellis-archive-'))
        self.path = self.root / 'archive'

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------ acceptance 1
    def test_scenario_one_merge_events_and_reopen(self):
        with Archive(self.path) as archive:
            first = {
                'batch_id': 'b1',
                'updates': [
                    update('A', 1, 'write', [0, 1]),
                    update('B', 1, 'write', [None, 1]),
                    update('C', 1, 'write', [None, 1]),
                ],
            }
            receipt = archive.submit(first)
            self.assertEqual([e['type'] for e in receipt['changes']], ['write'])
            write_event = receipt['changes'][0]
            self.assertEqual(write_event['payload'], [1])
            self.assertIsNone(write_event['previous_seq'])
            self.assertEqual(write_event['seq'], 1)

            evidence = archive.evidence('f')
            self.assertEqual(evidence['distance'], 1)
            self.assertEqual(evidence['optimal_paths'], 1)
            self.assertFalse(evidence['ambiguous'])
            self.assertEqual(evidence['payload'], [1])
            self.assertEqual(evidence['effective_payload'], [1])
            self.assertEqual(evidence['published_seq'], 1)
            by_id = {c['capture_id']: c for c in evidence['valid_copies']}
            self.assertEqual(by_id['A']['mismatch_indices'], [0])
            self.assertEqual(by_id['A']['erasure_indices'], [])
            self.assertEqual(by_id['B']['mismatch_indices'], [])
            self.assertEqual(by_id['B']['erasure_indices'], [0])
            self.assertEqual(by_id['C']['erasure_indices'], [0])

            second = {
                'batch_id': 'b2',
                'updates': [update('C', 3, 'write', [0, None])],
            }
            receipt2 = archive.submit(second)
            self.assertEqual(len(receipt2['changes']), 1)
            revoke = receipt2['changes'][0]
            self.assertEqual(revoke['type'], 'revoke')
            self.assertEqual(revoke['previous_seq'], 1)
            self.assertNotIn('payload', revoke)

            evidence = archive.evidence('f')
            self.assertEqual(evidence['distance'], 2)
            self.assertEqual(evidence['optimal_paths'], 2)
            self.assertTrue(evidence['ambiguous'])
            self.assertIsNone(evidence['effective_payload'])
            self.assertIsNone(evidence['published_seq'])

        # Reopening preserves evidence and the historical change log.
        with Archive(self.path) as archive:
            self.assertIsNone(archive.evidence('f')['published_seq'])
            page = archive.read_changes(0, 10)['changes']
            self.assertEqual([e['seq'] for e in page], [1, 2])
            self.assertEqual([e['type'] for e in page], ['write', 'revoke'])

            # Replaying batch 1 returns the original receipt without judgement.
            replay = archive.submit(first)
            self.assertEqual(replay['changes'][0]['seq'], 1)
            self.assertEqual(replay['changes'][0]['payload'], [1])
            self.assertIsNone(archive.evidence('f')['published_seq'])

            # A stale write for C cannot resurrect a revoked publication.
            stale = archive.submit({
                'batch_id': 'b3',
                'updates': [update('C', 2, 'write', [None, 1])],
            })
            self.assertEqual(stale['changes'], [])
            self.assertIsNone(archive.evidence('f')['effective_payload'])
            self.assertIsNone(archive.evidence('f')['published_seq'])

            # Same batch id with different content is rejected.
            tampered = {'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1])]}
            with self.assertRaises(ArchiveError):
                archive.submit(tampered)
            # Rejected retry leaves the original receipt intact.
            self.assertEqual(archive.receipt('b1')['changes'][0]['seq'], 1)

    # ------------------------------------------------------ acceptance 2
    def test_scenario_two_batch_net_result_and_cursor_stability(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [0, 1]),
                update('B', 1, 'write', [None, 1]),
                update('C', 1, 'write', [None, 1])]})
            archive.submit({'batch_id': 'b2', 'updates': [
                update('C', 3, 'write', [0, None])]})

            receipt = archive.submit({'batch_id': 'b3', 'updates': [
                update('B', 2, 'write', [0, None])]})
            self.assertEqual(len(receipt['changes']), 1)
            event = receipt['changes'][0]
            self.assertEqual(event['type'], 'write')
            self.assertEqual(event['payload'], [0])
            # The previous publication was revoked; no stale seq is pointed at.
            self.assertIsNone(event['previous_seq'])
            self.assertEqual(event['seq'], 3)
            self.assertEqual(archive.evidence('f')['published_seq'], 3)

            receipt = archive.submit({'batch_id': 'b4', 'updates': [
                update('B', 3, 'revoke'),
                update('C', 4, 'revoke'),
                update('D', 1, 'write', [0, 0])]})
            # No event: only A and D remain and payload [0] stays effective.
            self.assertEqual(receipt['changes'], [])
            evidence = archive.evidence('f')
            self.assertEqual({c['capture_id'] for c in evidence['valid_copies']},
                             {'A', 'D'})
            self.assertEqual(evidence['effective_payload'], [0])
            self.assertEqual(evidence['published_seq'], 3)

            # Late stale C write cannot come back.
            archive.submit({'batch_id': 'b5', 'updates': [
                update('C', 2, 'write', [None, 1])]})
            evidence = archive.evidence('f')
            self.assertEqual({c['capture_id'] for c in evidence['valid_copies']},
                             {'A', 'D'})

            # Paging history is unaffected by current evidence.
            first = archive.read_changes(0, 1)
            self.assertEqual(first['cursor'], 1)
            self.assertEqual(first['changes'][0]['payload'], [1])
            second = archive.read_changes(first['cursor'], 1)
            self.assertEqual(second['changes'][0]['type'], 'revoke')
            third = archive.read_changes(second['cursor'], 1)
            self.assertEqual(third['changes'][0]['payload'], [0])
            empty = archive.read_changes(third['cursor'], 1)
            self.assertEqual(empty['changes'], [])
            self.assertEqual(empty['cursor'], third['cursor'])

    # ------------------------------------------------------ acceptance 3
    def test_conflicting_batch_rolls_back_everything(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1], frame='f1'),
                update('B', 1, 'write', [1, 1], frame='f2')]})
            # Same-revision conflict arrives after legal updates in one batch.
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'b2', 'updates': [
                    update('C', 1, 'write', [1, 1], frame='f1'),
                    update('A', 1, 'write', [0, 1], frame='f1')]})
            evidence = archive.evidence('f1')
            self.assertEqual({c['capture_id'] for c in evidence['valid_copies']},
                             {'A'})
            with self.assertRaises(KeyError):
                archive.evidence('unknown-frame')
            self.assertEqual(
                [e['seq'] for e in archive.read_changes(0, 100)['changes']],
                [1, 2])
            with self.assertRaises(KeyError):
                archive.receipt('b2')
            self.assertEqual(archive.read_changes(2, 10)['cursor'], 2)

    def test_crash_before_commit_leaves_no_trace(self):
        request = {'batch_id': 'b1', 'updates': [
            update('A', 1, 'write', [1, 1])]}
        with Archive(self.path) as archive:
            with self.assertRaises(CrashSimulated):
                archive.submit(request, crash='staged')
            self.assertIsNone(archive._state['receipts'].get('b1'))
        # Orphan temp file cleaned on reopen; batch entirely invisible.
        with Archive(self.path) as archive:
            with self.assertRaises(KeyError):
                archive.receipt('b1')
            with self.assertRaises(KeyError):
                archive.evidence('f')
            self.assertEqual(archive.read_changes(0, 10)['changes'], [])
            # Safe retry of the identical request.
            receipt = archive.submit(request)
            self.assertEqual(receipt['changes'][0]['type'], 'write')

    def test_crash_after_commit_safe_retry(self):
        request = {'batch_id': 'b1', 'updates': [
            update('A', 1, 'write', [0, 1]),
            update('B', 1, 'write', [None, 1]),
            update('C', 1, 'write', [None, 1])]}
        with Archive(self.path) as archive:
            with self.assertRaises(CrashSimulated):
                archive.submit(request, crash='synced')
        with Archive(self.path) as archive:
            evidence = archive.evidence('f')
            self.assertEqual(evidence['effective_payload'], [1])
            receipt = archive.submit(request)
            self.assertEqual(len(receipt['changes']), 1)
            self.assertEqual(archive.read_changes(0, 10)['changes'][-1]['seq'], 1)

    def test_crash_after_replace_is_committed(self):
        request = {'batch_id': 'b1', 'updates': [
            update('A', 1, 'write', [1, 1])]}
        with Archive(self.path) as archive:
            with self.assertRaises(CrashSimulated):
                archive.submit(request, crash='replaced')
        # The process restarts; the replaced state file already holds the batch.
        with Archive(self.path) as archive:
            self.assertEqual(
                archive.evidence('f')['effective_payload'], [1])
            # Idempotent retry returns the original receipt, no new event or seq.
            retried = archive.submit(request)
            self.assertEqual(retried['batch_id'], 'b1')
            self.assertEqual(len(retried['changes']), 1)
            self.assertEqual(retried['changes'][0]['seq'], 1)
            self.assertEqual(
                len(archive.read_changes(0, 10)['changes']), 1)

    def test_terminated_max_payload_all_erased_path_count(self):
        constraint, generators = 3, [7, 5]
        length = 500
        desc = {'constraint': constraint, 'generators': generators,
                'payload_length': length, 'terminated': True}
        steps = length + constraint - 1
        erased = [None] * (steps * len(generators))
        with Archive(self.path) as archive:
            receipt = archive.submit({'batch_id': 'big', 'updates': [
                {'frame_id': 'big', 'capture_id': 'A', 'revision': 1,
                 'action': 'write', 'received': erased, **desc},
                {'frame_id': 'big', 'capture_id': 'B', 'revision': 1,
                 'action': 'write', 'received': list(erased), **desc}]})
            # Ambiguous: several payloads can reach state 0 during tail
            # flushing, so no downstream payload is published despite cost 0.
            self.assertEqual(receipt['changes'], [])
            evidence = archive.evidence('big')
            self.assertEqual(evidence['distance'], 0)
            self.assertTrue(evidence['ambiguous'])
            # Exact path count without enumerating 2**500 payloads, checked
            # against an independent state-transition DP.
            self.assertEqual(
                evidence['optimal_paths'],
                legal_path_count(constraint, generators, length, True))
            self.assertEqual(evidence['payload'], [0] * length)
            self.assertEqual(len(evidence['reencoded']), steps * len(generators))
            self.assertEqual(evidence['state_path'][-1], 0)
            self.assertEqual(
                evidence['valid_copies'][0]['erasure_indices'],
                list(range(steps * len(generators))))
            self.assertIsNone(evidence['published_seq'])

    def test_unterminated_all_erased_exact_path_count(self):
        constraint, generators = 2, [3, 1]
        desc = {'constraint': constraint, 'generators': generators,
                'payload_length': 3, 'terminated': False}
        erased = [None] * (3 * 2)
        with Archive(self.path) as archive:
            receipt = archive.submit({'batch_id': 'u', 'updates': [
                {'frame_id': 'u', 'capture_id': 'A', 'revision': 1,
                 'action': 'write', 'received': erased, **desc},
                {'frame_id': 'u', 'capture_id': 'B', 'revision': 1,
                 'action': 'write', 'received': list(erased), **desc},
                {'frame_id': 'u', 'capture_id': 'C', 'revision': 1,
                 'action': 'write', 'received': list(erased), **desc}]})
            self.assertEqual(receipt['changes'], [])
            evidence = archive.evidence('u')
            self.assertEqual(
                evidence['optimal_paths'],
                legal_path_count(constraint, generators, 3, False))
            self.assertEqual(evidence['optimal_paths'], 8)
            self.assertTrue(evidence['ambiguous'])
            self.assertEqual(evidence['distance'], 0)
            self.assertEqual(evidence['payload'], [0, 0, 0])
            self.assertEqual(len(evidence['valid_copies']), 3)
            for copy in evidence['valid_copies']:
                self.assertEqual(copy['mismatch_indices'], [])
                self.assertEqual(copy['erasure_indices'], [0, 1, 2, 3, 4, 5])

    def test_revoked_only_frame_is_known_but_invalid(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 2, 'revoke')]})
            evidence = archive.evidence('f')
            self.assertTrue(evidence['has_valid_copy'] is False)
            self.assertEqual(evidence['valid_copies'], [])
            self.assertEqual(evidence['distance'], 0)
            self.assertIsNone(evidence['effective_payload'])
            self.assertEqual(evidence['optimal_paths'], 2)
            self.assertIsNone(evidence['published_seq'])
        with Archive(self.path) as reopened:
            with self.assertRaises(KeyError):
                reopened.evidence('never-seen')
            self.assertIsNone(reopened.evidence('f')['effective_payload'])

    # ------------------------------------------------------ rules
    def test_request_validation_and_limits(self):
        with Archive(self.path) as archive:
            good = update('A', 1, 'write', [0, 1])
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'bad', 'updates': [
                    {**good, 'received': [0, 1, None]}]})
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'bad', 'updates': [
                    {**good, 'received': [True, 1]}]})
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'bad', 'updates': [
                    {**good, 'action': 'revoke', 'received': [0, 1]}]})
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'bad', 'updates': [
                    {**good, 'revision': 0}]})
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'bad', 'updates': [
                    {**good, 'frame_id': 'bad id!'}]})
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'bad',
                                'updates': [good, update('A', 2, 'revoke')]})
            # Nothing was persisted by any rejected batch.
            self.assertEqual(archive.read_changes(0, 10)['changes'], [])
            with self.assertRaises(KeyError):
                archive.receipt('bad')

            too_many = {'batch_id': 'many', 'updates': [
                update(f'C{i:02d}', 1, 'write', [0, 1]) for i in range(9)]}
            with self.assertRaises(ArchiveError):
                archive.submit(too_many)

            # Same revision, identical content is a no-op, still receipted.
            archive.submit({'batch_id': 'ok', 'updates': [good]})
            noop = archive.submit({'batch_id': 'ok2', 'updates': [good]})
            self.assertEqual(noop['changes'], [])

            # Immutable frame descriptor.
            changed = {**DESC, 'payload_length': 2}
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'b3', 'updates': [
                    {'frame_id': 'f', 'capture_id': 'B', 'revision': 1,
                     'action': 'write', 'received': [0, 1, 0, 1], **changed}]})

    def test_key_order_independent_array_order_significant(self):
        request = {'updates': [update('A', 1, 'write', [0, 1])],
                   'batch_id': 'order'}
        reordered = {'batch_id': 'order',
                     'updates': [dict(reversed(list(request['updates'][0].items())))]}
        with Archive(self.path) as archive:
            archive.submit(request)
            self.assertEqual(
                archive.submit(reordered)['changes'], [])

        # Received-array order is content: a reversed array is a different
        # request even though the multiset of symbols may look similar.
        with Archive(self.path / 'second') as archive:
            archive.submit({'batch_id': 'r1', 'updates': [
                update('A', 1, 'write', [0, 1])]})
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'r1', 'updates': [
                    update('A', 1, 'write', [1, 0])]})

    def test_corrupt_state_file_rejected_not_silently_reset(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1])]})
        (self.path / 'state.json').write_text('{not json', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            Archive(self.path)

    def test_multi_frame_events_sorted_by_frame_id(self):
        with Archive(self.path) as archive:
            receipt = archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1], frame='zeta'),
                update('A', 1, 'write', [1, 1], frame='alpha')]})
            self.assertEqual(
                [e['frame_id'] for e in receipt['changes']],
                ['alpha', 'zeta'])
            self.assertEqual([e['seq'] for e in receipt['changes']], [1, 2])

    def test_evidence_change_without_payload_change_emits_nothing(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1])]})
            # B erases symbol 0: distance/evidence moves, payload stays [1].
            receipt = archive.submit({'batch_id': 'b2', 'updates': [
                update('B', 1, 'write', [None, 1])]})
            self.assertEqual(receipt['changes'], [])
            evidence = archive.evidence('f')
            self.assertEqual(evidence['effective_payload'], [1])
            self.assertEqual(evidence['published_seq'], 1)

    def test_replace_event_carries_new_payload_and_previous_seq(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1])]})
            receipt = archive.submit({'batch_id': 'b2', 'updates': [
                update('A', 2, 'write', [0, 0])]})
            event = receipt['changes'][0]
            self.assertEqual(event['type'], 'replace')
            self.assertEqual(event['payload'], [0])
            self.assertEqual(event['previous_seq'], 1)
            self.assertEqual(event['seq'], 2)
            self.assertEqual(archive.evidence('f')['published_seq'], 2)
            raw = archive.read_changes(0, 10)['changes']
            self.assertEqual([e['type'] for e in raw], ['write', 'replace'])
            # Revoke events never carry a payload.
            receipt = archive.submit({'batch_id': 'b3', 'updates': [
                update('A', 3, 'revoke')]})
            self.assertEqual(set(receipt['changes'][0].keys()),
                             {'seq', 'frame_id', 'type', 'previous_seq'})

    def test_batch_size_limits_accepted_and_rejected(self):
        with Archive(self.path) as archive:
            # Exactly eight receiving identities on one frame is the limit.
            eight = {'batch_id': 'eight', 'updates': [
                update(f'C{i}', 1, 'write', [1, 1]) for i in range(8)]}
            self.assertEqual(len(archive.submit(eight)['changes']), 1)
            ninth = {'batch_id': 'ninth', 'updates': [
                update('C8', 1, 'write', [1, 1])]}
            with self.assertRaises(ArchiveError):
                archive.submit(ninth)

            twenty_frames = {'batch_id': 'frames', 'updates': [
                update('C', 1, 'write', [1, 1], frame=f'fr{i:02d}')
                for i in range(20)]}
            receipt = archive.submit(twenty_frames)
            self.assertEqual(len(receipt['changes']), 20)
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'over', 'updates': [
                    update('C', 1, 'write', [1, 1], frame=f'gx{i:02d}')
                    for i in range(21)]})
            with self.assertRaises(ArchiveError):
                archive.submit({'batch_id': 'empty', 'updates': []})
            with self.assertRaises(ArchiveError):
                archive.submit({'updates': [update('C', 1, 'write', [1, 1])]})

    def test_max_160_updates_spread_over_20_frames(self):
        with Archive(self.path) as archive:
            updates = []
            for fi in range(20):
                for ci in range(8):
                    updates.append(update(
                        f's{ci}', 1, 'write', [1, 1], frame=f'fr{fi:02d}'))
            self.assertEqual(len(updates), 160)
            receipt = archive.submit({'batch_id': 'full', 'updates': updates})
            self.assertEqual(len(receipt['changes']), 20)
            # Sequence numbers stay contiguous across the one batch.
            seqs = [event['seq'] for event in receipt['changes']]
            self.assertEqual(seqs, list(range(1, 21)))
            self.assertEqual(archive.read_changes(0, 1000)['cursor'], 20)

    def test_stale_revoke_remembered_across_reopen(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1]),
                update('B', 1, 'write', [1, 1])]})
            archive.submit({'batch_id': 'b2', 'updates': [
                update('A', 5, 'revoke')]})
        with Archive(self.path) as reopened:
            receipt = reopened.submit({'batch_id': 'b3', 'updates': [
                update('A', 4, 'write', [0, 0])]})
            self.assertEqual(receipt['changes'], [])
            self.assertEqual(
                [c['capture_id'] for c in
                 reopened.evidence('f')['valid_copies']], ['B'])

    def test_history_never_rebuilt_from_current_evidence(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1]),
                update('B', 1, 'write', [None, 1]),
                update('C', 1, 'write', [None, 1])]})
            archive.submit({'batch_id': 'b2', 'updates': [
                update('C', 3, 'write', [0, None])]})
        # Wipe every current receiver through new revisions; the stored log
        # entries still expose the old payloads verbatim.
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b3', 'updates': [
                update('A', 9, 'revoke'),
                update('B', 9, 'revoke'),
                update('C', 9, 'revoke')]})
            log = archive.read_changes(0, 100)['changes']
            self.assertEqual(log[0]['payload'], [1])
            self.assertEqual(log[1]['type'], 'revoke')
            self.assertNotIn('payload', log[1])
            self.assertIsNone(archive.evidence('f')['published_seq'])

    def test_json_safe_evidence_and_receipt(self):
        import json as json_module
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [1, 1])]})
            json_module.dumps(archive.evidence('f'), allow_nan=False)
            json_module.dumps(archive.receipt('b1'), allow_nan=False)
            json_module.dumps(archive.read_changes(0, 50), allow_nan=False)

    def test_revoke_then_newer_write_revives(self):
        with Archive(self.path) as archive:
            archive.submit({'batch_id': 'b1', 'updates': [
                update('A', 1, 'write', [0, 1]),
                update('B', 1, 'write', [None, 1]),
                update('C', 1, 'write', [None, 1])]})
            archive.submit({'batch_id': 'b2', 'updates': [
                update('A', 2, 'revoke'),
                update('B', 2, 'revoke'),
                update('C', 2, 'revoke')]})
            self.assertIsNone(archive.evidence('f')['effective_payload'])
            receipt = archive.submit({'batch_id': 'b3', 'updates': [
                update('A', 3, 'write', [1, 1])]})
            self.assertEqual(receipt['changes'][0]['type'], 'write')
            # Only the revoked seq 3 publication was ever effective for f;
            # seq 2 already revoked it, so it must not be referenced.
            self.assertIsNone(receipt['changes'][0]['previous_seq'])
            self.assertEqual(archive.evidence('f')['published_seq'], 3)

    def test_baseline_process_still_works(self):
        # Compatibility guard: the original single-frame API is untouched.
        result = process({
            'constraint': 3, 'generators': [7, 5],
            'frames': [{
                'id': 'block', 'payload_length': 3, 'terminated': True,
                'received': [0, 1, 1, 0, 0, 0, 1, 0, 1, 1]}]})
        frame = result['frames'][0]
        self.assertEqual(frame['payload'], [1, 0, 1])
        self.assertEqual(frame['corrected_indices'], [0])

    def test_encode_contract_compatibility(self):
        self.assertEqual(encode([1, 0, 1], 3, [7, 5])['symbols'],
                         [1, 1, 1, 0, 0, 0, 1, 0, 1, 1])


if __name__ == '__main__':
    unittest.main()
