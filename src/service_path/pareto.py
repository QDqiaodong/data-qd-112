"""Pareto routing over (arrival_seconds, transfers).

Keeps a set of (arrival, transfers) labels per stop instead of a single
earliest-arrival label, so a later-arriving label with fewer transfers is never
discarded just because an intermediate stop was reached earlier. A label is
only dropped when another label reaches the same stop no later with strictly
fewer transfers: every journey through it is then dominated. Labels that tie on
transfers but arrive later are kept, because they may carry the
lexicographically smallest (trip, index) path for some final combination.

Same-vehicle continuation needs no transfer buffer, the first boarding is free
of transfer time, and a transfer charges the transfer stop's transfer_seconds,
exactly as in the default earliest-arrival scan.
"""
from .timetable import parse


def signature(path):
    return [(c['trip'], c['index']) for c in path]


def insert(labels, arrival, transfers, path):
    # labels: mutable list of [arrival, transfers, path].
    for lab in labels:
        if lab[0] <= arrival and lab[1] < transfers:
            return  # no-later arrival with strictly fewer transfers dominates
        if lab[0] == arrival and lab[1] == transfers:
            if signature(path) < signature(lab[2]):
                lab[2] = path  # same combination: keep the lexicographically smallest path
            return
    labels[:] = [lab for lab in labels if not (arrival <= lab[0] and transfers < lab[1])]
    labels.append([arrival, transfers, path])


def pareto(data):
    stops, q, start, connections = parse(data)
    if q['origin'] == q['destination']:
        journey = {'arrival_seconds': start, 'elapsed_seconds': 0, 'transfers': 0, 'segments': []}
        return {'query': q, 'journeys': [journey]}
    labels = {q['origin']: [[start, 0, []]]}
    onboard = {}  # trip -> (index, transfers, path); all prefixes of one trip at one
    # index share the same position and time, so fewer transfers dominates and a single
    # best prefix (min transfers, then min segment signature) is complete.
    for c in connections:
        candidates = []  # (transfers, signature, path) ending on connection c
        state = onboard.get(c['trip'])
        if state is not None and state[0] == c['index'] - 1:
            path = state[2] + [c]
            candidates.append((state[1], signature(path), path))
        for lab in labels.get(c['from'], []):
            ready = lab[0] + (0 if not lab[2] else stops[c['from']])
            if ready <= c['departure']:
                path = lab[2] + [c]
                candidates.append((lab[1] + (0 if not lab[2] else 1), signature(path), path))
        if not candidates:
            continue
        transfers, _, path = min(candidates, key=lambda t: (t[0], t[1]))
        onboard[c['trip']] = (c['index'], transfers, path)
        insert(labels.setdefault(c['to'], []), c['arrival'], transfers, path)
    journeys = []
    for arrival, transfers, path in labels.get(q['destination'], []):
        dominated = any(a2 <= arrival and t2 <= transfers and (a2 < arrival or t2 < transfers)
                        for a2, t2, _ in labels[q['destination']])
        if not dominated:
            journeys.append({'arrival_seconds': arrival, 'elapsed_seconds': arrival - start,
                             'transfers': transfers, 'segments': path})
    journeys.sort(key=lambda j: (j['arrival_seconds'], j['transfers']))
    return {'query': q, 'journeys': journeys}
