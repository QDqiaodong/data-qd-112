from .timetable import parse

def route(data):
    stops,q,start,connections=parse(data)
    # A vehicle's reachable prefix stays distinct from the best stop label.
    best={q['origin']:(start,[])};onboard={}
    for c in connections:
        prefix=None
        if c['trip'] in onboard:
            i,path=onboard[c['trip']]
            if i==c['index']-1:prefix=path
        label=best.get(c['from'])
        if prefix is None and label is not None:
            ready=label[0]+(0 if not label[1] else stops[c['from']])
            if ready<=c['departure']:prefix=label[1]
        if prefix is None: continue
        path=prefix+[c];onboard[c['trip']]=(c['index'],path)
        if c['to'] not in best or c['arrival']<best[c['to']][0]: best[c['to']]=(c['arrival'],path)
    label=best.get(q['destination'])
    if label is None:return {'query':q,'reachable':False,'arrival_seconds':None,'segments':[],'transfers':None}
    arrival,path=label;boards=sum(i==0 or path[i-1]['trip']!=c['trip'] for i,c in enumerate(path))
    return {'query':q,'reachable':True,'arrival_seconds':arrival,'elapsed_seconds':arrival-start,
            'segments':path,'transfers':max(0,boards-1)}
