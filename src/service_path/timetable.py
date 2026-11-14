from datetime import date
import re
from .io import fields, text, integer, array

def clock(v):
    if not isinstance(v,str) or not re.fullmatch(r'[0-4][0-9]:[0-5][0-9]:[0-5][0-9]',v): raise ValueError('expected HH:MM:SS service time')
    h,m,s=map(int,v.split(':'))
    if h>47: raise ValueError('service hour must be <=47')
    return h*3600+m*60+s

def day(v):
    if not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v): raise ValueError('expected ISO service date')
    return date.fromisoformat(v)

def parse(data):
    fields(data,'stops calendars trips query');stops={};calendars={};trips={}
    for s in array(data['stops']):
        fields(s,'id transfer_seconds');sid=text(s['id'])
        if sid in stops: raise ValueError('duplicate stop')
        stops[sid]=integer(s['transfer_seconds'],0,3600)
    for c in array(data['calendars']):
        fields(c,'id start end weekdays added removed');cid=text(c['id'])
        if cid in calendars: raise ValueError('duplicate calendar')
        if day(c['start'])>day(c['end']): raise ValueError('reversed calendar')
        for w in array(c['weekdays']): integer(w,0,6)
        added={day(d) for d in array(c['added'])};removed={day(d) for d in array(c['removed'])}
        if added&removed: raise ValueError('contradictory service exception')
        calendars[cid]=c
    for t in array(data['trips']):
        fields(t,'id calendar calls');tid=text(t['id'])
        if tid in trips or t['calendar'] not in calendars: raise ValueError('duplicate trip or unknown calendar')
        calls=[];seen=set();previous=None
        for call in array(t['calls']):
            fields(call,'stop arrival departure');sid=text(call['stop'])
            if sid not in stops or sid in seen: raise ValueError('unknown/repeated stop within trip')
            seen.add(sid);arrival=clock(call['arrival']);departure=clock(call['departure'])
            if departure<arrival or previous is not None and arrival<=previous: raise ValueError('invalid dwell or nonpositive travel')
            calls.append((sid,arrival,departure));previous=departure
        if len(calls)<2: raise ValueError('trip needs two calls')
        trips[tid]=(t['calendar'],calls)
    q=data['query'];fields(q,'date origin destination depart');d=day(q['date']);start=clock(q['depart'])
    if q['origin'] not in stops or q['destination'] not in stops: raise ValueError('unknown query stop')
    active=set()
    for cid,c in calendars.items():
        base=day(c['start'])<=d<=day(c['end']) and d.weekday() in c['weekdays']
        if (base or q['date'] in c['added']) and q['date'] not in c['removed']:active.add(cid)
    connections=[]
    for tid,(cid,calls) in trips.items():
        if cid not in active:continue
        for i,(a,b) in enumerate(zip(calls,calls[1:])):
            connections.append({'trip':tid,'index':i,'from':a[0],'to':b[0],'departure':a[2],'arrival':b[1]})
    return stops,q,start,sorted(connections,key=lambda c:(c['departure'],c['trip'],c['index']))
