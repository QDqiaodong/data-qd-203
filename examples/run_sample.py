"""Fixed offline integration example; run from repository root."""
import json
from telemetry_trellis import process
from telemetry_trellis.io import load,publish

if __name__=='__main__':
    result=process(load('fixtures/input.json'))
    publish('run/sample.json',result)
    print(json.dumps(result['summary'],ensure_ascii=False,sort_keys=True))
