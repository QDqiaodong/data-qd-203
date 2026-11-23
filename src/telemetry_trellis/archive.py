"""On-disk, reopenable telemetry archive.

An :class:`Archive` is opened by directory path and offers three entry points:

* :meth:`Archive.submit` — atomically commit one batch of observation updates
  together with the downstream changes it produces and a batch receipt;
* :meth:`Archive.evidence` — current decoding evidence for one frame;
* :meth:`Archive.read_changes` — ordered, cursor-paged downstream change log.

All inputs, query arguments and receipts are plain JSON data structures.  The
archive state is a single JSON document replaced atomically (temp file, fsync,
``os.replace``, directory fsync) under an exclusive ``flock``; only the Python
standard library is used.
"""
import fcntl
import json
import os
import tempfile
from pathlib import Path

from .checks import array, fields, ident, integer
from .merge import copy_discrepancies, merge_decode

STATE_VERSION = 1
MAX_UPDATES = 160
MAX_FRAMES_PER_BATCH = 20
MAX_RECEIVERS_PER_FRAME = 8
MAX_PAGE_SIZE = 1000
MAX_REVISION = 2 ** 31 - 1
STATE_FILE = 'state.json'
LOCK_FILE = 'lock'
_TMP_PREFIX = '.state-'
_WRITE = 'write'
_REVOKE = 'revoke'
_ACTIONS = (_WRITE, _REVOKE)


class ArchiveError(ValueError):
    """Raised for malformed requests and rejected batches."""


class CrashSimulated(RuntimeError):
    """Test-only fault injected at a commit durability boundary."""


def _is_strict_int(value):
    return type(value) is int


def _canonical_request(request):
    # Dict key order is irrelevant; list order (updates, received, generators)
    # is significant and therefore preserved.
    return json.dumps(request, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _new_state():
    return {
        'version': STATE_VERSION,
        'frames': {},
        'events': [],
        'next_seq': 1,
        'published': {},
        'receipts': {},
    }


class Archive:
    """Offline telemetry archive backed by one directory."""

    def __init__(self, path):
        self._dir = Path(path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock_fh = open(self._dir / LOCK_FILE, 'a+b')
        fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX)
        self._closed = False
        self._cleanup_temp_files()
        self._state_path = self._dir / STATE_FILE
        if self._state_path.exists():
            with open(self._state_path, 'rb') as handle:
                self._state = json.loads(handle.read().decode('utf-8'))
            if self._state.get('version') != STATE_VERSION:
                raise ArchiveError('unsupported archive state version')
        else:
            self._state = _new_state()

    # ------------------------------------------------------------------ open
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        if not self._closed:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            self._lock_fh.close()
            self._closed = True

    def _cleanup_temp_files(self):
        # A process interrupted after staging but before os.replace leaves
        # orphaned temp files behind; they never were the committed state.
        for entry in self._dir.glob(_TMP_PREFIX + '*.tmp'):
            try:
                entry.unlink()
            except FileNotFoundError:
                pass

    # -------------------------------------------------------------- validation
    def _validate_descriptor(self, update):
        constraint = integer(update['constraint'], 2, 7)
        generators = array(update['generators'], 2, 4)
        generators = [integer(g, 1, (1 << constraint) - 1) for g in generators]
        if len(set(generators)) != len(generators):
            raise ArchiveError('generators must be distinct')
        length = integer(update['payload_length'], 1, 500)
        if type(update['terminated']) is not bool:
            raise ArchiveError('terminated must be boolean')
        return {
            'constraint': constraint,
            'generators': generators,
            'payload_length': length,
            'terminated': update['terminated'],
        }

    @staticmethod
    def _strict_received(symbols, expected_len):
        received = array(symbols, expected_len, expected_len)
        for symbol in received:
            if symbol is not None and (
                    type(symbol) is not int or symbol not in (0, 1)):
                raise ArchiveError(
                    'received symbols must be 0, 1 or null')
        return received

    def _validate_update(self, update):
        if not isinstance(update, dict):
            raise ArchiveError('each update must be an object')
        allowed = {'frame_id', 'capture_id', 'revision', 'action',
                   'constraint', 'generators', 'payload_length',
                   'terminated', 'received'}
        if set(update.keys()) - allowed:
            raise ArchiveError('unknown update field')
        required = allowed - {'received'}
        if not required <= set(update.keys()):
            raise ArchiveError('missing update field')
        frame_id = ident(update['frame_id'])
        capture_id = ident(update['capture_id'])
        if (not _is_strict_int(update['revision'])
                or not 1 <= update['revision'] <= MAX_REVISION):
            raise ArchiveError('revision must be a positive integer')
        action = update['action']
        if action not in _ACTIONS:
            raise ArchiveError("action must be 'write' or 'revoke'")
        descriptor = self._validate_descriptor(update)
        width = len(descriptor['generators'])
        steps = descriptor['payload_length'] + (
            descriptor['constraint'] - 1 if descriptor['terminated'] else 0)
        expected_len = steps * width
        if action == _WRITE:
            if 'received' not in update:
                raise ArchiveError('write action requires received')
            received = self._strict_received(
                update['received'], expected_len)
        else:
            if 'received' in update:
                raise ArchiveError('revoke action must not carry received')
            received = None
        return {
            'frame_id': frame_id,
            'capture_id': capture_id,
            'revision': update['revision'],
            'action': action,
            'descriptor': descriptor,
            'received': received,
        }

    def _validate_request(self, request):
        try:
            if not isinstance(request, dict):
                raise ArchiveError('batch request must be an object')
            fields(request, ('batch_id', 'updates'))
            batch_id = ident(request['batch_id'])
            raw_updates = array(request['updates'], 1, MAX_UPDATES)
            updates = [self._validate_update(item) for item in raw_updates]
        except ValueError as exc:
            raise ArchiveError(str(exc)) from exc
        if len({u['frame_id'] for u in updates}) > MAX_FRAMES_PER_BATCH:
            raise ArchiveError('a batch touches at most 20 frames')
        # A frame/capture pair may appear at most once in one batch.
        pairs = [(u['frame_id'], u['capture_id']) for u in updates]
        if len(set(pairs)) != len(pairs):
            raise ArchiveError('duplicate frame/capture pair in batch')
        # Frame descriptors are immutable: they must match within the batch
        # and against any descriptor already registered for the frame.
        descriptors = {}
        for update in updates:
            frame_id = update['frame_id']
            signature = (update['descriptor']['constraint'],
                         tuple(update['descriptor']['generators']),
                         update['descriptor']['payload_length'],
                         update['descriptor']['terminated'])
            if frame_id in descriptors and descriptors[frame_id] != signature:
                raise ArchiveError('inconsistent frame descriptor in batch')
            descriptors[frame_id] = signature
            stored = self._state['frames'].get(frame_id)
            if stored is not None:
                stored_signature = (stored['constraint'],
                                    tuple(stored['generators']),
                                    stored['payload_length'],
                                    stored['terminated'])
                if stored_signature != signature:
                    raise ArchiveError('frame descriptor cannot change')
        # At most eight receiving identities per frame, revoked ones included.
        for frame_id in descriptors:
            existing = set(self._state['frames'].get(frame_id, {})
                           .get('receivers', {}))
            incoming = {u['capture_id'] for u in updates
                        if u['frame_id'] == frame_id}
            if len(existing | incoming) > MAX_RECEIVERS_PER_FRAME:
                raise ArchiveError('at most 8 receiving identities per frame')
        return batch_id, updates

    # --------------------------------------------------------------- evidence
    def _frame_desc(self, frame):
        return {
            'constraint': frame['constraint'],
            'generators': list(frame['generators']),
            'length': frame['payload_length'],
            'terminated': frame['terminated'],
        }

    def _evidence_from_frame(self, frame):
        descriptor = self._frame_desc(frame)
        valid = sorted(
            (item for item in frame['receivers'].items()
             if item[1]['action'] == _WRITE),
            key=lambda item: item[0])
        copies = [item[1]['received'] for item in valid]
        result = merge_decode(descriptor, copies)
        copy_reports = []
        for capture_id, record in valid:
            mismatches, erasures = copy_discrepancies(
                record['received'], result['reencoded'])
            copy_reports.append({
                'capture_id': capture_id,
                'revision': record['revision'],
                'mismatch_indices': mismatches,
                'erasure_indices': erasures,
            })
        return {
            'frame_id': None,  # filled in by caller
            'constraint': descriptor['constraint'],
            'generators': descriptor['generators'],
            'payload_length': descriptor['length'],
            'terminated': descriptor['terminated'],
            'distance': result['distance'],
            'optimal_paths': result['optimal_paths'],
            'ambiguous': result['ambiguous'],
            'payload': result['payload'],
            'effective_payload': None if result['ambiguous'] else result['payload'],
            'selected_final_state': result['selected_final_state'],
            'reencoded': result['reencoded'],
            'state_path': result['state_path'],
            'valid_copies': copy_reports,
            'has_valid_copy': bool(valid),
            'published_seq': None,
        }

    def evidence(self, frame_id):
        """Return current decoding evidence for one registered frame.

        Unknown frames raise ``KeyError``; a frame registered through a revoke
        only is known and scored as a fully erased observation.
        """
        self._check_open()
        ident(frame_id)
        frame = self._state['frames'].get(frame_id)
        if frame is None:
            raise KeyError(frame_id)
        report = self._evidence_from_frame(frame)
        report['frame_id'] = frame_id
        report['published_seq'] = self._state['published'].get(frame_id)
        return report

    def _effective_payload(self, frame_id, frames):
        frame = frames.get(frame_id)
        if frame is None:
            return None
        report = self._evidence_from_frame(frame)
        return report['effective_payload']

    # ------------------------------------------------------------- change log
    def read_changes(self, cursor, page_size):
        """Return at most ``page_size`` changes with seq strictly > cursor."""
        self._check_open()
        if not _is_strict_int(cursor) or cursor < 0:
            raise ArchiveError('cursor must be a non-negative integer')
        if not _is_strict_int(page_size) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ArchiveError('page_size must be an integer in 1..1000')
        events = self._state['events']
        page = [dict(event) for event in events if event['seq'] > cursor][:page_size]
        next_cursor = page[-1]['seq'] if page else cursor
        return {'cursor': next_cursor, 'changes': page}

    def receipt(self, batch_id):
        """Return the stored receipt for a committed batch (KeyError if none)."""
        self._check_open()
        ident(batch_id)
        record = self._state['receipts'].get(batch_id)
        if record is None:
            raise KeyError(batch_id)
        return json.loads(record['receipt_json'])

    # ------------------------------------------------------------------ submit
    @staticmethod
    def _request_record(batch_id, updates):
        """Normalized JSON view of a request used for retry comparison."""
        records = []
        for update in updates:
            descriptor = update['descriptor']
            record = {
                'frame_id': update['frame_id'],
                'capture_id': update['capture_id'],
                'revision': update['revision'],
                'action': update['action'],
                'constraint': descriptor['constraint'],
                'generators': list(descriptor['generators']),
                'payload_length': descriptor['payload_length'],
                'terminated': descriptor['terminated'],
            }
            if update['received'] is not None:
                record['received'] = list(update['received'])
            records.append(record)
        return {'batch_id': batch_id, 'updates': records}

    def submit(self, request, *, crash=None):
        """Commit one batch; returns the receipt.

        ``crash`` is a test-only hook: ``'staged'`` fails after the temp file
        is durable but before it replaces the state, ``'replaced'`` after the
        replace but before the directory fsync, ``'synced'`` after the fsync
        but before the caller receives its answer.
        """
        self._check_open()
        if crash is not None and crash not in ('staged', 'replaced', 'synced'):
            raise ArchiveError("crash must be 'staged', 'replaced' or 'synced'")
        # Format and descriptor validation always runs, even on retries, so a
        # malformed retry cannot be mistaken for an idempotent replay.
        batch_id, updates = self._validate_request(request)
        canonical = _canonical_request(
            self._request_record(batch_id, updates))
        # Idempotency then runs before any fresh business judgement: a retried
        # identical request returns the original receipt unchanged.
        stored_receipt = self._state['receipts'].get(batch_id)
        if stored_receipt is not None:
            if stored_receipt['canonical_request'] != canonical:
                raise ArchiveError('batch_id reused with different content')
            return json.loads(stored_receipt['receipt_json'])

        projected = json.loads(json.dumps(self._state))
        touched = []
        for update in updates:
            frame_id = update['frame_id']
            frame = projected['frames'].get(frame_id)
            if frame is None:
                descriptor = update['descriptor']
                frame = {
                    'constraint': descriptor['constraint'],
                    'generators': list(descriptor['generators']),
                    'payload_length': descriptor['payload_length'],
                    'terminated': descriptor['terminated'],
                    'receivers': {},
                }
                projected['frames'][frame_id] = frame
            receiver = frame['receivers'].get(update['capture_id'])
            if receiver is not None:
                if update['revision'] < receiver['revision']:
                    # Stale copy or stale revoke: never resurrect old state.
                    continue
                if update['revision'] == receiver['revision']:
                    if (receiver['action'] == update['action']
                            and receiver['received'] == update['received']):
                        continue
                    raise ArchiveError(
                        'same revision with different content is rejected')
            frame['receivers'][update['capture_id']] = {
                'revision': update['revision'],
                'action': update['action'],
                'received': (list(update['received'])
                             if update['received'] is not None else None),
            }
            if frame_id not in touched:
                touched.append(frame_id)

        # Compare effective payloads before and after the whole batch so that
        # intermediate in-batch decoding states never become downstream events.
        changes = []
        for frame_id in sorted(touched):
            before = self._effective_payload(
                frame_id, self._state['frames'])
            after = self._effective_payload(
                frame_id, projected['frames'])
            if before == after:
                continue
            previous_seq = self._state['published'].get(frame_id)
            seq = projected['next_seq']
            projected['next_seq'] += 1
            if before is None and after is not None:
                event = {'seq': seq, 'frame_id': frame_id, 'type': 'write',
                         'previous_seq': previous_seq, 'payload': after}
                projected['published'][frame_id] = seq
            elif before is not None and after is None:
                event = {'seq': seq, 'frame_id': frame_id, 'type': 'revoke',
                         'previous_seq': previous_seq}
                projected['published'].pop(frame_id, None)
            else:
                event = {'seq': seq, 'frame_id': frame_id, 'type': 'replace',
                         'previous_seq': previous_seq, 'payload': after}
                projected['published'][frame_id] = seq
            projected['events'].append(event)
            changes.append(event)

        receipt = {'batch_id': batch_id, 'changes': changes}
        projected['receipts'][batch_id] = {
            'canonical_request': canonical,
            'receipt_json': json.dumps(receipt, ensure_ascii=False,
                                       sort_keys=True, allow_nan=False),
        }
        # Durable state and in-memory state move together: once os.replace
        # lands, reopening must show the whole batch even if the answer is
        # lost before this method returns.
        self._atomic_write(projected, crash,
                           on_replaced=lambda: setattr(self, '_state', projected))
        return json.loads(projected['receipts'][batch_id]['receipt_json'])

    # -------------------------------------------------------------- durability
    def _check_open(self):
        if self._closed:
            raise ArchiveError('archive is closed')

    def _atomic_write(self, state, crash, on_replaced):
        data = (json.dumps(state, ensure_ascii=False, sort_keys=True,
                           allow_nan=False) + '\n').encode('utf-8')
        parent = self._state_path.parent
        fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=_TMP_PREFIX,
                                        suffix='.tmp')
        tmp_path = Path(tmp_name)
        simulated = False
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if crash == 'staged':
                simulated = True
                raise CrashSimulated('interrupted before state replacement')
            os.replace(tmp_path, self._state_path)
            tmp_path = None
            on_replaced()
            if crash == 'replaced':
                simulated = True
                raise CrashSimulated('interrupted after replacement')
            self._fsync_dir(parent)
            if crash == 'synced':
                simulated = True
                raise CrashSimulated('interrupted before response')
        finally:
            # On a simulated crash the orphan temp file is intentionally left
            # for the next open() to clean up, mirroring a real interruption.
            if tmp_path is not None and not simulated:
                tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_dir(directory):
        flags = os.O_RDONLY
        if hasattr(os, 'O_DIRECTORY'):
            flags |= os.O_DIRECTORY
        fd = os.open(directory, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
