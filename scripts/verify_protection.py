"""Reproducible offline verification for XOR protection packages.

Run with the source tree on the path or from an installed package (the
script itself only uses the Python standard library and
``telemetry_trellis``)::

    PYTHONPATH=src python3 scripts/verify_protection.py
    python3 -I scripts/verify_protection.py     # self-locates src/

When launched with ``-I`` (so ``PYTHONPATH`` is ignored), the script inserts
the repository ``src`` tree itself, mirroring the fresh-process test runners.

It rebuilds the 128-frame / 32768-bit / 512-record offline scenario from the
data contract, runs preparation, JSON round-trip and frame completion in one
process, and asserts the ``tracemalloc`` allocation peak stays below 48 MiB
with exactly one bit (x0) determined.  It additionally checks the small
acceptance geometry and canonical-package invariance.  No device is
contacted and no third-party dependency is used.
"""
import json
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from telemetry_trellis import (
    SOURCE_PROTECTED,
    build_protection_package,
    complete_frames,
)

MEMORY_BUDGET = 48 * 1024 * 1024


def large_catalog():
    frames = [
        {'session_id': 's', 'frame_id': 'F%03d' % i, 'payload_length': 256}
        for i in range(128)]
    # The last frame is blind; the other 127 frames (32512 bits) are working.
    selection = [['s', 'F%03d' % i] for i in range(127)]

    def coordinate(bit):
        return ['s', 'F%03d' % (bit // 256), bit % 256]

    records = [{'coordinates': [coordinate(0)], 'value': 1}]
    for i in range(1, 512):
        start = 63 * (i - 1)
        records.append({
            'coordinates': [coordinate(start + k) for k in range(64)],
            'value': 0})
    return {'frames': frames, 'selection': selection, 'records': records}


def verify_large_scale():
    catalog = large_catalog()
    tracemalloc.start()
    try:
        package = build_protection_package(catalog)
        blob = json.dumps(package, sort_keys=True, separators=(',', ':'))
        reloaded = json.loads(blob)
        completed = complete_frames(reloaded, [])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak <= MEMORY_BUDGET, 'peak %.2f MiB exceeds 48 MiB' % (
        peak / 1024 / 1024)
    assert len(package['constraints']) == 512
    known = [value for frame in completed for value in frame['values']
             if value is not None]
    unknown = [value for frame in completed for value in frame['values']
               if value is None]
    assert known == [1], 'only x0 must be determined'
    assert len(unknown) == 127 * 256 - 1
    first = completed[0]
    assert first['values'][0] == 1
    assert first['sources'][0] == SOURCE_PROTECTED
    assert all(value is None for value in first['values'][1:])
    assert all(not frame['complete'] for frame in completed)
    assert len(completed) == 127  # the blind frame never appears
    print('large-scale: peak %.2f MiB, exactly x0=1 fixed, %d free bits'
          % (peak / 1024 / 1024, len(unknown)))


def verify_acceptance_geometry():
    catalog = {
        'frames': [
            {'session_id': 's1', 'frame_id': 'A', 'payload_length': 1},
            {'session_id': 's1', 'frame_id': 'B', 'payload_length': 1},
            {'session_id': 's1', 'frame_id': 'C', 'payload_length': 1},
            {'session_id': 's1', 'frame_id': 'D', 'payload_length': 2},
            {'session_id': 's1', 'frame_id': 'H', 'payload_length': 1},
        ],
        'selection': [['s1', 'A'], ['s1', 'B'], ['s1', 'C'], ['s1', 'D']],
        'records': [
            {'coordinates': [['s1', 'A', 0], ['s1', 'H', 0]], 'value': 0},
            {'coordinates': [['s1', 'H', 0], ['s1', 'B', 0]], 'value': 1},
            {'coordinates': [['s1', 'B', 0], ['s1', 'C', 0]], 'value': 0},
            {'coordinates': [['s1', 'C', 0], ['s1', 'D', 0]], 'value': 1},
        ],
    }
    package = build_protection_package(catalog)
    assert 'H' not in json.dumps(package)

    # Reordering, a derivable record and flipping blind H's value all leave
    # the serialized package identical.
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'))

    baseline = canonical(package)
    shuffled = json.loads(json.dumps(catalog))
    shuffled['frames'] = list(reversed(shuffled['frames']))
    shuffled['selection'] = list(reversed(shuffled['selection']))
    shuffled['records'] = list(reversed(shuffled['records']))
    assert canonical(build_protection_package(shuffled)) == baseline
    flipped = json.loads(json.dumps(catalog))
    flipped['records'][0]['value'] = 1
    flipped['records'][1]['value'] = 0
    assert canonical(build_protection_package(flipped)) == baseline

    evidence = [
        {'session_id': 's1', 'id': 'B', 'payload': [0],
         'ambiguous': False},
        {'session_id': 's1', 'id': 'D', 'payload': [0, 0],
         'ambiguous': True},
    ]
    completed = complete_frames(json.loads(baseline), evidence)
    by_key = {tuple(row['frame_key']): row for row in completed}
    assert by_key[('s1', 'A')]['values'] == [1]
    assert by_key[('s1', 'B')]['sources'] == ['observed']
    assert by_key[('s1', 'C')]['values'] == [0]
    assert by_key[('s1', 'D')]['values'] == [1, None]
    assert not by_key[('s1', 'D')]['complete']
    print('acceptance geometry: A=1, B=0 observed, C=0, D=[1, null]')


def main():
    verify_acceptance_geometry()
    verify_large_scale()
    print('all offline protection verifications passed')


if __name__ == '__main__':
    sys.exit(main())
