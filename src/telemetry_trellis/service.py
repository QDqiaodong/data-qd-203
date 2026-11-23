from .model import parse
from .decode import decode

def process(data):
    constraint,generators,frames=parse(data);results=[decode(f,constraint,generators) for f in frames]
    return {'frames':results,'summary':{'frames':len(results),'ambiguous':sum(f['ambiguous'] for f in results),'hamming_distance':sum(f['distance'] for f in results),'erasures':sum(len(f['erasure_indices']) for f in results)}}
