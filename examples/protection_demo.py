"""Offline working-set XOR protection demo (no device attached).

Run from the repository root after installing the package (see README), or
with the source tree on the path::

    python3 -I examples/protection_demo.py            # installed package
    PYTHONPATH=src python3 examples/protection_demo.py

The story mirrors the data contract.  The capture-side catalogue covers five
frames: working frames A, B, C and D plus blind frame H, with four trusted
XOR records.  The demo builds the canonical protection package (which never
contains H), re-decodes a continuously encoded session for frames B and D with
``decode_session``, then completes every working frame from the package plus
that local evidence in the same way an offline workstation would after
loading the JSON package in another process.  Only synthetic data is used.
"""
import copy
import json

from telemetry_trellis import (
    build_protection_package,
    complete_frames,
    decode_session,
)


def show(title, value):
    print(title)
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    print()


def main():
    # A, B, C hold one payload bit each; D holds two; blind H holds one.
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
    show('canonical protection package (no blind frame H appears)', package)

    # Flipping the first two RHS values only changes the unseen value chosen
    # for blind H; the package must be byte-identical.
    flipped = copy.deepcopy(catalog)
    flipped['records'][0]['value'] = 1
    flipped['records'][1]['value'] = 0
    assert json.dumps(build_protection_package(flipped),
                      sort_keys=True) == json.dumps(package, sort_keys=True)

    # B and D are two frames of one non-terminated constraint-2 session.
    # B is observed uniquely as 0; the D frame stays fully ambiguous.
    session = decode_session({
        'session_id': 's1', 'constraint': 2, 'generators': [1, 2],
        'terminated': False, 'tail_received': [],
        'frames': [
            {'id': 'B', 'payload_length': 1, 'received': [0, None]},
            {'id': 'D', 'payload_length': 2,
             'received': [None, None, None, None]},
        ],
    })
    evidence = [
        {'session_id': session['session_id'], 'id': row['id'],
         'payload': row['payload'], 'ambiguous': row['ambiguous']}
        for row in session['frames']
    ]
    show('trusted local decode evidence (only these two frame rows)', evidence)

    # The workstation only needs the JSON package plus this evidence; the
    # original catalogue, blind frames and convolutional symbols are absent.
    completed = complete_frames(json.loads(json.dumps(package)), evidence)
    show('completed working frames', completed)

    # A=1 and C=0 are forced by protection with B observed as 0; D0=1 is
    # forced while D1 stays unknown, so D is not a complete frame.
    by_key = {tuple(row['frame_key']): row for row in completed}
    assert by_key[('s1', 'A')]['values'] == [1]
    assert by_key[('s1', 'B')]['sources'] == ['observed']
    assert by_key[('s1', 'C')]['values'] == [0]
    assert by_key[('s1', 'D')]['values'] == [1, None]
    assert not by_key[('s1', 'D')]['complete']
    print('protection completion demo assertions passed')


if __name__ == '__main__':
    main()
