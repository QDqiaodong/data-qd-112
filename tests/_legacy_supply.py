"""Frozen pre-optimisation reference of the supply build.

This module keeps the original in-memory ``build`` implementation byte-for-byte
in its result semantics so the tests can compare the bounded-memory pipeline in
``service_path.supply`` against the audited old behaviour row by row.  It is a
test helper, never imported by the shipped package, and must not be "optimised"
away: its whole purpose is to encode the previous result contract.
"""
from datetime import timedelta

from service_path.supply import (DAY_SECONDS, HOURS_PER_DAY, MAX_DAYS,
                                 MAX_FACTS, MAX_HOURLY_ROWS)
from service_path.timetable import calendar_active


def _day_window(start, end):
    if start > end:
        raise ValueError('start date is later than end date')
    days = (end - start).days + 1
    if days > MAX_DAYS:
        raise ValueError('date range spans %d natural days; at most %d are supported'
                         % (days, MAX_DAYS))
    return days


def legacy_build(snap, start, end):
    """Exact copy of the original materialising ``supply.build`` algorithm."""
    days = _day_window(start, end)
    stations = sorted(snap['stops'])
    hourly_rows = days * len(stations) * HOURS_PER_DAY
    if hourly_rows > MAX_HOURLY_ROWS:
        raise ValueError('%d natural days x %d stations needs %d hourly rows; at most %d'
                         ' are supported' % (days, len(stations), hourly_rows,
                                             MAX_HOURLY_ROWS))

    facts = []
    service_day = start - timedelta(days=1)
    while service_day <= end:
        active = {calendar_id for calendar_id, calendar in snap['calendars'].items()
                  if calendar_active(calendar, service_day)}
        for trip_id in sorted(snap['trips']):
            trip = snap['trips'][trip_id]
            if trip['calendar'] not in active:
                continue
            calls = trip['calls']
            # The final call has no onward departure; only calls 0..n-2 do.
            for call_index in range(len(calls) - 1):
                service_seconds = calls[call_index][2]
                natural_day = service_day + timedelta(days=service_seconds // DAY_SECONDS)
                if start <= natural_day <= end:
                    if len(facts) >= MAX_FACTS:
                        raise ValueError('window holds more than %d departures;'
                                         ' narrow the date range' % MAX_FACTS)
                    facts.append((service_day.isoformat(), trip_id, call_index,
                                  natural_day.isoformat(), calls[call_index][0],
                                  service_seconds % DAY_SECONDS))
        service_day += timedelta(days=1)
    facts.sort()

    stops = stations
    counts = {}
    seconds = {}
    for fact in facts:
        natural_day, stop_id, day_seconds = fact[3], fact[4], fact[5]
        hour = day_seconds // 3600
        key = (natural_day, stop_id, hour)
        counts[key] = counts.get(key, 0) + 1
        seconds.setdefault(key, set()).add(day_seconds)

    hourly = []
    for offset in range(days):
        natural_day = (start + timedelta(days=offset)).isoformat()
        for stop_id in stops:
            for hour in range(HOURS_PER_DAY):
                key = (natural_day, stop_id, hour)
                count = counts.get(key, 0)
                departures = seconds.get(key)
                if departures is None:
                    max_headway = 3600
                else:
                    # Deduplicated departure seconds plus both hour boundaries.
                    points = sorted({hour * 3600, (hour + 1) * 3600} | departures)
                    max_headway = max(later - earlier
                                      for earlier, later in zip(points, points[1:]))
                hourly.append((natural_day, stop_id, hour, count, max_headway))

    return {
        'facts': facts,
        'hourly': hourly,
        'stations': stops,
        'stats': {
            'natural_days': days,
            'service_days': days + 1,
            'stations': len(stops),
            'departures': len(facts),
            'hourly_rows': days * len(stations) * HOURS_PER_DAY,
        },
    }
