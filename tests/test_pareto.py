import json, random, subprocess, sys, tempfile, unittest
from datetime import date
from pathlib import Path
from service_path.pareto import pareto
ROOT=Path(__file__).resolve().parents[1]

def oracle(data):
    # Independent exhaustive checker: enumerates every feasible journey straight from
    # the raw JSON with its own calendar/clock handling, then takes the non-dominated set.
    stops={s['id']:s['transfer_seconds'] for s in data['stops']}
    q=data['query'];d=date.fromisoformat(q['date'])
    def clock(v):
        h,m,s=map(int,v.split(':'));return h*3600+m*60+s
    active=set()
    for c in data['calendars']:
        base=date.fromisoformat(c['start'])<=d<=date.fromisoformat(c['end']) and d.weekday() in c['weekdays']
        if (base or q['date'] in c['added']) and q['date'] not in c['removed']:active.add(c['id'])
    conns=[]
    for t in data['trips']:
        if t['calendar'] not in active:continue
        calls=t['calls']
        for i in range(len(calls)-1):
            conns.append((t['id'],i,calls[i]['stop'],calls[i+1]['stop'],
                          clock(calls[i]['departure']),clock(calls[i+1]['arrival'])))
    start=clock(q['depart']);found=[]
    def rec(stop,arrived,path):
        if stop==q['destination']:
            arr=path[-1][5] if path else start
            tr=sum(1 for i in range(1,len(path)) if path[i-1][0]!=path[i][0])
            found.append((arr,tr,tuple((c[0],c[1]) for c in path)))
            return  # passing through and returning later is always dominated
        for c in conns:
            if c[2]!=stop:continue
            if path and path[-1][0]==c[0] and path[-1][1]+1==c[1]:
                rec(c[3],c[5],path+[c])  # stay on the vehicle: no transfer buffer
            else:
                ready=arrived+(0 if not path else stops[stop])
                if ready<=c[4]:rec(c[3],c[5],path+[c])
    rec(q['origin'],start,[])
    best={}
    for arr,tr,sig in found:
        if (arr,tr) not in best or sig<best[(arr,tr)]:best[(arr,tr)]=sig
    return [(arr,tr,best[(arr,tr)]) for arr,tr in sorted(best)
            if not any(a2<=arr and t2<=tr and (a2<arr or t2<tr) for a2,t2 in best)]

def summary(result):
    return [(j['arrival_seconds'],j['transfers'],tuple((s['trip'],s['index']) for s in j['segments']))
            for j in result['journeys']]

def make_data(stops,trips,query,calendars=None):
    return {'stops':stops,'calendars':calendars if calendars is not None else
            [{'id':'cal','start':'2026-01-01','end':'2026-12-31','weekdays':[0,1,2,3,4,5,6],'added':[],'removed':[]}],
            'trips':trips,'query':query}

def stop(sid,ts=0):return {'id':sid,'transfer_seconds':ts}
def call(sid,arr,dep=None):return {'stop':sid,'arrival':arr,'departure':dep if dep is not None else arr}
def trip(tid,*calls,calendar='cal'):return {'id':tid,'calendar':calendar,'calls':list(calls)}
Q={'date':'2026-01-02','origin':'O','destination':'D','depart':'07:00:00'}

class ParetoDirectedTests(unittest.TestCase):
    def setUp(self):self.fixture=json.loads((ROOT/'fixtures/timetable.json').read_text())
    def test_earlier_more_transfers_vs_later_fewer(self):
        r=pareto(self.fixture)
        self.assertEqual(summary(r),[(87300,1,(('N1',0),('N2',0))),(88200,0,(('N3',0),))])
        self.assertEqual([j['elapsed_seconds'] for j in r['journeys']],[1500,2400])
        seg=r['journeys'][0]['segments'][1]
        self.assertEqual(seg,{'trip':'N2','index':0,'from':'B','to':'D','departure':86880,'arrival':87300})
    def test_same_arrival_different_transfers_is_dominated(self):
        data=make_data([stop('O'),stop('M',60),stop('D')],[
            trip('T1',call('O','08:00:00'),call('D','09:00:00')),
            trip('T2',call('O','08:00:00'),call('M','08:30:00')),
            trip('T3',call('M','08:31:00'),call('D','09:00:00'))],Q)
        self.assertEqual(summary(pareto(data)),[(32400,0,(('T1',0),))])
    def test_same_combination_keeps_lexicographically_smallest_path(self):
        data=make_data([stop('O'),stop('M',0),stop('X',0),stop('D')],[
            trip('A1',call('O','08:00:00'),call('M','08:30:00')),
            trip('A3',call('M','08:30:00'),call('D','09:00:00')),
            trip('A2',call('O','08:00:00'),call('X','08:40:00')),
            trip('A4',call('X','08:40:00'),call('D','09:00:00'))],Q)
        self.assertEqual(summary(pareto(data)),[(32400,1,(('A1',0),('A3',0)))])
    def test_intermediate_labels_and_onboard_state_survive(self):
        # At B: (08:40, 1 transfer) via T1+T2 and (08:50, 0 transfers) via T3 both matter.
        stops=[stop('O'),stop('X',0),stop('B',0),stop('D')]
        data=make_data(stops,[
            trip('T1',call('O','08:00:00'),call('X','08:20:00')),
            trip('T2',call('X','08:25:00'),call('B','08:40:00')),
            trip('T3',call('O','08:05:00'),call('B','08:50:00')),
            trip('T4',call('B','08:45:00'),call('D','09:00:00')),   # only the 1-transfer label catches it
            trip('T5',call('B','08:55:00'),call('D','09:10:00'))],Q)# both catch it; fewer transfers wins
        self.assertEqual(summary(pareto(data)),
                         [(32400,2,(('T1',0),('T2',0),('T4',0))),(33000,1,(('T3',0),('T5',0)))])
    def test_staying_on_vehicle_beats_reboarding_same_trip(self):
        stops=[stop('O'),stop('B',3600),stop('D')]
        data=make_data(stops,[
            trip('T1',call('O','08:00:00'),call('B','08:30:00','08:31:00'),call('D','09:00:00')),
            trip('T2',call('O','08:10:00'),call('B','08:25:00'))],Q)
        # Reboarding T1 at B would need 3600s buffer and add a transfer; staying on is free.
        self.assertEqual(summary(pareto(data)),[(32400,0,(('T1',0),('T1',1)))])
    def test_onboard_prefix_with_fewer_transfers_wins_same_vehicle(self):
        # T2 is boarded at R with 2 transfers, but a 0-transfer label reaches B
        # (a later stop on T2's route): reboarding there must replace the onboard prefix.
        stops=[stop('O'),stop('P',0),stop('R',0),stop('B',0),stop('D')]
        data=make_data(stops,[
            trip('T0',call('O','07:30:00'),call('P','07:40:00')),
            trip('T8',call('P','07:45:00'),call('R','07:55:00')),
            trip('T9',call('O','08:00:00'),call('B','08:20:00')),
            trip('T2',call('R','08:00:00'),call('B','08:30:00','08:31:00'),call('D','09:00:00'))],Q)
        self.assertEqual(summary(pareto(data)),[(32400,1,(('T9',0),('T2',1)))])
    def test_unreachable_and_origin_equals_destination(self):
        data=make_data([stop('O'),stop('D')],[trip('T1',call('O','06:00:00'),call('D','06:30:00'))],Q)
        self.assertEqual(pareto(data)['journeys'],[])
        only=pareto(make_data([stop('O'),stop('D')],[],dict(Q,destination='O')))['journeys']
        self.assertEqual(only,[{'arrival_seconds':25200,'elapsed_seconds':0,'transfers':0,'segments':[]}])
    def test_calendar_exceptions_and_midnight(self):
        trips=[trip('T1',call('O','23:50:00'),call('D','25:10:00'))]
        stops=[stop('O'),stop('D')];q=dict(Q,depart='23:40:00')
        added=[{'id':'cal','start':'2026-02-01','end':'2026-12-31','weekdays':[0,1,2,3,4,5,6],
                'added':['2026-01-02'],'removed':[]}]
        self.assertEqual(summary(pareto(make_data(stops,trips,q,added))),[(90600,0,(('T1',0),))])
        removed=[{'id':'cal','start':'2026-01-01','end':'2026-12-31','weekdays':[0,1,2,3,4,5,6],
                  'added':[],'removed':['2026-01-02']}]
        self.assertEqual(pareto(make_data(stops,trips,q,removed))['journeys'],[])
    def test_input_permutation_does_not_change_journeys(self):
        r=pareto(self.fixture)
        for key in ('stops','calendars','trips'):self.fixture[key].reverse()
        self.assertEqual(pareto(self.fixture),r)

class ParetoOracleTests(unittest.TestCase):
    def random_data(self,rng):
        stops=[stop(chr(65+i),rng.choice([0,30,120])) for i in range(rng.randint(3,4))]
        ids=[s['id'] for s in stops]
        cals=[{'id':'cal','start':'2026-01-01','end':'2026-12-31','weekdays':[0,1,2,3,4,5,6],'added':[],'removed':[]}]
        roll=rng.random()
        if roll<0.15:cals[0]['removed']=['2026-01-02']
        elif roll<0.30:
            cals[0]['weekdays']=[0,1,2,3]  # 2026-01-02 is a Friday (weekday 4)
            if rng.random()<0.5:cals[0]['added']=['2026-01-02']
        trips=[]
        for i in range(rng.randint(4,6)):
            t=rng.randint(22*3600,24*3600+1800);calls=[]
            for sid in rng.sample(ids,rng.randint(2,3)):
                arr=t+rng.randint(0,180);dep=arr+rng.randint(0,60)
                calls.append(call(sid,'%02d:%02d:%02d'%(arr//3600,arr//60%60,arr%60),
                                      '%02d:%02d:%02d'%(dep//3600,dep//60%60,dep%60)))
                t=dep+rng.randint(60,480)
            trips.append(trip('T%d'%i,*calls))
        origin=rng.choice(ids)
        destination=rng.choice(ids) if rng.random()<0.9 else origin
        depart=rng.randint(21*3600,22*3600)
        q={'date':'2026-01-02','origin':origin,'destination':destination,
           'depart':'%02d:%02d:%02d'%(depart//3600,depart//60%60,depart%60)}
        return make_data(stops,trips,q,cals)
    def random_chain_data(self,rng):
        # Cascading legs along a line of stops, raced by slower long trips.
        n=rng.randint(4,5);ids=[chr(65+i) for i in range(n)]
        stops=[stop(sid,rng.choice([0,30,120])) for sid in ids]
        cals=[{'id':'cal','start':'2026-01-01','end':'2026-12-31','weekdays':[0,1,2,3,4,5,6],'added':[],'removed':[]}]
        if rng.random()<0.1:cals[0]['removed']=['2026-01-02']
        def fmt(t):return '%02d:%02d:%02d'%(t//3600,t//60%60,t%60)
        trips=[];t=22*3600+rng.randint(0,1800)
        for j in range(n-1):
            arr=t+rng.randint(120,600)
            trips.append(trip('L%d'%j,call(ids[j],fmt(t)),call(ids[j+1],fmt(arr),fmt(arr+rng.randint(0,60)))))
            t=arr+rng.randint(0,400)
        for k in range(rng.randint(1,3)):
            lo=rng.randint(0,n-2);hi=rng.randint(lo+1,n-1)
            seq=ids[lo:hi+1] if rng.random()<0.5 else ids[lo:hi+1][::-1]
            dep=22*3600+rng.randint(0,3600)
            trips.append(trip('D%d'%k,call(seq[0],fmt(dep)),call(seq[-1],fmt(dep+rng.randint(600,3600)))))
        q={'date':'2026-01-02','origin':ids[0],'destination':ids[-1],'depart':fmt(21*3600+rng.randint(0,1800))}
        return make_data(stops,trips,q,cals)
    def test_exhaustive_oracle_on_random_small_graphs(self):
        rng=random.Random(112)
        for case in range(300):
            for gen in (self.random_data,self.random_chain_data):
                data=gen(rng)
                with self.subTest(case=case,gen=gen.__name__):
                    self.assertEqual(summary(pareto(data)),oracle(data))
    def test_oracle_agrees_on_permuted_input(self):
        rng=random.Random(113)
        for case in range(60):
            data=(self.random_chain_data if case%2 else self.random_data)(rng)
            before=summary(pareto(data))
            for key in ('stops','calendars','trips'):rng.shuffle(data[key])
            with self.subTest(case=case):
                self.assertEqual(summary(pareto(data)),before)
                self.assertEqual(before,oracle(data))

class ParetoCliTests(unittest.TestCase):
    def setUp(self):self.data=json.loads((ROOT/'fixtures/timetable.json').read_text())
    def test_cli_pareto_matches_and_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);out=p/'report.json';bad=p/'bad.json';bad.write_text('{}');out.write_text('previous')
            args=[sys.executable,'-m','service_path.cli','--output',str(out),'--pareto','--source']
            r=subprocess.run(args+[str(bad)],capture_output=True)
            self.assertEqual(r.returncode,2);self.assertEqual(out.read_text(),'previous')
            r=subprocess.run(args+[str(ROOT/'fixtures/timetable.json')],capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads(out.read_text()),pareto(self.data))
            self.assertEqual(json.loads(r.stdout),pareto(self.data))
    def test_cli_default_mode_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'report.json'
            r=subprocess.run([sys.executable,'-m','service_path.cli','--output',str(out),
                              '--source',str(ROOT/'fixtures/timetable.json')],capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr)
            result=json.loads(out.read_text())
            self.assertNotIn('journeys',result);self.assertEqual(result['arrival_seconds'],87300)

if __name__=='__main__':unittest.main()
