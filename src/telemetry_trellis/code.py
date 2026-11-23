from .checks import integer,unique

def transition(state,bit,constraint,generators):
    register=(state<<1)|bit
    output=tuple((register&g).bit_count()%2 for g in generators)
    return register&((1<<(constraint-1))-1),output

def encode(bits,constraint,generators,terminated=True):
    integer(constraint,2,7)
    if not isinstance(bits,(list,tuple)) or not 1<=len(bits)<=500:raise ValueError('bounded source bits required')
    for bit in bits:integer(bit,0,1)
    if not isinstance(generators,(list,tuple)) or not 2<=len(generators)<=4:raise ValueError('bounded generators required')
    for g in generators:integer(g,1,(1<<constraint)-1)
    unique(generators)
    if type(terminated) is not bool:raise ValueError('terminated must be boolean')
    state=0;output=[];states=[0]
    source=list(bits)+([0]*(constraint-1) if terminated else [])
    for bit in source:
        state,word=transition(state,bit,constraint,generators);output.extend(word);states.append(state)
    return {'symbols':output,'states':states}
