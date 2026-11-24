"""Joint decoding of one continuously encoded, frame-split telemetry session.

The capture side cuts one continuous convolutional encoding into independently
identified transport frames, but the encoder register is never reset at a frame
boundary: the trellis state simply carries over.  The single-frame
:func:`telemetry_trellis.decode.decode` / :func:`telemetry_trellis.service.process`
contract deliberately treats every frame as a zero-state record, so it cannot
recover such a session.  This module adds the independent offline data API
:func:`decode_session`, which scores the whole ordered session on one continuous
state path and then attributes the result back to the input frames.

Input and state model
---------------------
A request is one object with ``session_id``, ``constraint``, ``generators``,
``frames`` (1..64, array order == encoding order, never sorted by id),
``terminated`` and a session-level ``tail_received``.  Each frame carries a
unique ``id``, a ``payload_length`` of 1..500 bits and a ``received`` array of
exactly ``payload_length * len(generators)`` strict 0/1/null symbols; the
session payload totals at most 20000 bits.  The encoder starts in state zero and
the state reached after each frame continues into the next.  Only after every
frame payload does a terminated session append ``constraint-1`` forced-zero tail
steps whose observation arrives once in ``tail_received``; the tail never
belongs to any frame and an unterminated session must carry an empty tail.

Scoring and delivery
--------------------
The minimum hard-decision Hamming distance is computed over the *whole* session
with one Viterbi sweep (null symbols cost nothing), keeping the exact number of
complete state paths attaining the minimum and the lexicographically smallest
full representative.  Termination (final state zero) is applied once, at session
level.  A backward suffix-cost sweep then marks every trellis step on which more
than one source bit still occurs among the globally optimal paths, which yields
per-frame ambiguity from the complete optimal path set rather than from any
local re-decode.  Path counts are plain Python integers (a 500-bit fully erased
session returns ``2**500``); payloads are never enumerated.

Only the Python standard library is used; every input and output is plain JSON
data and no device is contacted.
"""
from array import array

from .checks import array as _array
from .checks import fields, ident, integer, unique
from .code import transition

MAX_FRAMES = 64
MAX_FRAME_PAYLOAD = 500
MAX_SESSION_PAYLOAD = 20000
_INF = 2 ** 31 - 1


class SessionError(ValueError):
    """Raised for malformed session requests; no partial frame result is made."""


def _strict_symbol(value):
    # bool is a subclass of int; the single-frame contract rejects booleans.
    if value is not None and (type(value) is not int or value not in (0, 1)):
        raise SessionError('received symbols must be strict 0, 1 or null')
    return value


def _validate_session(request):
    """Validate and normalize a session request before any trellis work."""
    try:
        return _validate_session_body(request)
    except SessionError:
        raise
    except ValueError as exc:
        # The shared check helpers raise plain ValueErrors; keep one error
        # type for the session API without losing the specific message.
        raise SessionError(str(exc)) from exc


def _validate_session_body(request):
    if not isinstance(request, dict):
        raise SessionError('session request must be an object')
    fields(request, ('session_id', 'constraint', 'generators', 'frames',
                     'terminated', 'tail_received'))
    session_id = ident(request['session_id'])
    constraint = integer(request['constraint'], 2, 7)
    generators = [integer(g, 1, (1 << constraint) - 1)
                  for g in _array(request['generators'], 2, 4)]
    unique(generators)
    if type(request['terminated']) is not bool:
        raise SessionError('terminated must be a boolean')
    width = len(generators)

    raw_frames = _array(request['frames'], 1, MAX_FRAMES)
    frames = []
    seen_ids = set()
    total_payload = 0
    for raw_frame in raw_frames:
        if not isinstance(raw_frame, dict):
            raise SessionError('each frame must be an object')
        fields(raw_frame, ('id', 'payload_length', 'received'))
        frame_id = ident(raw_frame['id'])
        if frame_id in seen_ids:
            raise SessionError(f'duplicate frame id: {frame_id}')
        seen_ids.add(frame_id)
        length = integer(raw_frame['payload_length'], 1, MAX_FRAME_PAYLOAD)
        total_payload += length
        if total_payload > MAX_SESSION_PAYLOAD:
            raise SessionError(
                f'session payload exceeds {MAX_SESSION_PAYLOAD} bits')
        expected = length * width
        received = _array(raw_frame['received'], expected, expected)
        for symbol in received:
            _strict_symbol(symbol)
        frames.append({'id': frame_id, 'length': length,
                       'received': list(received)})

    tail_steps = constraint - 1 if request['terminated'] else 0
    expected_tail = tail_steps * width
    tail_received = _array(request['tail_received'], expected_tail,
                           expected_tail)
    for symbol in tail_received:
        _strict_symbol(symbol)
    return {
        'session_id': session_id,
        'constraint': constraint,
        'generators': generators,
        'frames': frames,
        'terminated': request['terminated'],
        'tail_received': list(tail_received),
    }


def _edge_metric(word, observation):
    return sum(observed is not None and observed != expected
               for observed, expected in zip(observation, word))


def decode_session(request):
    """Jointly decode one continuously encoded session split into frames.

    Returns a plain-data report with the session-level minimum ``distance``,
    the exact ``optimal_paths`` count over complete state paths, the
    ``ambiguous`` flag, the lexicographically smallest full representative
    ``payload`` and one entry per input frame (in request order) holding its
    ``id``, half-open ``range`` inside the concatenated payload, the
    representative ``payload`` slice and its frame-level ``ambiguous`` flag.

    Raises :class:`SessionError` for any malformed input; validation finishes
    before decoding starts, so rejection never returns partial frame results
    and never mutates the request.
    """
    req = _validate_session(request)
    constraint = req['constraint']
    generators = req['generators']
    width = len(generators)
    terminated = req['terminated']

    # One observation word per trellis step in encoding order.  Frame payload
    # steps come first, then the single session-level tail block.  Boundaries
    # are cumulative payload lengths and therefore the half-open frame ranges.
    observations = []
    boundaries = []
    total_payload = 0
    for frame in req['frames']:
        received = frame['received']
        for offset in range(0, len(received), width):
            observations.append(tuple(received[offset:offset + width]))
        total_payload += frame['length']
        boundaries.append(total_payload)
    tail_received = req['tail_received']
    for offset in range(0, len(tail_received), width):
        observations.append(tuple(tail_received[offset:offset + width]))
    total_steps = len(observations)
    state_count = 1 << (constraint - 1)

    # Forward Viterbi.  Live per-state row holds minimum metric, the packed
    # lexicographic survivor and the exact tie count; only the minimum-cost
    # column is mirrored into the flat forward table for the backward pass.
    # Packed prefixes place the first bit in the highest remaining position,
    # so numeric order equals bit-string lexicographic order.
    forward_costs = array('i', [_INF]) * ((total_steps + 1) * state_count)
    forward_costs[0] = 0
    paths = {0: (0, 0, 1)}
    for step, observation in enumerate(observations):
        shift = total_steps - step - 1
        grouped = {}
        for state, (cost, packed, count) in paths.items():
            for bit in (0, 1):
                target, word = transition(state, bit, constraint, generators)
                metric = cost + _edge_metric(word, observation)
                grouped.setdefault(target, []).append(
                    (metric, packed | (bit << shift), count))
        next_paths = {}
        for target, candidates in grouped.items():
            best_metric = min(metric for metric, _, _ in candidates)
            winners = [(packed, count)
                       for metric, packed, count in candidates
                       if metric == best_metric]
            representative = min(packed for packed, _ in winners)
            next_paths[target] = (
                best_metric, representative,
                sum(count for _, count in winners))
            forward_costs[(step + 1) * state_count + target] = best_metric
        paths = next_paths

    # Termination is a session-level rule only: terminated sessions must finish
    # in state zero after the shared tail, unterminated ones accept any state.
    if terminated:
        eligible = (0,) if 0 in paths else ()
    else:
        eligible = tuple(sorted(paths))
    if not eligible:
        raise SessionError('no legal path reaches the required final state')
    best = min(paths[state][0] for state in eligible)
    winning_states = [state for state in eligible
                      if paths[state][0] == best]
    optimal_paths = sum(paths[state][2] for state in winning_states)
    representative_packed = min(paths[state][1] for state in winning_states)
    representative_bits = [
        (representative_packed >> (total_steps - 1 - index)) & 1
        for index in range(total_steps)]
    if terminated and any(bit != 0 for bit in
                          representative_bits[total_payload:]):
        raise SessionError('termination invariant')
    representative_payload = representative_bits[:total_payload]

    # Backward suffix sweep.  One unrestricted minimum-suffix row is kept at a
    # time; while it is available the step's optimal-bit union is decided using
    # prefix minimum + edge + suffix minimum == global best.  A payload step is
    # ambiguous when optimal paths take both source bits there, evaluated over
    # the complete global optimum rather than any frame-local re-decode.
    suffix_next = [_INF] * state_count
    if terminated:
        suffix_next[0] = 0
    else:
        for state in range(state_count):
            if forward_costs[total_steps * state_count + state] < _INF:
                suffix_next[state] = 0
    ambiguous_steps = set()
    for step in range(total_steps - 1, -1, -1):
        observation = observations[step]
        suffix_current = [_INF] * state_count
        optimal_bits = set()
        for origin in range(state_count):
            prefix_cost = forward_costs[step * state_count + origin]
            if prefix_cost >= _INF:
                continue
            best_suffix = _INF
            for bit in (0, 1):
                target, word = transition(origin, bit, constraint, generators)
                suffix = suffix_next[target]
                if suffix >= _INF:
                    continue
                edge = _edge_metric(word, observation)
                candidate = edge + suffix
                if candidate < best_suffix:
                    best_suffix = candidate
                if prefix_cost + edge + suffix == best:
                    optimal_bits.add(bit)
            suffix_current[origin] = best_suffix
        if len(optimal_bits) > 1:
            ambiguous_steps.add(step)
        suffix_next = suffix_current

    frame_reports = []
    start = 0
    for frame, end in zip(req['frames'], boundaries):
        frame_reports.append({
            'id': frame['id'],
            'range': [start, end],
            'payload': representative_payload[start:end],
            'ambiguous': any(step in ambiguous_steps
                             for step in range(start, end)),
        })
        start = end
    return {
        'session_id': req['session_id'],
        'distance': best,
        'optimal_paths': optimal_paths,
        'ambiguous': optimal_paths != 1,
        'payload': representative_payload,
        'frames': frame_reports,
    }
