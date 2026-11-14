import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from service_path.supply import build, build_supply, write_database
from service_path.timetable import snapshot

ROOT = Path(__file__).resolve().parents[1]
SUPPLY = ROOT / 'fixtures' / 'supply' / 'timetable.json'


def load_snapshot():
    return snapshot(json.loads(SUPPLY.read_text()))


def fact_keys(built):
    return {(f[0], f[1], f[2]) for f in built['facts']}


def hourly_for(built, natural_day, stop_id):
    return {row[2]: (row[3], row[4]) for row in built['hourly']
            if row[0] == natural_day and row[1] == stop_id}


class SupplyFixtureTests(unittest.TestCase):
    def setUp(self):
        self.snap = load_snapshot()

    def test_only_non_terminal_calls_produce_departures(self):
        built = build(self.snap, date(2026, 1, 1), date(2026, 1, 5))
        # Terminals: T1 call 2 (C), T2 call 1 (D), T3 call 2 (D), T4 call 1 (A),
        # W1 call 1 (C) never appear as facts.  daily is removed on 2026-01-03
        # (Saturday); weekend runs on 01-03 and 01-04.
        self.assertEqual(fact_keys(built), {
            ('2026-01-01', 'T1', 0), ('2026-01-01', 'T1', 1),
            ('2026-01-01', 'T2', 0), ('2026-01-01', 'T3', 0),
            ('2026-01-01', 'T3', 1), ('2026-01-01', 'T4', 0),
            ('2026-01-02', 'T1', 0), ('2026-01-02', 'T1', 1),
            ('2026-01-02', 'T2', 0), ('2026-01-02', 'T3', 0),
            ('2026-01-02', 'T3', 1), ('2026-01-02', 'T4', 0),
            ('2026-01-03', 'W1', 0),
            ('2026-01-04', 'T1', 0), ('2026-01-04', 'T1', 1),
            ('2026-01-04', 'T2', 0), ('2026-01-04', 'T3', 0),
            ('2026-01-04', 'T3', 1), ('2026-01-04', 'T4', 0),
            ('2026-01-04', 'W1', 0),
            ('2026-01-05', 'T1', 0), ('2026-01-05', 'T1', 1),
            ('2026-01-05', 'T2', 0), ('2026-01-05', 'T3', 0),
        })

    def test_removed_service_day_keeps_only_weekend_and_carry_over(self):
        built = build(self.snap, date(2026, 1, 3), date(2026, 1, 3))
        self.assertEqual([(f[0], f[1], f[4]) for f in built['facts']],
                         [('2026-01-02', 'T3', 'C'), ('2026-01-02', 'T4', 'D'),
                          ('2026-01-03', 'W1', 'B')])
        # daily runs on 2026-01-02 but not 2026-01-03, so no daily fact is
        # emitted for the removed service day itself.
        self.assertNotIn(('2026-01-03', 'T1', 0), fact_keys(built))

    def test_added_and_removed_exceptions_across_range(self):
        # Added date outside the calendar's regular range still runs.
        built = build(self.snap, date(2026, 2, 7), date(2026, 2, 7))
        self.assertEqual([(f[0], f[1], f[4]) for f in built['facts']],
                         [('2026-02-07', 'W1', 'B')])
        # The removed 2026-01-03 is a Saturday: weekend still runs, daily does not.
        built = build(self.snap, date(2026, 1, 3), date(2026, 1, 4))
        self.assertNotIn(('2026-01-03', 'T1', 0), fact_keys(built))
        self.assertIn(('2026-01-04', 'T1', 0), fact_keys(built))

    def test_service_time_after_midnight_lands_on_next_natural_day(self):
        built = build(self.snap, date(2026, 1, 1), date(2026, 1, 2))
        facts = {f[:3]: f for f in built['facts']}
        # T3 departs C at 24:10 service time on service day 2026-01-01.
        self.assertEqual(facts[('2026-01-01', 'T3', 1)][3:],
                         ('2026-01-02', 'C', 600))
        # T4 departs D exactly at 24:00 on service day 2026-01-01.
        self.assertEqual(facts[('2026-01-01', 'T4', 0)][3:],
                         ('2026-01-02', 'D', 0))

    def test_previous_service_day_is_included_only_for_window_start(self):
        # Natural day 2026-01-02 must include the call that left on service day
        # 2026-01-01 after midnight, but not its same-day calls.
        built = build(self.snap, date(2026, 1, 2), date(2026, 1, 2))
        keys = fact_keys(built)
        self.assertIn(('2026-01-01', 'T3', 1), keys)
        self.assertIn(('2026-01-01', 'T4', 0), keys)
        self.assertNotIn(('2026-01-01', 'T3', 0), keys)
        # Calls of service day 2026-01-02 that spill into 2026-01-03 are out.
        self.assertNotIn(('2026-01-02', 'T3', 1), keys)
        self.assertNotIn(('2026-01-02', 'T4', 0), keys)

    def test_window_end_includes_same_service_day_calls(self):
        built = build(self.snap, date(2026, 1, 2), date(2026, 1, 2))
        keys = fact_keys(built)
        self.assertIn(('2026-01-02', 'T1', 0), keys)
        self.assertIn(('2026-01-02', 'T3', 0), keys)
        self.assertIn(('2026-01-02', 'T2', 0), keys)

    def test_query_date_does_not_limit_results(self):
        # The snapshot query points at 2026-01-02; the window is 01-04..01-05.
        built = build(self.snap, date(2026, 1, 4), date(2026, 1, 5))
        # 2026-01-03 is a removed daily service day and its weekend trip stays
        # on 01-03, so only the two requested natural days carry facts.
        self.assertEqual({f[0] for f in built['facts']},
                         {'2026-01-04', '2026-01-05'})
        self.assertEqual({f[3] for f in built['facts']},
                         {'2026-01-04', '2026-01-05'})

    def test_every_stop_has_all_24_hours(self):
        built = build(self.snap, date(2026, 1, 1), date(2026, 1, 2))
        self.assertEqual(len(built['hourly']), 2 * 4 * 24)
        for natural_day in ('2026-01-01', '2026-01-02'):
            for stop in ('A', 'B', 'C', 'D'):
                self.assertEqual(sorted(hourly_for(built, natural_day, stop)),
                                 list(range(24)))

    def test_same_second_vehicles_count_but_are_deduplicated(self):
        built = build(self.snap, date(2026, 1, 2), date(2026, 1, 2))
        a = hourly_for(built, '2026-01-02', 'A')
        # T1 and T2 both leave A at 08:00:00; T4 leaves A at 24:20 (out of window).
        self.assertEqual(a[8][0], 2)
        # One distinct instant in hour 8: gaps are 08:00-08:00 boundary = 2880 s
        # and 09:00-08:00 boundary = 3600 s, so the maximum is 3600.
        self.assertEqual(a[8][1], 3600)

    def test_empty_hours_are_3600(self):
        built = build(self.snap, date(2026, 1, 2), date(2026, 1, 2))
        a = hourly_for(built, '2026-01-02', 'A')
        for hour in (0, 1, 7, 9, 12, 23):
            self.assertEqual(a[hour], (0, 3600))

    def test_headway_uses_deduplicated_instants_and_hour_boundaries(self):
        built = build(self.snap, date(2026, 1, 2), date(2026, 1, 2))
        b = hourly_for(built, '2026-01-02', 'B')
        # B hour 8: T1 departs at 08:22:00 (30120 s).  Points 28800, 30120,
        # 32400 give gaps 1320 and 2280, so the maximum is 2280.
        self.assertEqual(b[8], (1, 2280))
        # B hour 23: T3 departs at 23:50:00 (85800 s).  Points 82800, 85800,
        # 86400 give gaps 3000 and 600, so the maximum is 3000.
        self.assertEqual(b[23], (1, 3000))

    def test_right_boundary_departure_belongs_to_next_hour(self):
        built = build(self.snap, date(2026, 1, 1), date(2026, 1, 3))
        # T4 leaves D exactly at 24:00:00 on service day 2026-01-02, i.e.
        # 2026-01-03T00:00:00, hour 0 of the next natural day.
        self.assertEqual(hourly_for(built, '2026-01-03', 'D')[0], (1, 3600))
        self.assertEqual(hourly_for(built, '2026-01-03', 'D')[23], (0, 3600))
        # T3 leaves C at 24:10:00, i.e. 2026-01-03T00:10:00, hour 0.
        self.assertEqual(hourly_for(built, '2026-01-03', 'C')[0], (1, 3000))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'mart.sqlite'
        self.snap = load_snapshot()

    def rows(self, sql, args=(), db=None):
        connection = sqlite3.connect(self.db if db is None else db)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def publish(self, start, end, snap=None, db=None):
        snap = self.snap if snap is None else snap
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        built = build(snap, start_day, end_day)
        write_database(self.db if db is None else db, built, start_day, end_day)
        return built

    def test_fact_primary_key_is_unique(self):
        self.publish('2026-01-01', '2026-01-02')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM departures')[0][0],
                         self.rows('SELECT COUNT(*) FROM (SELECT DISTINCT service_day,'
                                   ' trip_id, call_index FROM departures)')[0][0])
        connection = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO departures VALUES"
                                   " ('2026-01-01', 'T1', 0, '2026-01-01', 'A', 0)")
        finally:
            connection.close()

    def test_rerun_replaces_only_requested_partition(self):
        self.publish('2026-01-01', '2026-01-05')
        before = self.rows('SELECT * FROM departures ORDER BY 1, 2, 3')
        other_before = [row for row in before if row[3] != '2026-01-02']
        # Move T1's A departure one minute later, then rerun natural day 01-02.
        snap = load_snapshot()
        snap['trips']['T1']['calls'][0] = ('A', 8 * 3600 + 60, 8 * 3600 + 60)
        self.publish('2026-01-02', '2026-01-02', snap)
        after = self.rows('SELECT * FROM departures ORDER BY 1, 2, 3')
        self.assertNotEqual(before, after)
        self.assertEqual([row for row in after if row[3] != '2026-01-02'],
                         other_before)
        self.assertIn(('2026-01-02', 'T1', 0, '2026-01-02', 'A', 28860), after)
        self.assertNotIn(('2026-01-02', 'T1', 0, '2026-01-02', 'A', 28800), after)

    def test_rerun_replaces_summary_partition(self):
        self.publish('2026-01-01', '2026-01-05')
        other_before = self.rows(
            "SELECT * FROM hourly_departures WHERE natural_day <> '2026-01-02'"
            ' ORDER BY 1, 2, 3')
        snap = load_snapshot()
        snap['stops']['E'] = 0  # a stop with no service only in the new partition
        self.publish('2026-01-02', '2026-01-02', snap)
        other_after = self.rows(
            "SELECT * FROM hourly_departures WHERE natural_day <> '2026-01-02'"
            ' ORDER BY 1, 2, 3')
        self.assertEqual(other_before, other_after)
        self.assertEqual(len(self.rows("SELECT * FROM hourly_departures"
                                       " WHERE natural_day = '2026-01-02'"
                                       " AND stop_id = 'E'")), 24)
        self.assertEqual(len(self.rows("SELECT * FROM hourly_departures"
                                       " WHERE natural_day = '2026-01-01'")),
                         4 * 24)

    def test_bad_snapshot_leaves_existing_database_untouched(self):
        self.publish('2026-01-01', '2026-01-05')
        before = self.db.read_bytes()
        bad = self.root / 'bad.json'
        bad.write_text('{"stops": []}')
        with self.assertRaises(ValueError):
            build_supply(bad, '2026-01-02', '2026-01-02', self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_write_failure_rolls_back_both_tables(self):
        self.publish('2026-01-01', '2026-01-05')
        before = self.rows('SELECT * FROM departures ORDER BY 1, 2, 3')
        before_hourly = self.rows('SELECT * FROM hourly_departures ORDER BY 1, 2, 3')
        built = build(self.snap, date(2026, 1, 2), date(2026, 1, 2))
        # A duplicate primary key fails the fact insert after the partition has
        # already been deleted inside the same transaction.
        broken = {'facts': list(built['facts']) + [built['facts'][0]],
                  'hourly': built['hourly']}
        with self.assertRaises(ValueError):
            write_database(self.db, broken, date(2026, 1, 2), date(2026, 1, 2))
        self.assertEqual(self.rows('SELECT * FROM departures ORDER BY 1, 2, 3'), before)
        self.assertEqual(self.rows('SELECT * FROM hourly_departures ORDER BY 1, 2, 3'),
                         before_hourly)

    def test_failed_first_write_does_not_leave_a_database(self):
        built = build(self.snap, date(2026, 1, 2), date(2026, 1, 2))
        broken = {'facts': list(built['facts']) + [built['facts'][0]],
                  'hourly': built['hourly']}
        with self.assertRaises(ValueError):
            write_database(self.db, broken, date(2026, 1, 2), date(2026, 1, 2))
        self.assertFalse(self.db.exists())

    def test_schema_mismatch_is_rejected_without_modification(self):
        connection = sqlite3.connect(self.db)
        connection.execute('CREATE TABLE departures (wrong TEXT)')
        connection.commit()
        connection.close()
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            self.publish('2026-01-01', '2026-01-01')
        self.assertEqual(self.db.read_bytes(), before)

    def test_input_array_order_does_not_change_logical_result(self):
        self.publish('2026-01-01', '2026-01-03')
        facts_before = self.rows('SELECT * FROM departures ORDER BY 1, 2, 3')
        hourly_before = self.rows('SELECT * FROM hourly_departures ORDER BY 1, 2, 3')
        data = json.loads(SUPPLY.read_text())
        for key in ('stops', 'calendars', 'trips'):
            data[key].reverse()
        for calendar in data['calendars']:
            calendar['weekdays'].reverse()
            calendar['added'].reverse()
            calendar['removed'].reverse()
        permuted = snapshot(data)
        other_db = self.root / 'permuted.sqlite'
        self.publish('2026-01-01', '2026-01-03', permuted, other_db)
        facts_after = self.rows('SELECT * FROM departures ORDER BY 1, 2, 3', db=other_db)
        hourly_after = self.rows('SELECT * FROM hourly_departures ORDER BY 1, 2, 3',
                                 db=other_db)
        self.assertEqual(facts_before, facts_after)
        self.assertEqual(hourly_before, hourly_after)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'mart.sqlite'

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, '-m', 'service_path.supply_cli', *map(str, args)],
            capture_output=True, text=True)

    def test_cli_builds_mart_and_reports_counts(self):
        result = self.run_cli('--source', SUPPLY, '--start', '2026-01-01',
                              '--end', '2026-01-05', '--database', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['natural_days'], 5)
        self.assertEqual(report['service_days'], 6)
        self.assertEqual(report['stations'], 4)
        self.assertEqual(report['hourly_rows'], 480)
        self.assertEqual(report['departures'],
                         len(self.rows('SELECT * FROM departures')))

    def test_cli_bad_snapshot_exits_2_and_keeps_database(self):
        ok = self.run_cli('--source', SUPPLY, '--start', '2026-01-01',
                          '--end', '2026-01-02', '--database', self.db)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        before = self.db.read_bytes()
        bad = self.root / 'bad.json'
        bad.write_text('{"stops": [], "calendars": [], "trips": [], "query": {}}')
        result = self.run_cli('--source', bad, '--start', '2026-01-01',
                              '--end', '2026-01-02', '--database', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertIn('expected fields', result.stderr)
        self.assertEqual(self.db.read_bytes(), before)

    def test_cli_rejects_reversed_window_and_missing_arguments(self):
        result = self.run_cli('--source', SUPPLY, '--start', '2026-01-05',
                              '--end', '2026-01-01', '--database', self.db)
        self.assertEqual(result.returncode, 2)
        self.assertIn('start date is later than end date', result.stderr)
        self.assertFalse(self.db.exists())
        result = self.run_cli('--source', SUPPLY, '--database', self.db)
        self.assertEqual(result.returncode, 2)

    def test_cli_source_must_differ_from_database(self):
        result = self.run_cli('--source', SUPPLY, '--start', '2026-01-01',
                              '--end', '2026-01-01', '--database', SUPPLY)
        self.assertEqual(result.returncode, 2)
        self.assertIn('source and database must differ', result.stderr)

    def test_cli_uses_the_imported_gtfs_snapshot(self):
        snapshot_path = self.root / 'gtfs.json'
        imported = subprocess.run(
            [sys.executable, '-m', 'service_path.import_cli',
             '--source', str(ROOT / 'fixtures' / 'gtfs' / 'sample'),
             '--query', str(ROOT / 'fixtures' / 'gtfs' / 'query.json'),
             '--output', str(snapshot_path)], capture_output=True, text=True)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        result = self.run_cli('--source', snapshot_path, '--start', '2026-01-02',
                              '--end', '2026-01-02', '--database', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.rows('SELECT * FROM departures ORDER BY 1, 2, 3')
        # N1 call 0/1, N2 call 0, N3 call 0 (service day 2026-01-01) and
        # H1 call 0 (service day 2026-01-02); every terminal call is excluded.
        self.assertEqual(rows, [
            ('2026-01-01', 'N1', 1, '2026-01-02', 'B', 360),
            ('2026-01-01', 'N2', 0, '2026-01-02', 'B', 480),
            ('2026-01-01', 'N3', 0, '2026-01-02', 'A', 0),
            ('2026-01-02', 'H1', 0, '2026-01-02', 'A', 600),
            ('2026-01-02', 'N1', 0, '2026-01-02', 'A', 86100),
        ])

    def rows(self, sql):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()


class PackagingTests(unittest.TestCase):
    def test_supply_entry_point_is_declared(self):
        text = (ROOT / 'pyproject.toml').read_text()
        self.assertIn('service-path-supply = "service_path.supply_cli:main"', text)


if __name__ == '__main__':
    unittest.main()
