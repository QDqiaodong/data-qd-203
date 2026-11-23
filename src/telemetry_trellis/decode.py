from .code import transition,encode

def decode(frame,constraint,generators):
    # For each reachable state retain minimum metric, lexicographic survivor and
    # the number of ALL paths attaining that minimum, not just one survivor.
    paths={0:(0,(),1)};width=len(generators);received=frame['received']
    for offset in range(0,len(received),width):
        observation=received[offset:offset+width];next_paths={}
        for state,(cost,prefix,count) in paths.items():
            for bit in (0,1):
                target,word=transition(state,bit,constraint,generators)
                metric=cost+sum(a is not None and a!=b for a,b in zip(observation,word));candidate=prefix+(bit,)
                old=next_paths.get(target)
                if old is None or metric<old[0]:next_paths[target]=(metric,candidate,count)
                elif metric==old[0]:next_paths[target]=(metric,min(candidate,old[1]),count+old[2])
        paths=next_paths
    eligible={0:paths[0]} if frame['terminated'] else paths
    best=min(v[0] for v in eligible.values());winners=[(state,v) for state,v in eligible.items() if v[0]==best]
    state,winner=min(winners,key=lambda p:p[1][1]);bits=winner[1];count=sum(v[2] for _,v in winners)
    payload=bits[:frame['length']];reference=encode(payload,constraint,generators,frame['terminated'])
    if list(bits)!=list(payload)+([0]*(constraint-1) if frame['terminated'] else []):raise ValueError('termination invariant')
    corrections=[i for i,(a,b) in enumerate(zip(received,reference['symbols'])) if a is not None and a!=b]
    return {'id':frame['id'],'payload':list(payload),'selected_final_state':state,'distance':best,'optimal_paths':count,'ambiguous':count!=1,'reencoded':reference['symbols'],'state_path':reference['states'],'corrected_indices':corrections,'erasure_indices':[i for i,b in enumerate(received) if b is None]}
