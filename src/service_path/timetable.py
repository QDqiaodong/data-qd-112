"""Validation and normalisation of the v1 service-day snapshot.

``parse`` is the routing entry point used by both the default and the Pareto
modes.  ``snapshot`` exposes the same validation and normalised data without
binding the result to the query date, so the offline supply mart can iterate
over every service day in a requested natural-day window.
"""
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


def calendar_active(calendar, service_day):
    """True when a calendar runs on the given service day.

    Weekly service is the closed date range intersected with ``weekdays``;
    ``added`` dates activate a service even outside that range, and ``removed``
    dates win over both.  Dates are already parsed ``datetime.date`` values.
    """
    base = (calendar['start'] <= service_day <= calendar['end']
            and service_day.weekday() in calendar['weekdays'])
    return (base or service_day in calendar['added']) and service_day not in calendar['removed']


def snapshot(data):
    """Validate a v1 snapshot and return its normalised content.

    The returned mapping contains:
      * ``stops``: stop id -> transfer seconds;
      * ``calendars``: calendar id -> parsed ``start``/``end``/``weekdays``/
        ``added``/``removed``;
      * ``trips``: trip id -> ``{'calendar': id, 'calls': [(stop, arrival,
        departure), ...]}`` with service-day seconds;
      * ``query``: the original query object;
      * ``query_date`` and ``query_start``: parsed query date and departure.

    Validation is independent of the query date and covers stopped trips too.
    """
    fields(data,'stops calendars trips query');stops={};calendars={};trips={}
    for s in array(data['stops']):
        fields(s,'id transfer_seconds');sid=text(s['id'])
        if sid in stops: raise ValueError('duplicate stop')
        stops[sid]=integer(s['transfer_seconds'],0,3600)
    for c in array(data['calendars']):
        fields(c,'id start end weekdays added removed');cid=text(c['id'])
        if cid in calendars: raise ValueError('duplicate calendar')
        start=day(c['start']);end=day(c['end'])
        if start>end: raise ValueError('reversed calendar')
        weekdays=set()
        for w in array(c['weekdays']): weekdays.add(integer(w,0,6))
        added={day(d) for d in array(c['added'])};removed={day(d) for d in array(c['removed'])}
        if added&removed: raise ValueError('contradictory service exception')
        calendars[cid]={'start':start,'end':end,'weekdays':frozenset(weekdays),
                        'added':frozenset(added),'removed':frozenset(removed)}
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
        trips[tid]={'calendar':t['calendar'],'calls':calls}
    q=data['query'];fields(q,'date origin destination depart')
    query_date=day(q['date']);query_start=clock(q['depart'])
    if q['origin'] not in stops or q['destination'] not in stops: raise ValueError('unknown query stop')
    return {'stops':stops,'calendars':calendars,'trips':trips,'query':q,
            'query_date':query_date,'query_start':query_start}


def parse(data):
    snap=snapshot(data)
    stops=snap['stops'];q=snap['query']
    active={cid for cid,c in snap['calendars'].items()
            if calendar_active(c,snap['query_date'])}
    connections=[]
    for tid,trip in snap['trips'].items():
        if trip['calendar'] not in active:continue
        calls=trip['calls']
        for i,(a,b) in enumerate(zip(calls,calls[1:])):
            connections.append({'trip':tid,'index':i,'from':a[0],'to':b[0],
                                'departure':a[2],'arrival':b[1]})
    return stops,q,snap['query_start'],sorted(connections,key=lambda c:(c['departure'],c['trip'],c['index']))
