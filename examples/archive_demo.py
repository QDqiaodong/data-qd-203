"""Offline archive example; run from the repository root.

Demonstrates the durable telemetry archive on synthetic data only: open an
archive by directory path, submit batches, query per-frame evidence, page the
downstream change log from a cursor, reopen the archive and retry a batch.
Nothing here connects to receiving equipment.
"""
import json
import shutil
from pathlib import Path

from telemetry_trellis import Archive

RUN_DIR = Path('run/archive_demo')
DESC = {'constraint': 2, 'generators': [3, 1],
        'payload_length': 1, 'terminated': False}


def update(capture, revision, action, received=None, frame='frame-7'):
    item = {'frame_id': frame, 'capture_id': capture, 'revision': revision,
            'action': action, **DESC}
    if received is not None:
        item['received'] = received
    return item


def main():
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    first_request = {
        'batch_id': 'batch-0001',
        'updates': [
            update('station-A', 1, 'write', [0, 1]),
            update('station-B', 1, 'write', [None, 1]),
            update('station-C', 1, 'write', [None, 1]),
        ],
    }
    with Archive(RUN_DIR) as archive:
        receipt = archive.submit(first_request)
        print('receipt 1:', json.dumps(receipt, sort_keys=True))
        print('evidence :', json.dumps(archive.evidence('frame-7'),
                                       sort_keys=True))
        # A newer revision of C makes the frame ambiguous: downstream revoke.
        receipt = archive.submit({
            'batch_id': 'batch-0002',
            'updates': [update('station-C', 3, 'write', [0, None])],
        })
        print('receipt 2:', json.dumps(receipt, sort_keys=True))

        cursor = 0
        pages = []
        while True:
            page = archive.read_changes(cursor, page_size=1)
            if not page['changes']:
                break
            pages.extend(page['changes'])
            cursor = page['cursor']
        print('change log:', json.dumps(pages, sort_keys=True))

    # Reopen after the process exits: evidence, history and idempotent retry
    # are all still available from disk.
    with Archive(RUN_DIR) as reopened:
        retried = reopened.submit(first_request)
        print('retried 1 seq:', retried['changes'][0]['seq'])
        print('published_seq after retry:',
              reopened.evidence('frame-7')['published_seq'])


if __name__ == '__main__':
    main()
