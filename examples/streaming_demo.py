"""Offline incremental-delivery example; no device is involved.

Run from the repository root after installing the package, or with the source
tree on the path:

    python3 -I examples/streaming_demo.py            # installed package
    PYTHONPATH=src python3 examples/streaming_demo.py

The demo reproduces the acceptance story for a shard-arriving record: append
fragments (some ending inside a coding step), correct an unconfirmed span, hold
back until enough whole steps are confirmed, deliver payload bits in order,
hand the JSON state to a fresh process, and finally print the single-frame
decoding evidence.
"""
import json
import tempfile
from pathlib import Path

from telemetry_trellis import (
    append,
    confirm,
    finish,
    load_state,
    new_state,
    progress,
    replace,
    save_state,
)


def show(label, result):
    delivery = result['delivery']
    print(label, json.dumps({
        'received': result['received_count'],
        'confirmed': result['confirmed'],
        'delivered': result['delivered'],
        'delivery': delivery,
    }, ensure_ascii=False))


def main():
    state = new_state('demo-record', 2, [1, 2, 3], 4, False)
    # [0,null,null | 0,0,0 | 0,0,0], split inside the first group.
    for fragment in ([0, None], [None, 0, 0], [0, 0], [0, 0]):
        state = append(state, fragment)['state']
    # Only the first group is confirmed; nothing is certain yet.
    result = confirm(state, 4)
    show('confirm 4 :', result)
    state = result['state']

    # Correct the second group inside the still-mutable region.
    state = replace(state, 4, [1, 1])['state']
    result = confirm(state, 9)
    show('confirm 9 :', result)
    state = result['state']

    # Hand the state to a fresh offline step without replaying any fragment.
    directory = Path(tempfile.mkdtemp(prefix='trellis-demo-'))
    path = directory / 'state.json'
    save_state(path, state)
    resumed = load_state(path)
    print('handoff   :', json.dumps(progress(resumed), ensure_ascii=False))

    resumed = append(resumed, [1, 0, 1])['state']
    result = confirm(resumed, 12)
    show('confirm 12:', result)
    final = finish(result['state'])
    show('finish    :', final)
    evidence = final['evidence']
    print('evidence  :', json.dumps({
        'payload': evidence['payload'],
        'distance': evidence['distance'],
        'optimal_paths': evidence['optimal_paths'],
        'corrected_indices': evidence['corrected_indices'],
        'erasure_indices': evidence['erasure_indices'],
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
