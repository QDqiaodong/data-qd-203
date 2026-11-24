"""Offline readback scheduling and settlement demo (no device attached).

Run from the repository root after installing the package (see README), or
with the source tree on the path:

    python3 -I examples/readback_demo.py            # installed package
    PYTHONPATH=src python3 examples/readback_demo.py

The demo writes a receiver-facing order and a self-contained continuation
document under run/readback/, then "reopens" the continuation as a fresh
offline step and settles synthetic window replies, including one unavailable
window and one overlapping-coordinate case.
"""
import json
from pathlib import Path

from telemetry_trellis import (
    load_continuation,
    plan_readback,
    save_continuation,
    save_order,
    settle_readback,
)

OUT = Path('run/readback')
REQUEST = {
    'capture_id': 'cap-demo',
    'constraint': 2,
    'generators': [1, 2],
    'payload_length': 3,
    'terminated': True,
    # Distance 0, two optimal payloads (000 and 100); erased at 0, 3, 5.
    'received': [None, 0, 0, None, 0, None, 0, 0],
    'quota': 3,
    'windows': [
        {'id': 'w0', 'start': 0, 'end': 4, 'fee': 1},
        {'id': 'w1', 'start': 0, 'end': 1, 'fee': 3},
        {'id': 'w2', 'start': 3, 'end': 6, 'fee': 2},
    ],
}


def main():
    plan = plan_readback(REQUEST)
    print('plan:', json.dumps(
        {key: plan[key] for key in
         ('status', 'distance', 'optimal_paths', 'selected_windows',
          'total_fee')}, ensure_ascii=False))
    if plan['status'] != 'scheduled':
        raise SystemExit('demo expected a schedulable request')

    order_path = OUT / 'order.json'
    continuation_path = OUT / 'continuation.json'
    save_order(order_path, plan['readback_order'])
    save_continuation(continuation_path, plan['continuation'])
    print('order written:', order_path)
    print('continuation written:', continuation_path)

    # A later, independent processing step reloads the continuation; no live
    # object and no "latest capture" lookup is involved.
    bundle = load_continuation(continuation_path)
    schedule_id = bundle['schedule_id']
    reply = {
        'schedule_id': schedule_id,
        'windows': [
            # w0 returns nothing this time; the remaining window still has to
            # carry enough evidence under the scheduling guarantee.
            {'window_id': 'w0', 'available': False},
            {'window_id': 'w2', 'available': True,
             'bits': {'3': 1, '5': 0}},
        ],
    }
    settled = settle_readback(bundle, reply)
    print('settled:', json.dumps({
        'filled_indices': settled['filled_indices'],
        'unavailable_windows': settled['unavailable_windows'],
        'payload': settled['decode']['payload'],
        'distance': settled['decode']['distance'],
        'optimal_paths': settled['decode']['optimal_paths'],
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
