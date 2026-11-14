import json, unittest, subprocess, sys, tempfile
from pathlib import Path
from service_path.route import route
from service_path.timetable import clock
ROOT=Path(__file__).resolve().parents[1]
class RouteTests(unittest.TestCase):
    def setUp(self):self.data=json.loads((ROOT/'fixtures/timetable.json').read_text())
    def test_midnight_and_exact_transfer(self):
        r=route(self.data);self.assertEqual(r['arrival_seconds'],87300);self.assertEqual(r['transfers'],1)
        self.assertEqual([c['trip'] for c in r['segments']],['N1','N2'])
    def test_missed_transfer_uses_later_direct_service(self):
        self.data['stops'][1]['transfer_seconds']=181
        r=route(self.data);self.assertEqual(r['arrival_seconds'],88200);self.assertEqual(r['transfers'],0)
    def test_staying_on_vehicle_needs_no_transfer_time(self):
        self.data['query']['destination']='C';self.data['stops'][1]['transfer_seconds']=3600
        self.assertEqual(route(self.data)['arrival_seconds'],87600)
    def test_removed_calendar_and_origin_equals_destination(self):
        self.data['calendars'][0]['removed']=['2026-01-02'];self.assertFalse(route(self.data)['reachable'])
        self.data['query']['destination']='A';self.assertEqual(route(self.data)['elapsed_seconds'],0)
    def test_added_service_outside_regular_window(self):
        self.data['calendars'][0]['start']='2026-02-01';self.data['calendars'][0]['added']=['2026-01-02']
        self.assertTrue(route(self.data)['reachable'])
    def test_invalid_times_and_references(self):
        for t in ['48:00:00','24:60:00','1:00:00']:
            with self.assertRaises(ValueError):clock(t)
        self.data['trips'][0]['calls'][0]['stop']='UNKNOWN'
        with self.assertRaises(ValueError):route(self.data)
    def test_no_time_travel(self):
        self.data['trips'][0]['calls'][1]['arrival']='23:55:00'
        with self.assertRaises(ValueError):route(self.data)
    def test_input_permutation_does_not_change_itinerary(self):
        r=route(self.data);self.data['trips'].reverse();self.assertEqual(route(self.data),r)
    def test_cli_failure_preserves_output_and_success_matches(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);out=p/'report.json';bad=p/'bad.json';bad.write_text('{}');out.write_text('previous')
            args=[sys.executable,'-m','service_path.cli','--output',str(out),'--source']
            r=subprocess.run(args+[str(bad)],capture_output=True);self.assertEqual(r.returncode,2);self.assertEqual(out.read_text(),'previous')
            r=subprocess.run(args+[str(ROOT/'fixtures/timetable.json')],capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(out.read_text()),route(self.data))
if __name__=='__main__':unittest.main()
