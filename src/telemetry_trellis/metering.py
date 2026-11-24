"""Local metering facts derived from already decoded frame evidence.

The offline bench has one cumulative flow meter at the inlet and one at the
outlet.  An upstream job owns frame decoding: this module never sees raw
symbols and never calls a decoder.  Its inputs are a trusted *point catalogue*
(the two meter IDs, each point's local tick, frame ID and a frozen evidence
digest) and single-frame result objects shaped like
:func:`telemetry_trellis.process` ``...["frames"][k]`` (possibly gathered from
several calls).  Only ``id``, ``payload``, ``optimal_paths`` and ``ambiguous``
are consumed; other diagnostic fields are not re-verified.

For every pair of points that are actually adjacent after sorting one meter by
tick, the module emits one local metering fact: a half-open local range, the
endpoint frame identities, endpoint availability and either an exact
increment or a structured unknown reason.  Facts are plain JSON data with a
versioned document kind and can be delivered without any time alignment; the
alignment API consumes the same prepared facts repeatedly against different
calibration catalogues and never mutates them.

Only the Python standard library is used.
"""
import hashlib
import json
import re

from .checks import array, fields, ident, unique

FACTS_KIND = 'telemetry_trellis.meter_facts'
FACTS_VERSION = 1
MAX_POINTS_PER_METER = 2048
TICK_MAX = 10 ** 15
MAX_PAYLOAD_BITS = 64
_DIGEST_RE = re.compile(r'[0-9a-f]{64}')
_REASON_MISSING = 'missing_evidence'
_REASON_AMBIGUOUS = 'ambiguous_evidence'
_READABLE = 'readable'


class MeteringError(ValueError):
    """Raised when a catalogue/evidence batch is malformed or tampered with."""


# --------------------------------------------------------------- JSON rationals
def rational(value):
    """Normalize any exact rational input to ``{'num', 'den'}`` JSON form."""
    num = value.numerator
    den = value.denominator
    return {'num': num, 'den': den}


def _strict_int(value, low, high, label):
    # bool is a subclass of int; ticks, counts and path counts reject it.
    if type(value) is not int or not low <= value <= high:
        raise MeteringError(f'{label} must be an integer in {low}..{high}')
    return value


def _strict_bool(value, label):
    if type(value) is not bool:
        raise MeteringError(f'{label} must be a boolean')
    return value


# ------------------------------------------------------------------ validation
def _validate_catalogue(catalog):
    if not isinstance(catalog, dict):
        raise MeteringError('catalogue must be an object')
    fields(catalog, ('inlet_meter_id', 'outlet_meter_id', 'points'))
    inlet_id = ident(catalog['inlet_meter_id'])
    outlet_id = ident(catalog['outlet_meter_id'])
    if inlet_id == outlet_id:
        raise MeteringError('inlet and outlet meter IDs must differ')
    raw_points = array(catalog['points'], 4, 2 * MAX_POINTS_PER_METER)
    points = []
    frame_ids = []
    grouped = {inlet_id: [], outlet_id: []}
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise MeteringError('each catalogue point must be an object')
        fields(raw, ('meter_id', 'tick', 'frame_id', 'evidence_digest'))
        meter_id = ident(raw['meter_id'])
        if meter_id not in grouped:
            raise MeteringError('point meter ID is neither inlet nor outlet')
        tick = _strict_int(raw['tick'], 0, TICK_MAX, 'tick')
        frame_id = ident(raw['frame_id'])
        digest = raw['evidence_digest']
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise MeteringError('evidence_digest must be 64 lowercase hex chars')
        point = {'meter_id': meter_id, 'tick': tick,
                 'frame_id': frame_id, 'evidence_digest': digest}
        points.append(point)
        grouped[meter_id].append(point)
        frame_ids.append(frame_id)
    for meter_id, own in grouped.items():
        if not 2 <= len(own) <= MAX_POINTS_PER_METER:
            raise MeteringError(
                f'meter {meter_id} needs 2..{MAX_POINTS_PER_METER} points')
        ticks = [point['tick'] for point in own]
        if len(set(ticks)) != len(ticks):
            raise MeteringError(f'duplicate tick on meter {meter_id}')
    # Frame IDs are batch-unique: one frame result is bound to one point.
    unique(frame_ids)
    return inlet_id, outlet_id, points, grouped


def _validate_frame_evidence(frame):
    if not isinstance(frame, dict):
        raise MeteringError('each frame evidence entry must be an object')
    for required in ('id', 'payload', 'optimal_paths', 'ambiguous'):
        if required not in frame:
            raise MeteringError(f'frame evidence missing field: {required}')
    frame_id = ident(frame['id'])
    payload = frame['payload']
    if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_PAYLOAD_BITS:
        raise MeteringError(
            f'payload must be a list of 1..{MAX_PAYLOAD_BITS} bits')
    for bit in payload:
        # Strict 0/1 integers: booleans are rejected even though int subclass.
        if type(bit) is not int or bit not in (0, 1):
            raise MeteringError('payload bits must be strict integers 0 or 1')
    path_limit = 1 << len(payload)
    path_count = frame['optimal_paths']
    _strict_int(path_count, 1, path_limit, 'optimal_paths')
    ambiguous = _strict_bool(frame['ambiguous'], 'ambiguous')
    if ambiguous != (path_count != 1):
        raise MeteringError(
            'ambiguous must equal (optimal_paths != 1)')
    return frame_id, payload, path_count, ambiguous


def _digest_for(meter_id, tick, frame_id, payload, path_count, ambiguous):
    envelope = [meter_id, tick, frame_id, list(payload), path_count, ambiguous]
    text = json.dumps(envelope, ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _index_evidence(evidence, point_ids):
    if not isinstance(evidence, list):
        raise MeteringError('frame evidence must be a list')
    indexed = {}
    for frame in evidence:
        frame_id, payload, path_count, ambiguous = _validate_frame_evidence(frame)
        if frame_id in indexed:
            raise MeteringError(f'duplicate frame evidence: {frame_id}')
        if frame_id not in point_ids:
            # Stale results mixed into the batch are rejected, never silently
            # treated as "evidence absent".
            raise MeteringError(f'unknown frame evidence: {frame_id}')
        indexed[frame_id] = {
            'payload': list(payload),
            'optimal_paths': path_count,
            'ambiguous': ambiguous,
        }
    return indexed


def _reading(payload):
    # MSB-first unsigned cumulative millilitre reading.
    total = 0
    for bit in payload:
        total = (total << 1) | bit
    return total


def _endpoint(point, evidence):
    frame = evidence.get(point['frame_id'])
    if frame is None:
        return {'status': _REASON_MISSING, 'frame_id': point['frame_id'],
                'reading': None}
    if frame['ambiguous']:
        return {'status': _REASON_AMBIGUOUS, 'frame_id': point['frame_id'],
                'reading': None}
    return {'status': _READABLE, 'frame_id': point['frame_id'],
            'reading': _reading(frame['payload'])}


def _unknown_reason(start, end):
    if start['status'] != _READABLE:
        return start['status']
    if end['status'] != _READABLE:
        return end['status']
    return 'decreasing_reading'


def _facts_for_meter(side, meter_id, grouped, evidence):
    ordered = sorted(grouped[meter_id], key=lambda point: point['tick'])
    facts = []
    for index in range(len(ordered) - 1):
        start_point, end_point = ordered[index], ordered[index + 1]
        start = _endpoint(start_point, evidence)
        end = _endpoint(end_point, evidence)
        fact = {
            'fact_id': f'{side}-fact-{index + 1}',
            'side': side,
            'meter_id': meter_id,
            'local_range': {'start': start_point['tick'],
                            'end': end_point['tick']},
            'endpoint_frame_ids': {'start': start_point['frame_id'],
                                   'end': end_point['frame_id']},
            'endpoints': {'start': start, 'end': end},
        }
        if (start['status'] == _READABLE and end['status'] == _READABLE
                and end['reading'] >= start['reading']):
            fact['status'] = 'known'
            fact['increment_ml'] = end['reading'] - start['reading']
            fact['unknown_reason'] = None
        else:
            fact['status'] = 'unknown'
            fact['increment_ml'] = None
            fact['unknown_reason'] = _unknown_reason(start, end)
        facts.append(fact)
    return facts


# ------------------------------------------------------------------- public API
def build_meter_facts(catalog, evidence):
    """Verify a catalogue/evidence batch and derive local metering facts.

    ``catalog`` is ``{'inlet_meter_id', 'outlet_meter_id', 'points'}`` where
    each point fixes ``meter_id``, ``tick`` (0..10**15, strict integer),
    ``frame_id`` (batch-unique) and the upstream ``evidence_digest``.
    ``evidence`` is a list of single-frame result objects; only ``id``,
    ``payload``, ``optimal_paths`` and ``ambiguous`` are consumed.

    The digest is the SHA-256 lowercase hex of the compact JSON serialization
    of ``[meter_id, tick, frame_id, payload, optimal_paths, ambiguous]``.
    Unknown/duplicate frames, illegal consumed fields or any digest mismatch
    reject the whole batch with :class:`MeteringError`; a point whose frame is
    simply absent from the evidence list is a legal *missing evidence*
    endpoint, not a format error.

    Returns a versioned, JSON-only document whose ``facts`` are ordered by
    side (inlet then outlet) and, within each meter, by tick.
    """
    try:
        inlet_id, outlet_id, points, grouped = _validate_catalogue(catalog)
        point_ids = {point['frame_id'] for point in points}
        indexed = _index_evidence(evidence, point_ids)
        # Digest verification is the tamper boundary: every supplied evidence
        # object must match the frozen catalogue binding exactly.
        for point in points:
            frame = indexed.get(point['frame_id'])
            if frame is None:
                continue
            actual = _digest_for(point['meter_id'], point['tick'],
                                 point['frame_id'], frame['payload'],
                                 frame['optimal_paths'], frame['ambiguous'])
            if actual != point['evidence_digest']:
                raise MeteringError(
                    f"evidence digest mismatch for frame {point['frame_id']}")
        inlet_facts = _facts_for_meter('inlet', inlet_id, grouped, indexed)
        outlet_facts = _facts_for_meter('outlet', outlet_id, grouped, indexed)
    except MeteringError:
        raise
    except ValueError as error:
        # Every format violation must surface as the documented error type.
        raise MeteringError(str(error)) from error
    return {
        'kind': FACTS_KIND,
        'version': FACTS_VERSION,
        'inlet_meter_id': inlet_id,
        'outlet_meter_id': outlet_id,
        'facts': inlet_facts + outlet_facts,
    }


def evidence_digest(meter_id, tick, frame_id, frame):
    """Compute the catalogue digest for one point/frame binding.

    This helper lets an example (or the simulated upstream publisher) produce
    exactly the digest the verifier expects from a public frame result.
    """
    _strict_int(tick, 0, TICK_MAX, 'tick')
    ident(meter_id)
    ident(frame_id)
    _, payload, path_count, ambiguous = _validate_frame_evidence(
        {**frame, 'id': frame_id})
    return _digest_for(meter_id, tick, frame_id, payload, path_count,
                       ambiguous)


# ------------------------------------------------------------- facts reloading
def validate_facts(document):
    """Structurally validate a prepared facts document (including reloaded JSON).

    Returns the document unchanged.  This binds the alignment entry point:
    alignment never re-derives or rewrites local facts, it only reads them.
    """
    if not isinstance(document, dict) or document.get('kind') != FACTS_KIND:
        raise MeteringError('not a meter facts document')
    if document.get('version') != FACTS_VERSION:
        raise MeteringError('unsupported meter facts version')
    try:
        fields(document, ('kind', 'version', 'inlet_meter_id',
                          'outlet_meter_id', 'facts'))
        inlet_id = ident(document['inlet_meter_id'])
        outlet_id = ident(document['outlet_meter_id'])
        if inlet_id == outlet_id:
            raise MeteringError('inlet and outlet meter IDs must differ')
        raw_facts = array(document['facts'], 2,
                          2 * (MAX_POINTS_PER_METER - 1))
        expected_side = {'inlet': inlet_id, 'outlet': outlet_id}
        valid_reasons = {_REASON_MISSING, _REASON_AMBIGUOUS,
                         'decreasing_reading'}
        seen_sides = []
        last_key = None
        for fact in raw_facts:
            fields(fact, ('fact_id', 'side', 'meter_id', 'local_range',
                          'endpoint_frame_ids', 'endpoints', 'status',
                          'increment_ml', 'unknown_reason'))
            side = fact['side']
            if (side not in expected_side
                    or fact['meter_id'] != expected_side[side]):
                raise MeteringError(
                    'fact does not bind its declared meter/side')
            ident(fact['fact_id'])
            fields(fact['endpoint_frame_ids'], ('start', 'end'))
            ident(fact['endpoint_frame_ids']['start'])
            ident(fact['endpoint_frame_ids']['end'])
            local = fact['local_range']
            fields(local, ('start', 'end'))
            _strict_int(local['start'], 0, TICK_MAX, 'local range start')
            _strict_int(local['end'], 0, TICK_MAX, 'local range end')
            if not local['start'] < local['end']:
                raise MeteringError(
                    'fact local range must be a non-empty half open')
            key = (side, local['start'])
            if last_key is not None and key < last_key:
                raise MeteringError('facts must stay ordered by side and tick')
            last_key = key
            if side not in seen_sides:
                seen_sides.append(side)
            endpoints = fact['endpoints']
            fields(endpoints, ('start', 'end'))
            readable = {}
            for position, endpoint in endpoints.items():
                fields(endpoint, ('status', 'frame_id', 'reading'))
                ident(endpoint['frame_id'])
                if endpoint['status'] == _READABLE:
                    _strict_int(endpoint['reading'], 0,
                                (1 << MAX_PAYLOAD_BITS) - 1, 'reading')
                    readable[position] = endpoint['reading']
                elif endpoint['status'] in (
                        _REASON_MISSING, _REASON_AMBIGUOUS):
                    if endpoint['reading'] is not None:
                        raise MeteringError(
                            'unreadable endpoint must not carry reading')
                else:
                    raise MeteringError('invalid endpoint status vocabulary')
            if fact['status'] == 'known':
                _strict_int(fact['increment_ml'], 0,
                            (1 << MAX_PAYLOAD_BITS) - 1, 'increment_ml')
                if fact['unknown_reason'] is not None:
                    raise MeteringError('known fact must not carry a reason')
                if set(readable) != {'start', 'end'}:
                    raise MeteringError(
                        'known fact requires two readable endpoints')
                if fact['increment_ml'] != \
                        readable['end'] - readable['start']:
                    raise MeteringError(
                        'increment disagrees with the endpoint readings')
            elif fact['status'] == 'unknown':
                if fact['increment_ml'] is not None:
                    raise MeteringError(
                        'unknown fact must not carry an increment')
                if fact['unknown_reason'] not in valid_reasons:
                    raise MeteringError('unknown fact carries an invalid reason')
            else:
                raise MeteringError('fact status must be known or unknown')
        if seen_sides != ['inlet', 'outlet']:
            raise MeteringError(
                'facts document must cover inlet then outlet')
    except MeteringError:
        raise
    except ValueError as error:
        raise MeteringError(str(error)) from error
    return document
