"""Minimum-Hamming merge of several independently received copies of one frame.

The single-copy :func:`telemetry_trellis.decode.decode` scores one received
array against every legal trellis path.  The archive keeps several receiving
identities per frame (some of them revoked); this module scores each candidate
path against every still-valid copy at once.

A candidate path pays, for every copy, the number of *non-erased* observed
symbols that disagree with its re-encoded word.  Two copies holding different
hard bits at the same index are therefore both counted against any path: there
is deliberately no per-index voting step that would first collapse the copies
into one synthetic received array.

The Viterbi sweep keeps, per reachable state, the minimum metric, the
lexicographically smallest surviving bit prefix and the exact number of paths
attaining that metric.  Payloads of up to 500 bits are handled without
enumerating payloads; path counts are plain Python integers and never overflow.
"""
from .code import encode, transition


def _word_metric(word, copies, offset):
    """Mismatches of one output word against every copy at ``offset``.

    ``word`` holds only the output symbols produced at this trellis step;
    ``offset`` is the word's absolute position inside each expanded received
    array, so the copy must be indexed at ``offset + position``.
    """
    total = 0
    for received in copies:
        for position, expected in enumerate(word):
            observed = received[offset + position]
            if observed is not None and observed != expected:
                total += 1
    return total


def merge_decode(frame_desc, copies):
    """Decode a frame from its currently valid received copies.

    ``frame_desc`` is ``{'constraint', 'generators', 'payload_length',
    'terminated'}``; ``copies`` is a list of received arrays that already
    passed the single-frame length/symbol/erasure rules.  With no valid copy
    the frame is scored as if every symbol were erased.

    Returns a dict with ``distance``, ``optimal_paths``, ``ambiguous``,
    ``payload`` (lexicographic representative), ``selected_final_state``,
    ``reencoded`` and ``state_path``.
    """
    constraint = frame_desc['constraint']
    generators = frame_desc['generators']
    width = len(generators)
    length = frame_desc['length']
    terminated = frame_desc['terminated']
    if copies:
        received_len = len(copies[0])
    else:
        steps = length + (constraint - 1 if terminated else 0)
        received_len = steps * width
    # state -> (metric, integer bit prefix, exact path count).  Prefixes are
    # stored as integers with the first source bit in the highest position, so
    # numeric order equals lexicographic order at equal prefix lengths.
    paths = {0: (0, 0, 1)}
    for step, offset in enumerate(range(0, received_len, width)):
        # Group every incoming transition by target state, keep only the
        # minimum metric per target, then sum all source paths attaining it.
        # Grouping first matters: a later transition may beat equal-metric
        # ones already merged, and equal metrics from different sources must
        # add even when the target was first seen at another metric.
        grouped = {}
        shift = received_len // width - step - 1
        for state, (cost, prefix, count) in paths.items():
            for bit in (0, 1):
                target, word = transition(state, bit, constraint, generators)
                # Two copies holding conflicting hard bits at this window
                # both contribute to the cost; nothing is voted into one
                # synthetic hard-decision input first.
                metric = cost + _word_metric(word, copies, offset)
                candidate = prefix | (bit << shift)
                grouped.setdefault(target, []).append(
                    (metric, candidate, count))
        next_paths = {}
        for target, candidates in grouped.items():
            best_metric = min(metric for metric, _, _ in candidates)
            winners = [(candidate, count)
                       for metric, candidate, count in candidates
                       if metric == best_metric]
            representative = min(candidate for candidate, _ in winners)
            total = sum(count for _, count in winners)
            next_paths[target] = (best_metric, representative, total)
        paths = next_paths
    eligible = {0: paths[0]} if terminated else paths
    best = min(value[0] for value in eligible.values())
    winners = [(state, value) for state, value in eligible.items()
               if value[0] == best]
    state, winner = min(winners, key=lambda pair: pair[1][1])
    metric, packed, _ = winner
    optimal_paths = sum(value[2] for _, value in winners)
    total_steps = received_len // width
    bits = tuple((packed >> (total_steps - 1 - index)) & 1
                 for index in range(total_steps))
    payload = list(bits[:length])
    reference = encode(payload, constraint, generators, terminated)
    return {
        'distance': metric,
        'optimal_paths': optimal_paths,
        'ambiguous': optimal_paths != 1,
        'payload': payload,
        'selected_final_state': state,
        'reencoded': reference['symbols'],
        'state_path': reference['states'],
    }


def copy_discrepancies(received, reencoded):
    """Mismatched (known, wrong) and erased expanded symbol indices."""
    mismatches = []
    erasures = []
    for index, (observed, expected) in enumerate(zip(received, reencoded)):
        if observed is None:
            erasures.append(index)
        elif observed != expected:
            mismatches.append(index)
    return mismatches, erasures
