"""Offline continuous-session joint-decoding demo (no device attached).

Run from the repository root after installing the package (see README), or
with the source tree on the path:

    python3 -I examples/session_demo.py            # installed package
    PYTHONPATH=src python3 examples/session_demo.py

The demo synthesizes one continuously encoded record split across transport
frames, including a case where decoding a frame independently from the zero
state would give the wrong bit, a fully erased ambiguous frame, and a
terminated session whose forced-zero tail is scored once at the end.  Only
synthetic in-memory data is used.
"""
import json

from telemetry_trellis import decode_session
from telemetry_trellis.decode import decode as decode_frame


def show(title, result):
    print(title)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print()


def main():
    # Two one-bit frames of one continuous constraint-2 encoding.  The encoder
    # state after frame A carries into frame B; neither frame is terminated.
    joint = decode_session({
        'session_id': 'demo-continuous',
        'constraint': 2,
        'generators': [2, 3],
        'terminated': False,
        'tail_received': [],
        'frames': [
            {'id': 'A', 'payload_length': 1, 'received': [0, 1]},
            {'id': 'B', 'payload_length': 1, 'received': [1, 1]},
        ],
    })
    show('joint session decode', joint)

    # The same frame B, treated as an independent zero-state record, prefers
    # bit 1; the joint session correctly attributes bit 0 because state
    # continuation, not a reset, happens at the frame boundary.
    standalone = decode_frame(
        {'id': 'B', 'length': 1, 'terminated': False,
         'received': [1, 1]}, 2, [2, 3])
    print('frame B standalone payload:', standalone['payload'])
    print('frame B inside the session:', joint['frames'][1]['payload'])
    print()

    # A fully erased step stays ambiguous: two complete optimal paths exist
    # and the lexicographic representative [0] is not a confirmed reading.
    erased = decode_session({
        'session_id': 'demo-erased',
        'constraint': 2,
        'generators': [2, 3],
        'terminated': False,
        'tail_received': [],
        'frames': [{'id': 'X', 'payload_length': 1,
                    'received': [None, None]}],
    })
    show('fully erased frame', erased)

    # Terminated variant: the constraint-1 forced-zero tail arrives once in
    # tail_received and only constrains the final state after both frames.
    terminated = decode_session({
        'session_id': 'demo-terminated',
        'constraint': 2,
        'generators': [2, 3],
        'terminated': True,
        'tail_received': [0, 0],
        'frames': [
            {'id': 'A', 'payload_length': 1, 'received': [0, 1]},
            {'id': 'B', 'payload_length': 1, 'received': [1, 1]},
        ],
    })
    show('terminated session decode', terminated)


if __name__ == '__main__':
    main()
