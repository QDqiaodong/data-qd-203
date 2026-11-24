"""Offline local-metering and reference-alignment example; no device attached.

Run from the repository root after installing the package (see README), or
with the source tree on the path:

    python3 -I examples/metering_demo.py            # installed package
    PYTHONPATH=src python3 examples/metering_demo.py

The demo simulates the whole offline hand-off with synthetic data only:

1. the existing public ``process`` API produces uniquely decoded single-frame
   evidence the way the upstream decoder would;
2. the simulated upstream publisher builds the point catalogue (meter IDs,
   local ticks, frame IDs and the frozen SHA-256 evidence digests);
3. ``build_meter_facts`` verifies every digest and derives the local
   metering facts without seeing raw symbols or calling a decoder;
4. ``align_meter_facts`` projects the same prepared facts onto shared
   reference time against two different calibration catalogues, showing that
   realignment neither needs frame evidence nor rewrites the facts.
"""
import json

from telemetry_trellis import (
    align_meter_facts,
    build_meter_facts,
    evidence_digest,
    process,
)
from telemetry_trellis.code import encode

CONSTRAINT = 2
GENERATORS = [3, 1]
PAYLOAD_BITS = 8
INLET_METER = 'bench-inlet'
OUTLET_METER = 'bench-outlet'


def decoded_frame(frame_id, reading):
    """Synthesize one unique-decode frame result carrying an MSB-first value."""
    payload = [(reading >> (PAYLOAD_BITS - 1 - k)) & 1
               for k in range(PAYLOAD_BITS)]
    received = encode(payload, CONSTRAINT, GENERATORS, True)['symbols']
    request = {
        'constraint': CONSTRAINT,
        'generators': GENERATORS,
        'frames': [{'id': frame_id, 'payload_length': PAYLOAD_BITS,
                    'terminated': True, 'received': received}],
    }
    frame = process(request)['frames'][0]
    assert frame['optimal_paths'] == 1 and not frame['ambiguous']
    return frame


def publish_catalogue(meter_specs):
    """Simulate the trusted upstream catalogue/evidence publisher."""
    points = []
    evidence = []
    for meter_id, side, specs in meter_specs:
        for index, (tick, reading) in enumerate(specs):
            frame_id = f'{side}-frame-{index}'
            frame = decoded_frame(frame_id, reading)
            points.append({
                'meter_id': meter_id,
                'tick': tick,
                'frame_id': frame_id,
                'evidence_digest': evidence_digest(
                    meter_id, tick, frame_id, frame),
            })
            evidence.append(frame)
    catalogue = {
        'inlet_meter_id': INLET_METER,
        'outlet_meter_id': OUTLET_METER,
        'points': points,
    }
    return catalogue, evidence


def anchors(spec):
    return [{'tick': tick, 'reference_time': ref, 'interpolate': enabled}
            for tick, ref, enabled in spec]


def main():
    # Acceptance geometry: inlet (tick, reading) and outlet points.
    inlet_spec = [(0, 0), (6, 12), (12, 24)]
    outlet_spec = [(0, 0), (4, 4), (10, 10), (16, 16)]
    catalogue, evidence = publish_catalogue([
        (INLET_METER, 'inlet', inlet_spec),
        (OUTLET_METER, 'outlet', outlet_spec),
    ])

    # Local metering facts: verified evidence only, no raw symbols involved.
    facts = build_meter_facts(catalogue, evidence)
    print('local facts:')
    for fact in facts['facts']:
        print(' ', fact['fact_id'], fact['local_range'],
              fact['status'], fact['increment_ml'])

    base_timing = {
        'inlet_meter_id': INLET_METER,
        'outlet_meter_id': OUTLET_METER,
        'inlet_anchors': anchors([
            (0, 0, False), (6, 6, True), (12, 18, True)]),
        'outlet_anchors': anchors([
            (0, 2, False), (16, 18, True)]),
    }
    aligned = align_meter_facts(facts, base_timing)
    print('\npaired downstream facts (base calibration):')
    for record in aligned['paired']:
        ref = record['reference_range']
        print(' ', f"[{ref['start']['num']},{ref['end']['num']})",
              'inlet share', record['inlet']['share_ml'],
              'outlet share', record['outlet']['share_ml'])
    print('unpaired:')
    for record in aligned['unpaired']:
        print(' ', record['record_id'], record['type'], record['side'],
              json.dumps(record['share_ml'], separators=(',', ':')))

    # The same facts are realigned against a revised timing catalogue; the
    # facts document is reused verbatim and no frame evidence is supplied.
    revised_timing = dict(base_timing)
    revised_timing['inlet_anchors'] = anchors([
        (0, 0, False), (6, 8, True), (12, 18, True)])
    realigned = align_meter_facts(facts, revised_timing)
    print('\npaired downstream facts (revised inlet calibration):')
    for record in realigned['paired']:
        ref = record['reference_range']
        share = record['inlet']['share_ml']
        print(' ', f"[{ref['start']['num']}/{ref['start']['den']},"
                   f"{ref['end']['num']}/{ref['end']['den']})",
              f"inlet {share['num']}/{share['den']} ml")


if __name__ == '__main__':
    main()
