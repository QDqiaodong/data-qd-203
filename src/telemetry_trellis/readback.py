"""Offline readback-window scheduling and reply settlement.

A captured frame may stay ambiguous after the initial decode: the single-frame
:func:`telemetry_trellis.decode.decode` keeps the exact number of minimum
Hamming-distance paths but only returns one representative payload.  The
receiver still holds the short raw capture and can re-read erased symbol
positions, but each readback *window* costs quota and a whole window may come
back empty.

This module adds two offline, device-free entry points on top of the existing
single-frame contract:

* :func:`plan_readback` — decide which windows are worth requesting so that
  every pair of *original* optimal payloads stays distinguishable even when any
  one scheduled window returns nothing;
* :func:`settle_readback` — merge the actual window replies into the capture
  held when the plan was made, then re-decode the whole legal path set.

Everything is plain JSON data; the continuation document is self-contained, so
settlement runs in another process without any live object or a "latest
capture" lookup.  Only the Python standard library is used.
"""
import hashlib
import json

from .checks import array, fields, ident, integer, unique
from .code import encode, transition
from .decode import decode
from .io import load as _load_json
from .io import publish as _publish_json

READBACK_VERSION = 1
MAX_WINDOWS = 16
MAX_QUOTA = 16000
MAX_WINDOW_FEE = 1000
MAX_DISTINGUISH_CANDIDATES = 32
BUNDLE_KIND = 'telemetry_trellis.readback_continuation'
ORDER_KIND = 'telemetry_trellis.readback_order'
_STATUS_UNIQUE = 'unique'
_STATUS_TOO_MANY = 'too_many_candidates'
_STATUS_UNSCHEDULABLE = 'unschedulable'
_STATUS_SCHEDULED = 'scheduled'


class ReadbackError(ValueError):
    """Raised for malformed continuation/reply data and binding mismatches."""


# ---------------------------------------------------------------- validation
def _strict_bit(value):
    # bool is a subclass of int; the single-frame contract rejects booleans.
    if type(value) is not int or value not in (0, 1):
        raise ValueError('received symbols must be strict 0, 1 or null')
    return value


def _validate_request(request):
    if not isinstance(request, dict):
        raise ValueError('readback request must be an object')
    fields(request, ('capture_id', 'constraint', 'generators',
                     'payload_length', 'terminated', 'received', 'quota',
                     'windows'))
    capture_id = ident(request['capture_id'])
    constraint = integer(request['constraint'], 2, 7)
    generators = [integer(g, 1, (1 << constraint) - 1)
                  for g in array(request['generators'], 2, 4)]
    unique(generators)
    length = integer(request['payload_length'], 1, 500)
    if type(request['terminated']) is not bool:
        raise ValueError('terminated must be boolean')
    quota = integer(request['quota'], 0, MAX_QUOTA)
    steps = length + (constraint - 1 if request['terminated'] else 0)
    symbol_count = steps * len(generators)
    received = array(request['received'], symbol_count, symbol_count)
    for symbol in received:
        if symbol is not None:
            _strict_bit(symbol)
    raw_windows = array(request['windows'], 1, MAX_WINDOWS)
    windows = []
    window_ids = []
    for item in raw_windows:
        fields(item, ('id', 'start', 'end', 'fee'))
        window_id = ident(item['id'])
        start = integer(item['start'], 0, symbol_count)
        end = integer(item['end'], 0, symbol_count)
        fee = integer(item['fee'], 1, MAX_WINDOW_FEE)
        if not start < end:
            raise ValueError('window half-open range must be non-empty')
        if end > symbol_count:
            raise ValueError('window range must stay inside expanded symbols')
        window_ids.append(window_id)
        windows.append({'id': window_id, 'start': start, 'end': end,
                        'fee': fee})
    unique(window_ids)
    # Catalogue order has no business meaning; stable ID order is used for all
    # decisions and for the deterministic ID-list tie-break.
    windows.sort(key=lambda item: item['id'])
    for window in windows:
        window['requested'] = [i for i in range(window['start'], window['end'])
                               if received[i] is None]
    return {
        'capture_id': capture_id,
        'constraint': constraint,
        'generators': generators,
        'payload_length': length,
        'terminated': request['terminated'],
        'received': list(received),
        'quota': quota,
        'windows': windows,
    }


def _canonical_request(request):
    """Canonical JSON of the full request.

    Dict key order and window catalogue order are irrelevant; list order that
    carries meaning (generators, received, requested coordinates) is preserved.
    """
    canonical_windows = [{
        'id': w['id'], 'start': w['start'], 'end': w['end'], 'fee': w['fee'],
    } for w in sorted(request['windows'], key=lambda w: w['id'])]
    canonical = {
        'capture_id': request['capture_id'],
        'constraint': request['constraint'],
        'generators': list(request['generators']),
        'payload_length': request['payload_length'],
        'terminated': request['terminated'],
        'received': list(request['received']),
        'quota': request['quota'],
        'windows': canonical_windows,
    }
    return json.dumps(canonical, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _schedule_id(canonical):
    return 'rb-' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]


# --------------------------------------------- exact optimal-payload enumeration
def _optimal_payloads(req, best_distance):
    """Enumerate the distinct optimal payloads without enumerating all paths.

    A forward Viterbi sweep keeps the minimum metric at every reachable state.
    Backward recursion only follows transitions that lie on a globally optimal
    path, using precomputed best suffix metrics.  As long as the frame has at
    most :data:`MAX_DISTINGUISH_CANDIDATES` optimal payloads the visited tree is
    tiny; the 500-bit cap is handled because the sweep itself is per-state DP.
    """
    constraint = req['constraint']
    generators = req['generators']
    width = len(generators)
    received = req['received']
    steps = len(received) // width

    # Forward table: step -> state -> minimum metric reaching it.
    forward = [{0: 0}]
    for step in range(steps):
        observation = received[step * width:(step + 1) * width]
        nxt = {}
        for state, cost in forward[step].items():
            for bit in (0, 1):
                target, word = transition(state, bit, constraint, generators)
                metric = cost + sum(a is not None and a != b
                                    for a, b in zip(observation, word))
                old = nxt.get(target)
                if old is None or metric < old:
                    nxt[target] = metric
        forward.append(nxt)

    if req['terminated']:
        final_states = (0,)
    else:
        final_states = tuple(sorted(forward[steps]))

    # Backward table: best remaining metric from (step, state) to an eligible
    # final state, where ``step`` is the index of the next output word.
    backward = [None] * (steps + 1)
    backward[steps] = {state: 0 if state in final_states else None
                       for state in forward[steps]}
    for step in range(steps - 1, -1, -1):
        observation = received[step * width:(step + 1) * width]
        row = {}
        for state in forward[step]:
            options = []
            for bit in (0, 1):
                target, word = transition(state, bit, constraint, generators)
                suffix = backward[step + 1].get(target)
                if suffix is None:
                    continue
                edge = sum(a is not None and a != b
                           for a, b in zip(observation, word))
                options.append(edge + suffix)
            if options:
                row[state] = min(options)
        backward[step] = row

    payloads = set()

    def walk(step, state, cost, prefix):
        if step == steps:
            if state in final_states and cost == best_distance:
                payloads.add(tuple(prefix[:req['payload_length']]))
            return
        observation = received[step * width:(step + 1) * width]
        for bit in (0, 1):
            target, word = transition(state, bit, constraint, generators)
            edge = sum(a is not None and a != b
                       for a, b in zip(observation, word))
            new_cost = cost + edge
            suffix = backward[step + 1].get(target)
            if (suffix is None or new_cost + suffix != best_distance
                    or new_cost != forward[step + 1].get(target)):
                continue
            walk(step + 1, target, new_cost, prefix + (bit,))

    walk(0, 0, 0, ())
    return sorted(payloads)


# ------------------------------------------------------------- window selection
def _pair_requirements(req, payloads):
    """Return ``(pair_masks, impossible)`` over sorted window IDs.

    A window distinguishes a payload pair at coordinate ``i`` only when both
    payloads actually differ at ``i`` *and* ``i`` is erased in the original
    capture and therefore requested by that window.  A pair that fewer than two
    windows can distinguish makes every subset infeasible.
    """
    codewords = [encode(list(payload), req['constraint'], req['generators'],
                        req['terminated'])['symbols'] for payload in payloads]
    masks = []
    impossible = False
    for a in range(len(payloads)):
        for b in range(a + 1, len(payloads)):
            mask = 0
            for index, window in enumerate(req['windows']):
                if any(codewords[a][i] != codewords[b][i]
                       for i in window['requested']):
                    mask |= 1 << index
            masks.append(mask)
            if mask.bit_count() < 2:
                impossible = True
    return masks, impossible


def _choose_windows(req, pair_masks):
    """Minimum-cost window subset covering every pair at least twice.

    A pair is safe against one whole-window loss exactly when at least two
    selected windows distinguish it.  Search is include-first over windows
    sorted by ID, which yields the lexicographically smallest ID list among
    equal-cost feasible subsets.
    """
    windows = req['windows']
    size = len(windows)
    best = None  # (cost, sorted index tuple)

    def search(index, chosen_mask, coverage, spent):
        nonlocal best
        if spent > req['quota']:
            return
        if best is not None and spent >= best[0]:
            return
        # Pairs that the still-unvisited, not-yet-chosen windows cannot bring
        # to two covers can never become feasible on this branch.  Coverage is
        # indexed per pair: distinct pairs may share the same window mask and
        # must not collapse onto one shared counter.
        remaining = (((1 << size) - 1) ^ ((1 << index) - 1)) & ~chosen_mask
        for pair_index, mask in enumerate(pair_masks):
            if (coverage[pair_index] + (mask & remaining).bit_count()) < 2:
                return
        if index == size:
            if all(count >= 2 for count in coverage):
                chosen = tuple(i for i in range(size)
                               if chosen_mask & (1 << i))
                candidate = (spent, chosen)
                if best is None or candidate < best:
                    best = candidate
            return
        # Include-first: equal cost favours the lexicographically smaller list.
        window_mask = 1 << index
        for pair_index, mask in enumerate(pair_masks):
            if mask & window_mask:
                coverage[pair_index] += 1
        search(index + 1, chosen_mask | window_mask, coverage,
               spent + windows[index]['fee'])
        for pair_index, mask in enumerate(pair_masks):
            if mask & window_mask:
                coverage[pair_index] -= 1
        search(index + 1, chosen_mask, coverage, spent)

    search(0, 0, [0] * len(pair_masks), 0)
    return best


# -------------------------------------------------------------------- planning
def plan_readback(request):
    """Plan readback windows for one already frame-bounded capture.

    Returns a JSON-able result whose ``status`` is one of ``unique`` (nothing to
    request), ``too_many_candidates`` (more than 32 original optimal payloads;
    no schedule), ``unschedulable`` (no within-quota subset guarantees
    distinction) or ``scheduled``.  Non-scheduled results still carry the
    original minimum ``distance`` and exact ``optimal_paths``.
    """
    req = _validate_request(request)
    frame = {'id': req['capture_id'], 'length': req['payload_length'],
             'terminated': req['terminated'], 'received': req['received']}
    evidence = decode(frame, req['constraint'], req['generators'])
    distance = evidence['distance']
    path_count = evidence['optimal_paths']
    base = {'capture_id': req['capture_id'], 'distance': distance,
            'optimal_paths': path_count, 'quota': req['quota']}

    if path_count == 1:
        return {**base, 'status': _STATUS_UNIQUE, 'schedule_id': None,
                'selected_windows': [], 'total_fee': 0,
                'readback_order': None, 'continuation': None}
    if path_count > MAX_DISTINGUISH_CANDIDATES:
        return {**base, 'status': _STATUS_TOO_MANY, 'schedule_id': None,
                'selected_windows': [], 'total_fee': 0,
                'readback_order': None, 'continuation': None}

    payloads = _optimal_payloads(req, distance)
    if len(payloads) != path_count:
        raise RuntimeError('optimal payload enumeration disagrees with decoder')
    pair_masks, impossible = _pair_requirements(req, payloads)
    best = None if impossible else _choose_windows(req, pair_masks)
    if best is None:
        return {**base, 'status': _STATUS_UNSCHEDULABLE, 'schedule_id': None,
                'selected_windows': [], 'total_fee': 0,
                'readback_order': None, 'continuation': None}

    total_fee, chosen = best
    canonical = _canonical_request(req)
    schedule_id = _schedule_id(canonical)
    selected = []
    for index in chosen:
        window = req['windows'][index]
        selected.append({'window_id': window['id'],
                         'requested_coordinates': list(window['requested']),
                         'fee': window['fee']})
    order = {
        'kind': ORDER_KIND,
        'version': READBACK_VERSION,
        'schedule_id': schedule_id,
        'capture_id': req['capture_id'],
        'windows': selected,
        'total_fee': total_fee,
    }
    continuation = {
        'kind': BUNDLE_KIND,
        'version': READBACK_VERSION,
        'schedule_id': schedule_id,
        'request': json.loads(canonical),
        'selected_window_ids': [item['window_id'] for item in selected],
        'total_fee': total_fee,
        'original_distance': distance,
        'original_optimal_paths': path_count,
    }
    return {**base, 'status': _STATUS_SCHEDULED, 'schedule_id': schedule_id,
            'selected_windows': [item['window_id'] for item in selected],
            'total_fee': total_fee, 'readback_order': order,
            'continuation': continuation}


# ------------------------------------------------------------- persistence
def save_continuation(path, continuation):
    """Atomically write a continuation document to a new JSON file."""
    _publish_json(path, continuation)


def load_continuation(path):
    """Load and structurally validate a continuation document.

    Returns the JSON-able continuation dict, ready for :func:`settle_readback`
    in another process; :func:`settle_readback` re-validates every binding.
    """
    return _validate_continuation(_load_json(path))[0]


def save_order(path, order):
    """Atomically write the receiver-facing readback order."""
    _publish_json(path, order)


# ---------------------------------------------------------------- settlement
def _validate_continuation(bundle):
    if not isinstance(bundle, dict):
        raise ReadbackError('continuation must be an object')
    if bundle.get('kind') != BUNDLE_KIND:
        raise ReadbackError('not a readback continuation document')
    if bundle.get('version') != READBACK_VERSION:
        raise ReadbackError('unsupported continuation version')
    for key in ('schedule_id', 'request', 'selected_window_ids', 'total_fee',
                'original_distance', 'original_optimal_paths'):
        if key not in bundle:
            raise ReadbackError(f'continuation missing field: {key}')
    req = _validate_request(bundle['request'])
    if bundle['schedule_id'] != _schedule_id(_canonical_request(req)):
        raise ReadbackError('continuation does not bind its embedded request')
    selected = bundle['selected_window_ids']
    if not isinstance(selected, list) or not selected:
        raise ReadbackError('continuation must select at least one window')
    known = {w['id'] for w in req['windows']}
    if any(not isinstance(w, str) or w not in known for w in selected):
        raise ReadbackError('continuation references unknown windows')
    if len(set(selected)) != len(selected):
        raise ReadbackError('duplicate selected window in continuation')
    if type(bundle['total_fee']) is not int:
        raise ReadbackError('continuation total_fee must be an integer')
    return bundle, req


def _validate_reply_entries(reply):
    if not isinstance(reply, dict):
        raise ReadbackError('reply envelope must be an object')
    if reply.get('schedule_id') is None or not isinstance(
            reply['schedule_id'], str):
        raise ReadbackError('reply must carry a schedule_id')
    entries = reply.get('windows')
    if not isinstance(entries, list):
        raise ReadbackError('reply windows must be a list')
    seen = set()
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReadbackError('each window reply must be an object')
        allowed = {'window_id', 'available', 'bits'}
        if set(entry.keys()) - allowed:
            raise ReadbackError('unknown window reply field')
        if not {'window_id', 'available'} <= set(entry.keys()):
            raise ReadbackError('window reply missing window_id or available')
        window_id = entry['window_id']
        if not isinstance(window_id, str):
            raise ReadbackError('window_id must be a string')
        if window_id in seen:
            raise ReadbackError('duplicate window reply entry')
        seen.add(window_id)
        if type(entry['available']) is not bool:
            raise ReadbackError('available must be a boolean')
        if not entry['available']:
            if 'bits' in entry and entry['bits'] is not None:
                raise ReadbackError('unavailable window must carry no bits')
            normalized.append((window_id, False, None))
            continue
        bits = entry.get('bits')
        if not isinstance(bits, dict):
            raise ReadbackError('available window must carry a bits object')
        parsed = {}
        for raw_index, value in bits.items():
            if not isinstance(raw_index, str) or not raw_index.isdigit():
                raise ReadbackError('bit keys must be decimal index strings')
            index = int(raw_index)
            if index in parsed:
                raise ReadbackError('duplicate coordinate inside window reply')
            if type(value) is not int or value not in (0, 1):
                raise ReadbackError('readback bits must be strict 0 or 1')
            parsed[index] = value
        normalized.append((window_id, True, parsed))
    return reply['schedule_id'], normalized


def settle_readback(continuation, reply, original_request=None):
    """Validate replies against one schedule, merge them, then re-decode.

    ``continuation`` is either the dict returned by :func:`plan_readback` or a
    reloaded continuation document.  ``reply`` is the window-result envelope;
    window order is irrelevant.  ``original_request`` may supply the referenced
    original input separately; when given it must match the embedded request
    exactly or the settlement is rejected.

    Any illegal data, missing/extra window, cross-schedule material or bit
    conflict raises :class:`ReadbackError` and no partial result is returned;
    neither the original input nor the continuation document is modified.
    """
    if not isinstance(continuation, dict):
        raise ReadbackError('continuation must be an object')
    bundle, req = _validate_continuation(continuation)
    if original_request is not None:
        referenced = _validate_request(original_request)
        if _canonical_request(referenced) != _canonical_request(req):
            raise ReadbackError('supplied original request does not match '
                                'the scheduled capture')
    reply_schedule, entries = _validate_reply_entries(reply)
    if reply_schedule != bundle['schedule_id']:
        raise ReadbackError('reply belongs to a different schedule')

    selected_ids = list(bundle['selected_window_ids'])
    selected_set = set(selected_ids)
    by_id = {w['id']: w for w in req['windows']}
    if len(entries) != len(selected_ids):
        raise ReadbackError('reply must contain exactly one entry per '
                            'selected window')
    entry_ids = {window_id for window_id, _, _ in entries}
    if entry_ids != selected_set:
        raise ReadbackError('reply window set does not match the schedule')

    # Full structural validation happens before any merge so rejection leaves
    # no partially settled state.
    observations = {}
    unavailable = []
    for window_id, available, bits in entries:
        window = by_id[window_id]
        if not available:
            unavailable.append(window_id)
            continue
        requested = list(window['requested'])
        if sorted(bits.keys()) != sorted(requested):
            raise ReadbackError(
                f'window {window_id} reply coordinates do not match the '
                'exact scheduled request')
        for index, value in bits.items():
            prior = observations.get(index)
            if prior is not None and prior != value:
                raise ReadbackError(
                    f'conflicting readback values at coordinate {index}')
            observations[index] = value

    merged = list(req['received'])
    filled = sorted(observations)
    for index in filled:
        if merged[index] is not None:
            # Windows only request erased positions, but keep the invariant
            # explicit: known symbols are never overwritten.
            raise ReadbackError('readback targeted an already known symbol')
        merged[index] = observations[index]

    frame = {'id': req['capture_id'], 'length': req['payload_length'],
             'terminated': req['terminated'], 'received': merged}
    evidence = decode(frame, req['constraint'], req['generators'])
    return {
        'schedule_id': bundle['schedule_id'],
        'capture_id': req['capture_id'],
        'status': 'settled',
        'filled_indices': filled,
        'unavailable_windows': sorted(unavailable),
        'decode': evidence,
    }
