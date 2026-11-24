"""Reference-time alignment of already prepared local metering facts.

A metering fact says how much one bench end measured over one half-open local
tick interval.  A separate calibration job supplies, per end, 0..4096 anchors
binding local ticks to integer microsecond reference times.  The same prepared
facts can be aligned repeatedly against different calibration catalogues: this
module never takes frame evidence and never rewrites local facts.

Calibration geometry
--------------------
Anchors are sorted by tick; both tick and reference columns must be strictly
increasing.  Each anchor carries an ``interpolate`` flag: a true flag enables
the linear segment from the previous anchor to this one, a false flag disables
it (the first anchor's flag must be false).  Enabled segments map their
half-open local range linearly; there is no interpolation across a disabled
line, no extrapolation beyond anchor coverage, and a single anchor has no
length coverage.  Every boundary computation uses exact :class:`Fraction`
arithmetic.

Allocation
----------
A known local increment is attributed by local length over its *original*
local interval.  That interval is first cut into local atoms by the enabled
calibration segments: calibrated atoms carry a reference interval, the rest
stay local-only.  Atoms from the two ends are then overlaid in reference time
with a merge sweep; every non-empty reference intersection becomes one paired
downstream fact, and endpoint contact produces nothing.  Calibrated atoms that
never intersect the other end become ``calibrated_unpaired`` shares (never
interpreted as zero flow on the other side); atoms without calibration become
``uncalibrated`` shares.  Unknown facts are never apportioned.

For every known fact the paired and unpaired local sub-ranges cover the
original interval exactly once and the exact rational shares sum to the
original increment, including genuine zero increments.  Work grows with the
input breakpoints and emitted records only; no tick or microsecond is ever
expanded and there is no cartesian product of intervals.
"""
from fractions import Fraction

from .checks import array, fields, ident
from .metering import (
    TICK_MAX,
    MeteringError,
    rational,
    validate_facts,
)

ALIGNMENT_KIND = 'telemetry_trellis.alignment'
ALIGNMENT_VERSION = 1
MAX_ANCHORS = 4096
_SIDE_INLET = 'inlet'
_SIDE_OUTLET = 'outlet'


class AlignmentError(MeteringError):
    """Raised for malformed calibration input or violated alignment bounds."""


# ------------------------------------------------------------------ validation
def _strict_int(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise AlignmentError(f'{label} must be an integer in {low}..{high}')
    return value


def _validate_anchor_list(raw, side, meter_id):
    anchors = array(raw, 0, MAX_ANCHORS)
    previous_tick = None
    previous_ref = None
    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, dict):
            raise AlignmentError(f'{side} anchor must be an object')
        fields(anchor, ('tick', 'reference_time', 'interpolate'))
        tick = _strict_int(anchor['tick'], 0, TICK_MAX,
                           f'{side} anchor tick')
        ref = _strict_int(anchor['reference_time'], 0, TICK_MAX,
                          f'{side} anchor reference_time')
        if type(anchor['interpolate']) is not bool:
            raise AlignmentError(f'{side} anchor interpolate must be boolean')
        if index == 0 and anchor['interpolate']:
            raise AlignmentError(
                f'{side} first anchor must not enable interpolation')
        if previous_tick is not None:
            if tick <= previous_tick:
                raise AlignmentError(
                    f'{side} anchor ticks must be strictly increasing')
            if ref <= previous_ref:
                raise AlignmentError(
                    f'{side} anchor reference times must be strictly '
                    'increasing')
        previous_tick = tick
        previous_ref = ref
    return [{'tick': a['tick'], 'reference_time': a['reference_time'],
             'interpolate': a['interpolate']} for a in anchors]


def _validate_timing(timing, facts):
    if not isinstance(timing, dict):
        raise AlignmentError('timing catalogue must be an object')
    fields(timing, ('inlet_meter_id', 'outlet_meter_id',
                    'inlet_anchors', 'outlet_anchors'))
    if ident(timing['inlet_meter_id']) != facts['inlet_meter_id']:
        raise AlignmentError('inlet calibration binds a different meter')
    if ident(timing['outlet_meter_id']) != facts['outlet_meter_id']:
        raise AlignmentError('outlet calibration binds a different meter')
    anchors = {
        _SIDE_INLET: _validate_anchor_list(
            timing['inlet_anchors'], _SIDE_INLET,
            timing['inlet_meter_id']),
        _SIDE_OUTLET: _validate_anchor_list(
            timing['outlet_anchors'], _SIDE_OUTLET,
            timing['outlet_meter_id']),
    }
    return anchors


# ------------------------------------------------------------- calibration map
def _build_segments(anchors, side, meter_id):
    """Enabled half-open linear segments from a validated anchor list."""
    segments = []
    for index in range(1, len(anchors)):
        anchor = anchors[index]
        if not anchor['interpolate']:
            continue
        previous = anchors[index - 1]
        tick_span = anchor['tick'] - previous['tick']
        ref_span = anchor['reference_time'] - previous['reference_time']
        segment = {
            'side': side,
            'meter_id': meter_id,
            'segment_index': len(segments),
            'anchor_index': index,
            'start_tick': previous['tick'],
            'end_tick': anchor['tick'],
            'start_reference': rational(Fraction(previous['reference_time'])),
            'end_reference': rational(Fraction(anchor['reference_time'])),
            'slope_ref_per_tick': Fraction(ref_span, tick_span),
        }
        segments.append(segment)
    return segments


def _map_to_reference(segment, tick):
    base = Fraction(segment['start_tick'])
    return (Fraction(segment['start_reference']['num'],
                     segment['start_reference']['den'])
            + segment['slope_ref_per_tick'] * (Fraction(tick) - base))


def _map_to_tick(segment, ref):
    start_ref = Fraction(segment['start_reference']['num'],
                         segment['start_reference']['den'])
    start_tick = Fraction(segment['start_tick'])
    span_tick = Fraction(segment['end_tick'] - segment['start_tick'])
    span_ref = (Fraction(segment['end_reference']['num'],
                         segment['end_reference']['den']) - start_ref)
    return start_tick + span_tick * (ref - start_ref) / span_ref


def _public_segment(segment):
    return {key: segment[key] for key in (
        'side', 'meter_id', 'segment_index', 'anchor_index',
        'start_tick', 'end_tick', 'start_reference', 'end_reference')}


def _pieces_for_fact(fact, segments):
    """Partition one known fact's local interval into calibration atoms.

    Calibrated pieces carry the owning segment and exact reference range;
    uncalibrated pieces stay purely local.  Enabled segments may be disjoint
    (a disabled line creates a gap), so every segment that overlaps the fact
    range is visited in order; the gaps between them -- and the head/tail
    outside all coverage -- become uncalibrated pieces.  Piece local ranges
    partition the original half-open interval without overlap.
    """
    lo = fact['local_range']['start']
    hi = fact['local_range']['end']
    pieces = []
    cursor = lo
    # Segments are disjoint and tick-ordered; locate the first one whose
    # half-open range can still contain a position inside the fact.
    start_index = 0
    while start_index < len(segments) \
            and segments[start_index]['end_tick'] <= lo:
        start_index += 1
    for index in range(start_index, len(segments)):
        segment = segments[index]
        if segment['start_tick'] >= hi:
            break
        overlap_start = max(lo, segment['start_tick'])
        overlap_end = min(hi, segment['end_tick'])
        if overlap_start >= overlap_end:
            continue
        if cursor < overlap_start:
            pieces.append({'calibrated': False, 'segment': None,
                           'local_start': Fraction(cursor),
                           'local_end': Fraction(overlap_start),
                           'ref_start': None, 'ref_end': None})
        pieces.append({
            'calibrated': True,
            'segment': segment,
            'local_start': Fraction(overlap_start),
            'local_end': Fraction(overlap_end),
            'ref_start': _map_to_reference(segment, overlap_start),
            'ref_end': _map_to_reference(segment, overlap_end),
        })
        cursor = overlap_end
    if cursor < hi:
        pieces.append({'calibrated': False, 'segment': None,
                       'local_start': Fraction(cursor),
                       'local_end': Fraction(hi),
                       'ref_start': None, 'ref_end': None})
    return pieces


def _build_atoms(facts, segments_by_side):
    """All calibrated atoms per side, each bound to its originating fact."""
    atoms = {_SIDE_INLET: [], _SIDE_OUTLET: []}
    pieces_by_fact = {}
    for fact in facts:
        if fact['status'] != 'known':
            continue
        side = fact['side']
        pieces = _pieces_for_fact(
            fact, segments_by_side[side])
        pieces_by_fact[fact['fact_id']] = pieces
        length = Fraction(fact['local_range']['end']
                          - fact['local_range']['start'])
        increment = Fraction(fact['increment_ml'])
        for piece in pieces:
            piece['fact_id'] = fact['fact_id']
            piece['side'] = side
            piece['meter_id'] = fact['meter_id']
            piece['fact_length'] = length
            piece['increment'] = increment
            piece['endpoint_frame_ids'] = dict(fact['endpoint_frame_ids'])
            if piece['calibrated']:
                atoms[side].append(piece)
    for side in atoms:
        atoms[side].sort(key=lambda piece: (
            piece['ref_start'], piece['ref_end'], piece['fact_id'],
            piece['local_start']))
    return atoms, pieces_by_fact


# -------------------------------------------------------------------- sweeps
def _pair_sweep(inlet_atoms, outlet_atoms):
    """Merge-sweep calibrated atoms in reference time.

    Returns pairs with their exact reference intersection.  Contact only
    (one range ending where the other starts) yields no pair.
    """
    i = j = 0
    pairs = []
    while i < len(inlet_atoms) and j < len(outlet_atoms):
        left = inlet_atoms[i]
        right = outlet_atoms[j]
        if left['ref_end'] <= right['ref_start']:
            i += 1
            continue
        if right['ref_end'] <= left['ref_start']:
            j += 1
            continue
        ref_start = max(left['ref_start'], right['ref_start'])
        ref_end = min(left['ref_end'], right['ref_end'])
        pairs.append((left, right, ref_start, ref_end))
        if left['ref_end'] == ref_end:
            i += 1
        if right['ref_end'] == ref_end:
            j += 1
    return pairs


def _uncovered_parts(atom, pairs):
    """Reference sub-ranges of one atom not covered by any intersecting pair."""
    cursor = atom['ref_start']
    parts = []
    for pair in pairs:
        ref_start, ref_end = pair
        if ref_end <= atom['ref_start']:
            continue
        if ref_start >= atom['ref_end']:
            break
        if ref_start > cursor:
            parts.append((cursor, min(ref_start, atom['ref_end'])))
        if ref_end > cursor:
            cursor = min(ref_end, atom['ref_end'])
    if cursor < atom['ref_end']:
        parts.append((cursor, atom['ref_end']))
    return parts


def _share(piece, local_start, local_end):
    length = local_end - local_start
    return piece['increment'] * length / piece['fact_length']


def _local_view(piece, ref_start, ref_end):
    """Inverse-map a reference sub-range back onto the piece's local ticks."""
    segment = piece['segment']
    return (_map_to_tick(segment, ref_start),
            _map_to_tick(segment, ref_end))


def _paired_records(pairs):
    ordered = sorted(pairs, key=lambda item: (
        item[2], item[3], item[0]['fact_id'], item[1]['fact_id'],
        item[0]['local_start'], item[1]['local_start']))
    records = []
    for number, (inlet, outlet, ref_start, ref_end) in enumerate(ordered, 1):
        in_start, in_end = _local_view(inlet, ref_start, ref_end)
        out_start, out_end = _local_view(outlet, ref_start, ref_end)
        records.append({
            'record_id': f'paired-{number:04d}',
            'type': 'paired',
            'reference_range': {
                'start': rational(ref_start),
                'end': rational(ref_end),
            },
            'inlet': {
                'fact_id': inlet['fact_id'],
                'meter_id': inlet['meter_id'],
                'endpoint_frame_ids': inlet['endpoint_frame_ids'],
                'anchor_segment': _public_segment(inlet['segment']),
                'local_range': {'start': rational(in_start),
                                'end': rational(in_end)},
                'share_ml': rational(_share(inlet, in_start, in_end)),
            },
            'outlet': {
                'fact_id': outlet['fact_id'],
                'meter_id': outlet['meter_id'],
                'endpoint_frame_ids': outlet['endpoint_frame_ids'],
                'anchor_segment': _public_segment(outlet['segment']),
                'local_range': {'start': rational(out_start),
                                'end': rational(out_end)},
                'share_ml': rational(_share(outlet, out_start, out_end)),
            },
        })
    return records, ordered


def _unpaired_records(atoms, ordered_pairs):
    """Split calibrated atoms into paired cover and calibrated-unpaired parts."""
    pair_ranges = [(ref_start, ref_end) for _, _, ref_start, ref_end
                   in ordered_pairs]
    leftovers = []
    for atom in atoms:
        for ref_start, ref_end in _uncovered_parts(atom, pair_ranges):
            local_start, local_end = _local_view(atom, ref_start, ref_end)
            leftovers.append({
                'sort_key': (atom['side'], atom['fact_id'], local_start),
                'record': {
                    'type': 'calibrated_unpaired',
                    'side': atom['side'],
                    'meter_id': atom['meter_id'],
                    'fact_id': atom['fact_id'],
                    'endpoint_frame_ids': atom['endpoint_frame_ids'],
                    'anchor_segment': _public_segment(atom['segment']),
                    'reference_range': {'start': rational(ref_start),
                                        'end': rational(ref_end)},
                    'local_range': {'start': rational(local_start),
                                    'end': rational(local_end)},
                    'share_ml': rational(
                        _share(atom, local_start, local_end)),
                },
            })
    leftovers.sort(key=lambda item: item['sort_key'])
    return [item['record'] for item in leftovers]


def _uncalibrated_records(pieces_by_fact):
    entries = []
    for fact_id, pieces in pieces_by_fact.items():
        for piece in pieces:
            if piece['calibrated']:
                continue
            entries.append({
                'sort_key': (piece['side'], piece['fact_id'],
                             piece['local_start']),
                'piece': piece,
            })
    entries.sort(key=lambda item: item['sort_key'])
    records = []
    for number, entry in enumerate(entries, 1):
        piece = entry['piece']
        records.append({
            'record_id': f'unpaired-{number:04d}',
            'type': 'uncalibrated',
            'side': piece['side'],
            'meter_id': piece['meter_id'],
            'fact_id': piece['fact_id'],
            'endpoint_frame_ids': piece['endpoint_frame_ids'],
            'anchor_segment': None,
            'reference_range': None,
            'local_range': {'start': rational(piece['local_start']),
                            'end': rational(piece['local_end'])},
            'share_ml': rational(_share(
                piece, piece['local_start'], piece['local_end'])),
        })
    return records


def _verify_coverage(facts, pieces_by_fact, ordered_pairs):
    """Defensive exactness check: one local cover and full share conservation."""
    covered = {fact_id: [Fraction(0) for _ in pieces]
               for fact_id, pieces in pieces_by_fact.items()}
    piece_index = {id(piece): (fact_id, index)
                   for fact_id, pieces in pieces_by_fact.items()
                   for index, piece in enumerate(pieces)}

    def add(piece, local_start, local_end):
        fact_id, index = piece_index[id(piece)]
        covered[fact_id][index] += local_end - local_start

    for inlet, outlet, ref_start, ref_end in ordered_pairs:
        add(inlet, *_local_view(inlet, ref_start, ref_end))
        add(outlet, *_local_view(outlet, ref_start, ref_end))
    # Uncalibrated pieces and the uncovered remainder of calibrated pieces
    # account for every local atom that did not enter a pair.
    pair_ranges = [(rs, re) for _, _, rs, re in ordered_pairs]
    for fact_id, pieces in pieces_by_fact.items():
        for piece in pieces:
            if not piece['calibrated']:
                add(piece, piece['local_start'], piece['local_end'])
                continue
            for rs, re in _uncovered_parts(piece, pair_ranges):
                add(piece, *_local_view(piece, rs, re))
    for fact in facts:
        if fact['status'] != 'known':
            continue
        pieces = pieces_by_fact[fact['fact_id']]
        expected_length = Fraction(fact['local_range']['end']
                                   - fact['local_range']['start'])
        if sum(covered[fact['fact_id']]) != expected_length:
            raise AlignmentError(
                f"internal coverage broken for fact {fact['fact_id']}")
        total_share = sum(
            fact['increment_ml'] * length / expected_length
            for length in covered[fact['fact_id']])
        if total_share != Fraction(fact['increment_ml']):
            raise AlignmentError(
                f"share conservation broken for fact {fact['fact_id']}")


# ------------------------------------------------------------------- public API
def align_meter_facts(facts, timing):
    """Align prepared local facts onto shared reference time.

    ``facts`` is a document returned by
    :func:`telemetry_trellis.metering.build_meter_facts` (or an equivalent
    reloaded JSON document); ``timing`` binds each meter ID to 0..4096 anchors.
    The same facts can be realigned against different timing catalogues; the
    facts document is never modified.  Malformed calibration raises
    :class:`AlignmentError` before any record is produced.  Legal missing
    evidence, ambiguity and absent calibration coverage are data, not errors.

    Output ordering is deterministic: paired records by reference range then
    source identities; unpaired records by side (inlet first), fact and local
    start.  Every rational boundary/share is emitted as ``{'num', 'den'}``.
    """
    document = validate_facts(facts)
    try:
        anchors = _validate_timing(timing, document)
        segments = {
            _SIDE_INLET: _build_segments(
                anchors[_SIDE_INLET], _SIDE_INLET,
                document['inlet_meter_id']),
            _SIDE_OUTLET: _build_segments(
                anchors[_SIDE_OUTLET], _SIDE_OUTLET,
                document['outlet_meter_id']),
        }
        atoms, pieces_by_fact = _build_atoms(document['facts'], segments)
        pairs = _pair_sweep(atoms[_SIDE_INLET], atoms[_SIDE_OUTLET])
        paired_records, ordered_pairs = _paired_records(pairs)

        calibrated_unpaired = []
        for side in (_SIDE_INLET, _SIDE_OUTLET):
            calibrated_unpaired.extend(
                _unpaired_records(atoms[side], ordered_pairs))
        calibrated_unpaired.sort(key=lambda record: (
            record['side'], record['fact_id'],
            Fraction(record['local_range']['start']['num'],
                     record['local_range']['start']['den'])))
        uncalibrated = _uncalibrated_records(pieces_by_fact)
        unpaired_records = calibrated_unpaired + uncalibrated
        for number, record in enumerate(unpaired_records, 1):
            record['record_id'] = f'unpaired-{number:04d}'

        _verify_coverage(document['facts'], pieces_by_fact, ordered_pairs)
    except AlignmentError:
        raise
    except ValueError as error:
        raise AlignmentError(str(error)) from error

    unknown_facts = [{
        'fact_id': fact['fact_id'],
        'side': fact['side'],
        'meter_id': fact['meter_id'],
        'local_range': dict(fact['local_range']),
        'endpoint_frame_ids': dict(fact['endpoint_frame_ids']),
        'unknown_reason': fact['unknown_reason'],
    } for fact in document['facts'] if fact['status'] == 'unknown']

    return {
        'kind': ALIGNMENT_KIND,
        'version': ALIGNMENT_VERSION,
        'inlet_meter_id': document['inlet_meter_id'],
        'outlet_meter_id': document['outlet_meter_id'],
        'anchor_segments': {
            side: [_public_segment(segment) for segment in segments[side]]
            for side in (_SIDE_INLET, _SIDE_OUTLET)
        },
        'paired': paired_records,
        'unpaired': unpaired_records,
        'unknown_facts': unknown_facts,
    }
