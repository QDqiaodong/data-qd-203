from .checks import fields,array,integer,ident,unique

def parse(data):
    fields(data,('constraint','generators','frames'));constraint=integer(data['constraint'],2,7)
    generators=[integer(g,1,(1<<constraint)-1) for g in array(data['generators'],2,4)];unique(generators)
    frames=[]
    for f in array(data['frames'],1,20):
        fields(f,('id','payload_length','terminated','received'));name=ident(f['id']);length=integer(f['payload_length'],1,500)
        if type(f['terminated']) is not bool:raise ValueError('terminated must be boolean')
        steps=length+(constraint-1 if f['terminated'] else 0);received=array(f['received'],steps*len(generators),steps*len(generators))
        for bit in received:
            if bit is not None:integer(bit,0,1)
        frames.append({'id':name,'length':length,'terminated':f['terminated'],'received':received})
    unique([f['id'] for f in frames]);return constraint,generators,frames
