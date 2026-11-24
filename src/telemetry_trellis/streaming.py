"""Incremental, in-order delivery for one shard-arriving telemetry record.

A capture endpoint knows a record's coding parameters and total coded length up
front, but its received array arrives in fragments and it may still *correct*
recent observations.  A monotonically advancing confirmation position says
which earlier observations can no longer change.  Downstream consumers read
payload bits in order and cannot retract them, so the useful question is: using
only fully confirmed trellis steps, which payload prefix is already certain?

The module is an offline state machine over unfolded symbol coordinates:

* :func:`new_state` -- start a session for one record;
* :func:`append` / :func:`replace` -- grow or correct the received array;
* :func:`confirm` -- advance the confirmed half-open prefix and deliver;
* :func:`finish` -- require full confirmation, deliver the remainder, and emit
  the same single-frame evidence a one-shot ``process`` call would produce;
* :func:`progress` -- report received, confirmed and delivered lengths;
* :func:`save_state` / :func:`load_state` -- a plain-JSON document another
  process can resume from without replaying any retired raw fragment.

Every operation returns a report with a new state; the argument state is never
modified.  An illegal operation raises before any successor exists, so it
neither changes state nor produces a delivery.

Coordinates and coding
----------------------
Expanded symbol coordinates run ``0 .. steps*width``.  Step ``j`` emits symbols
``[j*width, (j+1)*width)`` and consumes source bit ``j``.  Only source bits
``0 .. payload_length-1`` are payload; a terminated record's trailing
``constraint-1`` zero bits are a tail and never payload.  Coding follows the
repository shift convention and zero initial state (:func:`code.encode`), and
the encoded length is fixed when the state is created.

Early-delivery rule
-------------------
After a confirmation let ``K = confirmed // width`` be the number of fully
confirmed whole steps.  Only those ``K`` steps take part; a fully received but
not confirmed step and a half-confirmed step are ignored.  For every reachable
state at step ``K`` consider **all** minimum-cost legal bit prefixes arriving
there -- not one representative per state and not only the globally cheapest
state.  Their longest common prefix at payload positions is the delivery
boundary; the newly covered part is delivered in one absolute range.  Nothing
is guessed and nothing is held back until the end.

Final rule
----------
:func:`finish` requires the whole array received and confirmed.  It scores all
complete legal paths with the repository's Hamming rule, delivers the remaining
common payload prefix of the global optimum, and returns decoding evidence equal
to a one-shot ``process`` call on the final received array: exact path count,
lexicographic representative payload, selected final state, state path,
re-encoded symbols, distance and the corrected/erased expanded indices.  The
representative in the evidence is not itself a delivery.

Retirement
----------
Once a payload bit is delivered, every complete step feeding it leaves the
retained input.  Delivery only ever covers bits shared by *every* minimum-cost
prefix, so the delivered prefix fixes one deterministic state (call it the
anchor) and one mismatch cost.  From that anchor onward the trellis never needs
the retired raw symbols: subsequent delivery recomputes from the anchor, and the
final evidence composes each retired step from its stored per-symbol
``match``/``mismatch``/``erased`` provenance and a short digest.  The evidence
still covers every coordinate because the delivered bits fix the expected
codeword of every retired step.

Only the Python standard library is used; every document is plain JSON.
"""
import hashlib

from .checks import array, fields, ident, integer, unique
from .code import encode, transition
from .io import load as _load_json
from .io import publish as _publish_json

STATE_VERSION = 1
STATE_KIND = 'telemetry_trellis.stream_state'
MAX_PAYLOAD = 500
STATUS_VALUES = ('match', 'mismatch', 'erased')
_STATE_FIELDS = ('kind', 'version', 'frame_id', 'constraint', 'generators',
                 'payload_length', 'terminated', 'steps', 'width',
                 'symbol_count', 'received_count', 'confirmed', 'delivered',
                 'delivered_bits', 'done', 'pending', 'retired', 'evidence')


class StreamingError(ValueError):
    """Raised for malformed state documents and illegal stream operations."""


# ------------------------------------------------------------------ helpers
def _strict_symbol(value):
    # bool is a subclass of int; the single-frame contract rejects booleans.
    if value is not None and (type(value) is not int or value not in (0, 1)):
        raise ValueError('received symbols must be strict 0, 1 or null')
    return value


def _symbols(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError('symbols must be a list')
    if not value:
        raise ValueError('observation fragment must be non-empty')
    return [_strict_symbol(item) for item in value]


def _tail(constraint, terminated):
    return constraint - 1 if terminated else 0


def _running(state):
    if not isinstance(state, dict) or state.get('kind') != STATE_KIND:
        raise StreamingError('not a streaming session state')
    if state.get('version') != STATE_VERSION:
        raise StreamingError('unsupported streaming state version')
    if state.get('done'):
        raise StreamingError('session already finished')


def _clone(state):
    result = dict(state)
    result['pending'] = {key: list(block)
                         for key, block in state['pending'].items()}
    result['retired'] = list(state['retired'])
    result['delivered_bits'] = list(state['delivered_bits'])
    return result


def _advance(state, source, bit):
    return transition(source, bit, state['constraint'], state['generators'])


def _edge_metric(word, observation):
    return sum(observed is not None and observed != expected
               for observed, expected in zip(observation, word))


def _block(state, step_index):
    """Complete raw observation of one not-yet-retired step."""
    block = state['pending'].get(str(step_index))
    if block is None or len(block) != state['width']:
        raise StreamingError(f'observation block {step_index} is not retained')
    return block


def _anchor(state):
    """State and mismatch cost reached by the already delivered payload."""
    source = 0
    for bit in state['delivered_bits']:
        source, _ = _advance(state, source, bit)
    cost = sum(block['cost'] for block in state['retired'])
    return source, cost


def _forward(state, first, last):
    """Minimum metric per state over steps ``first .. last-1``.

    The base is the single anchor point fixed by the delivered payload, because
    every minimum-cost prefix shares those bits and therefore passes through it.
    Returns ``table[step] -> {state: minimum cost}``.
    """
    source, cost = _anchor(state)
    table = {first: {source: cost}}
    for step_index in range(first, last):
        observation = _block(state, step_index)
        following = {}
        for origin, prior in table[step_index].items():
            for bit in (0, 1):
                target, word = _advance(state, origin, bit)
                metric = prior + _edge_metric(word, observation)
                old = following.get(target)
                if old is None or metric < old:
                    following[target] = metric
        table[step_index + 1] = following
    return table


def _backward(state, first, last, forward):
    """Best remaining metric from each (step, state) to an eligible final.

    ``last`` is the number of steps.  Terminated records must end in state 0;
    unterminated records accept any final state, so the last row is all zero.
    """
    if state['terminated']:
        last_row = {0: 0} if 0 in forward[last] else {}
    else:
        last_row = {origin: 0 for origin in forward[last]}
    table = {last: last_row}
    for step_index in range(last - 1, first - 1, -1):
        observation = _block(state, step_index)
        row = {}
        for origin in forward[step_index]:
            best = None
            for bit in (0, 1):
                target, word = _advance(state, origin, bit)
                suffix = table[step_index + 1].get(target)
                if suffix is None:
                    continue
                metric = _edge_metric(word, observation) + suffix
                if best is None or metric < best:
                    best = metric
            if best is not None:
                row[origin] = best
        table[step_index] = row
    return table


def _common_prefix_bits(state, first, forward, backward, total_best):
    """Newly fixed bits from the anchor while the global optimum stays common.

    Walks the single anchor state; at each step it keeps the bit shared by every
    minimum-cost edge that still reaches an eligible final state at total cost
    ``total_best``.  Stops at the first step where two bits qualify or at the
    payload boundary.
    """
    source, _ = _anchor(state)
    payload_length = state['payload_length']
    steps = state['steps']
    bits = []
    for step_index in range(first, steps):
        if step_index >= payload_length:
            break
        observation = _block(state, step_index)
        options = []
        for bit in (0, 1):
            target, word = _advance(state, source, bit)
            suffix = backward[step_index + 1].get(target)
            if suffix is None:
                continue
            if forward[step_index][source] + _edge_metric(word, observation) \
                    + suffix == total_best:
                options.append((bit, target))
        if len(options) != 1:
            break
        bit, target = options[0]
        bits.append(bit)
        source = target
    return bits


def _decode_paths(state, first):
    """Viterbi with exact counts and lexicographic survivors from the anchor.

    Reproduces :func:`telemetry_trellis.decode.decode` on the part of the trellis
    that can still be optimal.  The anchor is the unique minimum-cost entry
    state, so per-state minima and exact tie counts from there onward are
    unaffected by any other state at ``first``.
    """
    source, cost = _anchor(state)
    # state -> (minimum cost, lexicographically smallest continuation, tie count)
    paths = {source: (cost, (), 1)}
    for step_index in range(first, state['steps']):
        observation = _block(state, step_index)
        grouped = {}
        for origin, (prior, prefix, count) in paths.items():
            for bit in (0, 1):
                target, word = _advance(state, origin, bit)
                metric = prior + _edge_metric(word, observation)
                grouped.setdefault(target, []).append(
                    (metric, prefix + (bit,), count))
        following = {}
        for target, candidates in grouped.items():
            best_metric = min(metric for metric, _, _ in candidates)
            winners = [(prefix, count)
                       for metric, prefix, count in candidates
                       if metric == best_metric]
            representative = min(prefix for prefix, _ in winners)
            total = sum(count for _, count in winners)
            following[target] = (best_metric, representative, total)
        paths = following
    return paths


# --------------------------------------------------------------- public API
def new_state(frame_id, constraint, generators, payload_length, terminated):
    """Create the initial state document for one record.

    ``frame_id`` follows the archive identity rule; ``constraint`` is 2..7;
    ``generators`` holds 2..4 distinct positive masks below ``2**constraint``;
    ``payload_length`` is 1..500; ``terminated`` is a strict boolean.  The
    total encoded length and step count are fixed here and never change.
    """
    frame_id = ident(frame_id)
    constraint = integer(constraint, 2, 7)
    generators = [integer(g, 1, (1 << constraint) - 1)
                  for g in array(generators, 2, 4)]
    unique(generators)
    length = integer(payload_length, 1, MAX_PAYLOAD)
    if type(terminated) is not bool:
        raise ValueError('terminated must be boolean')
    steps = length + _tail(constraint, terminated)
    width = len(generators)
    return {
        'kind': STATE_KIND,
        'version': STATE_VERSION,
        'frame_id': frame_id,
        'constraint': constraint,
        'generators': list(generators),
        'payload_length': length,
        'terminated': terminated,
        'steps': steps,
        'width': width,
        'symbol_count': steps * width,
        'received_count': 0,
        'confirmed': 0,
        'delivered': 0,
        'delivered_bits': [],
        'done': False,
        'pending': {},
        'retired': [],
        'evidence': None,
    }


def progress(state):
    """Report received length, confirmation position and delivered length."""
    if not isinstance(state, dict) or state.get('kind') != STATE_KIND:
        raise StreamingError('not a streaming session state')
    return {
        'frame_id': state['frame_id'],
        'received_count': state['received_count'],
        'confirmed': state['confirmed'],
        'delivered': state['delivered'],
        'symbol_count': state['symbol_count'],
        'finished': state['done'],
    }


def append(state, symbols):
    """Append a non-empty fragment at the current received length.

    The fragment must start exactly at ``received_count``, may end inside a
    coding step, and must not push the array past the fixed total length.
    Appending never confirms anything and never delivers.
    """
    _running(state)
    values = _symbols(symbols)
    start = state['received_count']
    if start + len(values) > state['symbol_count']:
        raise ValueError('append would exceed the fixed total length')
    result = _clone(state)
    width = result['width']
    for offset, value in enumerate(values):
        index = start + offset
        step_index, position = divmod(index, width)
        key = str(step_index)
        block = result['pending'].get(key)
        if block is None:
            block = [None] * position
            result['pending'][key] = block
        elif len(block) != position:
            raise StreamingError('observation blocks are not contiguous')
        block.append(value)
    result['received_count'] = start + len(values)
    return _report(result, None)


def replace(state, start, symbols):
    """Replace a non-empty, non-confirmed range of already received symbols.

    ``start`` may not lie inside the confirmed prefix even when the new value is
    identical, and the whole half-open range must stay in the received,
    unconfirmed part.  Values may switch between 0/1 and ``None``.  No implicit
    confirmation or delivery happens; on any error the state is untouched.
    """
    _running(state)
    if type(start) is not int:
        raise ValueError('replacement start must be an integer')
    values = _symbols(symbols)
    end = start + len(values)
    if start < 0:
        raise ValueError('replacement start must be non-negative')
    if start < state['confirmed']:
        raise ValueError('replacement overlaps the confirmed prefix')
    if end > state['received_count']:
        raise ValueError('replacement must stay inside the received array')
    result = _clone(state)
    width = result['width']
    for offset, value in enumerate(values):
        index = start + offset
        step_index, position = divmod(index, width)
        block = result['pending'].get(str(step_index))
        if block is None or position >= len(block):
            raise StreamingError('replacement targets a retired observation')
        block[position] = value
    return _report(result, None)


def confirm(state, confirmed):
    """Advance the confirmed half-open prefix and deliver certain payload bits.

    ``confirmed`` may not move backwards or past ``received_count``.  After the
    move, only fully confirmed whole steps are used: every reachable state's
    complete set of minimum-cost prefixes is examined and their common payload
    prefix is delivered in one absolute range.  Confirming at an unchanged
    position re-sends nothing.
    """
    _running(state)
    confirmed = integer(confirmed, 0, state['symbol_count'])
    if confirmed < state['confirmed']:
        raise ValueError('confirmation position cannot move backwards')
    if confirmed > state['received_count']:
        raise ValueError('confirmation cannot exceed received length')
    result = _clone(state)
    result['confirmed'] = confirmed
    bits = _frontier(result)
    delivery = _retire(result, bits)
    return _report(result, delivery)


def finish(state):
    """Require full receipt and confirmation, then emit delivery and evidence.

    Fails without changing anything unless every symbol has been received and
    confirmed.  Returns the remaining common payload range of the global
    optimum plus the same evidence dict a one-shot ``process`` call produces.
    """
    _running(state)
    if state['received_count'] != state['symbol_count'] or \
            state['confirmed'] != state['symbol_count']:
        raise ValueError('finish requires the whole array received and confirmed')
    result = _clone(state)
    evidence, common = _final(result)
    delivery = _retire(result, common)
    result['done'] = True
    result['evidence'] = evidence
    return _report(result, delivery)


def save_state(path, state):
    """Atomically write a session state document to a new JSON file."""
    _publish_json(path, state)


def load_state(path):
    """Load and validate a session state document for another process."""
    document = _load_json(path)
    try:
        return _validate_state(document)
    except StreamingError:
        raise
    except ValueError as error:
        raise StreamingError(f'invalid session state: {error}') from error


# ------------------------------------------------------------- internals
def _report(state, delivery):
    report = {
        'frame_id': state['frame_id'],
        'received_count': state['received_count'],
        'confirmed': state['confirmed'],
        'delivered': state['delivered'],
        'symbol_count': state['symbol_count'],
        'finished': state['done'],
        'delivery': delivery,
        'state': state,
    }
    if state['done']:
        report['evidence'] = state['evidence']
    return report


def _frontier(state):
    """Bits fixed by the currently confirmed whole steps, from the anchor.

    Implements the early-delivery rule.  A state is *live* at step ``j`` when
    it lies on at least one minimum-cost prefix from the anchor to the
    confirmed boundary.  Walking forward from the anchor, the next bit is
    deliverable only when every live minimum-cost edge takes the same bit; the
    walk stops at the first step where two bits qualify, so a fully received
    (or half-received) step never contributes.
    """
    width = state['width']
    first = state['delivered']
    confirmed_steps = state['confirmed'] // width
    payload_length = state['payload_length']
    if confirmed_steps <= first:
        return []
    forward = _forward(state, first, confirmed_steps)
    live = {confirmed_steps: set(forward[confirmed_steps])}
    for step_index in range(confirmed_steps - 1, first - 1, -1):
        observation = _block(state, step_index)
        previous = set()
        for origin in forward[step_index]:
            for bit in (0, 1):
                target, word = _advance(state, origin, bit)
                if target in live[step_index + 1] and \
                        forward[step_index][origin] + \
                        _edge_metric(word, observation) == \
                        forward[step_index + 1].get(target):
                    previous.add(origin)
                    break
        live[step_index] = previous
    source, _ = _anchor(state)
    bits = []
    for step_index in range(first, confirmed_steps):
        if step_index >= payload_length:
            break
        observation = _block(state, step_index)
        options = []
        for bit in (0, 1):
            target, word = _advance(state, source, bit)
            if target in live[step_index + 1] and \
                    forward[step_index][source] + \
                    _edge_metric(word, observation) == \
                    forward[step_index + 1].get(target):
                options.append((bit, target))
        if len(options) != 1:
            break
        bit, target = options[0]
        bits.append(bit)
        source = target
    return bits


def _retire(state, bits):
    """Deliver ``bits`` from the current frontier and drop their raw blocks."""
    if not bits:
        return None
    width = state['width']
    start = state['delivered']
    source, _ = _anchor(state)
    for bit in bits:
        step_index = state['delivered']
        observation = _block(state, step_index)
        target, word = _advance(state, source, bit)
        status = ['erased' if observed is None else
                  ('match' if observed == expected else 'mismatch')
                  for observed, expected in zip(observation, word)]
        state['retired'].append({
            'step': step_index,
            'digest': _digest(observation),
            'status': status,
            'cost': sum(item == 'mismatch' for item in status),
        })
        del state['pending'][str(step_index)]
        state['delivered_bits'].append(bit)
        state['delivered'] += 1
        source = target
    return {'start': start, 'bits': list(bits), 'end': state['delivered']}


def _final(state):
    """Compute finish evidence and the remaining common payload bits."""
    first = state['delivered']
    forward = _forward(state, first, state['steps'])
    backward = _backward(state, first, state['steps'], forward)
    final_costs = forward[state['steps']]
    if state['terminated']:
        eligible = {0: final_costs[0]} if 0 in final_costs else {}
    else:
        eligible = dict(final_costs)
    if not eligible:
        raise ValueError('no legal path ends in an eligible final state')
    total_best = min(eligible.values())
    paths = _decode_paths(state, first)
    winners = [(origin, value) for origin, value in paths.items()
               if origin in eligible and value[0] == total_best]
    if not winners:
        raise StreamingError('composed evidence disagrees with the optimum')
    # Exactly the single-frame rule: the representative is the lexicographically
    # smallest continuation across winning final states, while the tie count
    # sums every winning state's path count.
    origin, (distance, continuation, _) = min(
        winners, key=lambda pair: pair[1][1])
    count = sum(value[2] for _, value in winners)
    bits = list(state['delivered_bits']) + list(continuation)
    # Terminated records append a zero tail; verify the invariant exactly as
    # the single-frame decoder does.
    payload = bits[:state['payload_length']]
    tail = _tail(state['constraint'], state['terminated'])
    if bits != payload + [0] * tail:
        raise ValueError('termination invariant')
    reference = encode(payload, state['constraint'], state['generators'],
                       state['terminated'])
    corrections, erasures = _provenance(state, reference['symbols'])
    evidence = {
        'id': state['frame_id'],
        'payload': payload,
        'selected_final_state': origin,
        'distance': distance,
        'optimal_paths': count,
        'ambiguous': count != 1,
        'reencoded': reference['symbols'],
        'state_path': reference['states'],
        'corrected_indices': corrections,
        'erasure_indices': erasures,
    }
    common = _common_prefix_bits(state, first, forward, backward, total_best)
    return evidence, common


def _provenance(state, reencoded):
    """Correction and erasure indices over retired and pending coordinates."""
    width = state['width']
    corrections = []
    erasures = []
    for step_index, block in enumerate(state['retired']):
        for position, status in enumerate(block['status']):
            index = step_index * width + position
            if status == 'mismatch':
                corrections.append(index)
            elif status == 'erased':
                erasures.append(index)
    for step_index in range(len(state['retired']), state['steps']):
        observation = state['pending'].get(str(step_index))
        if observation is None:
            raise StreamingError(f'missing observation block {step_index}')
        for position, observed in enumerate(observation):
            index = step_index * width + position
            if observed is None:
                erasures.append(index)
            elif observed != reencoded[index]:
                corrections.append(index)
    return corrections, erasures


def _digest(symbols):
    text = ''.join('n' if item is None else str(item) for item in symbols)
    return hashlib.sha256(text.encode('ascii')).hexdigest()[:16]


# --------------------------------------------------------------- validation
def _validate_state(state):
    """Structurally validate and re-check a loaded session document."""
    if not isinstance(state, dict) or state.get('kind') != STATE_KIND:
        raise StreamingError('not a streaming session state')
    fields(state, _STATE_FIELDS)
    if state['version'] != STATE_VERSION:
        raise StreamingError('unsupported streaming state version')
    ident(state['frame_id'])
    constraint = integer(state['constraint'], 2, 7)
    generators = [integer(g, 1, (1 << constraint) - 1)
                  for g in array(state['generators'], 2, 4)]
    unique(generators)
    length = integer(state['payload_length'], 1, MAX_PAYLOAD)
    if type(state['terminated']) is not bool:
        raise StreamingError('terminated must be boolean')
    steps = state['steps']
    if steps != length + _tail(constraint, state['terminated']):
        raise StreamingError('step count disagrees with payload description')
    width = state['width']
    if width != len(generators) or state['symbol_count'] != steps * width:
        raise StreamingError('symbol layout is inconsistent')
    received = integer(state['received_count'], 0, state['symbol_count'])
    confirmed = integer(state['confirmed'], 0, received)
    delivered = integer(state['delivered'], 0, min(confirmed // width,
                                                   length))
    if type(state['done']) is not bool:
        raise StreamingError('done must be boolean')
    if state['evidence'] is not None and not state['done']:
        raise StreamingError('unfinished state cannot carry evidence')
    bits = state['delivered_bits']
    if not isinstance(bits, list) or len(bits) != delivered:
        raise StreamingError('delivered bits disagree with delivered length')
    for bit in bits:
        if type(bit) is not int or bit not in (0, 1):
            raise StreamingError('delivered bits must be strict 0 or 1')
    retired = state['retired']
    if not isinstance(retired, list) or len(retired) != delivered:
        raise StreamingError('retired steps disagree with delivered length')
    for step_index, block in enumerate(retired):
        fields(block, ('step', 'digest', 'status', 'cost'))
        if block['step'] != step_index:
            raise StreamingError('retired steps must be contiguous from zero')
        status = block['status']
        if not isinstance(status, list) or len(status) != width:
            raise StreamingError('retired provenance has the wrong width')
        if any(item not in STATUS_VALUES for item in status):
            raise StreamingError('invalid retired provenance status')
        if not isinstance(block['digest'], str):
            raise StreamingError('retired digest must be a string')
        cost = integer(block['cost'], 0, width)
        if cost != sum(item == 'mismatch' for item in status):
            raise StreamingError('retired cost disagrees with provenance')
    pending = state['pending']
    if not isinstance(pending, dict):
        raise StreamingError('pending observations must be an object')
    base = delivered * width
    leftover = received - base
    if leftover < 0:
        raise StreamingError('delivered bits exceed the received prefix')
    expected = {}
    remaining = leftover
    for step_index in range(delivered, steps):
        size = min(width, remaining)
        if size > 0:
            expected[str(step_index)] = size
        remaining -= size
    if remaining > 0:
        raise StreamingError('received length exceeds the fixed total')
    if set(pending) != set(expected):
        raise StreamingError('pending blocks do not match the frontier')
    for key, size in expected.items():
        block = pending[key]
        if not isinstance(block, list) or len(block) != size:
            raise StreamingError('pending block has an invalid size')
        for symbol in block:
            _strict_symbol(symbol)
    if state['done'] and received != state['symbol_count']:
        raise StreamingError('finished state requires full receipt')
    return state
