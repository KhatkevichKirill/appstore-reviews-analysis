import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import ratings_report as rr  # noqa: E402

UTC = timezone.utc


def snap(at, count, average, country="US", version="5.5.12"):
    text = str(average)
    return rr.Snapshot(
        at=at,
        country=country,
        count=count,
        average=Decimal(text),
        decimals=len(text.split(".")[1]) if "." in text else 0,
        version=version,
    )


def rounded_average(total_stars, count, places=5):
    return (Decimal(total_stars) / Decimal(count)).quantize(Decimal(1).scaleb(-places))


def by_key(stats):
    return {(s.start.strftime("%Y-%m-%d"), s.country): s for s in stats}


class ImpliedMeanTest(unittest.TestCase):
    def test_mean_of_new_ratings_recovered_within_bound(self):
        # 1000 ratings averaging 4.5, then ten 1-star ratings arrive.
        a1 = rounded_average(4500, 1000)
        a2 = rounded_average(4510, 1010)
        stats = rr.compute(
            [
                snap(datetime(2026, 10, 1, 23, 7, tzinfo=UTC), 1000, a1),
                snap(datetime(2026, 10, 2, 23, 7, tzinfo=UTC), 1010, a2),
            ],
            [],
        )
        day = by_key(stats)[("2026-10-02", "US")]
        self.assertEqual(day.new_ratings, 10)
        self.assertEqual(day.new_average_error, Decimal("0.000005") * 2010 / 10)
        self.assertLessEqual(abs(day.new_average - 1), day.new_average_error)
        self.assertNotIn("inconsistent", day.flags)

    def test_fewer_decimals_widen_the_bound(self):
        stats = rr.compute(
            [
                snap(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 1000, "4.50"),
                snap(datetime(2026, 10, 2, 23, 0, tzinfo=UTC), 1010, "4.47"),
            ],
            [],
        )
        day = by_key(stats)[("2026-10-02", "US")]
        self.assertEqual(day.new_average_error, Decimal("0.005") * 2010 / 10)

    def test_impossible_mean_flagged_inconsistent(self):
        # +1 rating cannot lift a 1000-rating average by a whole star.
        stats = rr.compute(
            [
                snap(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 1000, "4.00000"),
                snap(datetime(2026, 10, 2, 23, 0, tzinfo=UTC), 1001, "4.90000"),
            ],
            [],
        )
        self.assertIn("inconsistent", by_key(stats)[("2026-10-02", "US")].flags)

    def test_no_new_ratings_gives_zero_and_no_mean(self):
        stats = rr.compute(
            [
                snap(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 1000, "4.50000"),
                snap(datetime(2026, 10, 2, 23, 0, tzinfo=UTC), 1000, "4.50000"),
            ],
            [],
        )
        day = by_key(stats)[("2026-10-02", "US")]
        self.assertEqual(day.new_ratings, 0)
        self.assertIsNone(day.new_average)


class PeriodTest(unittest.TestCase):
    def hourly(self, start, hours, first_count=1000, per_hour=1, country="US"):
        return [
            snap(start + timedelta(hours=h), first_count + h * per_hour, "4.50000", country=country)
            for h in range(hours)
        ]

    def test_day_uses_last_snapshot_at_or_before_each_midnight(self):
        # 00:07 on Oct 1 .. 23:07 on Oct 3, +1 rating per hour.
        snaps = self.hourly(datetime(2026, 10, 1, 0, 7, tzinfo=UTC), 72)
        days = by_key(rr.compute(snaps, []))
        oct2 = days[("2026-10-02", "US")]
        self.assertEqual(oct2.ratings_start, 1000 + 23)  # Oct 1 23:07
        self.assertEqual(oct2.ratings_end, 1000 + 47)  # Oct 2 23:07
        self.assertEqual(oct2.new_ratings, 24)
        self.assertEqual(oct2.flags, [])

    def test_first_and_last_periods_are_partial(self):
        snaps = self.hourly(datetime(2026, 10, 1, 0, 7, tzinfo=UTC), 60)
        days = by_key(rr.compute(snaps, []))
        self.assertIn("partial", days[("2026-10-01", "US")].flags)
        self.assertIn("partial", days[("2026-10-03", "US")].flags)
        self.assertNotIn("partial", days[("2026-10-02", "US")].flags)

    def test_week_starts_on_monday(self):
        # 2026-10-05 is a Monday.
        snaps = self.hourly(datetime(2026, 10, 1, 0, 7, tzinfo=UTC), 24 * 14)
        starts = {s.start.strftime("%Y-%m-%d") for s in rr.compute(snaps, [], period="week")}
        self.assertEqual(starts, {"2026-09-28", "2026-10-05", "2026-10-12"})

    def test_timezone_moves_boundaries(self):
        snaps = self.hourly(datetime(2026, 10, 1, 0, 7, tzinfo=UTC), 72)
        tz = ZoneInfo("America/Sao_Paulo")  # UTC-3
        days = by_key(rr.compute(snaps, [], tz=tz))
        oct2 = days[("2026-10-02", "US")]
        # Local midnight Oct 2 = 03:00 UTC, so the base is the 02:07 UTC snapshot.
        self.assertEqual(oct2.ratings_start, 1000 + 26)
        self.assertEqual(oct2.new_ratings, 24)

    def test_dst_change_keeps_midnight_boundaries(self):
        tz = ZoneInfo("Europe/London")  # clocks go back on 2026-10-25
        bounds = rr.period_bounds(
            datetime(2026, 10, 24, 12, tzinfo=UTC), datetime(2026, 10, 27, 12, tzinfo=UTC), "day", tz
        )
        for start, end in bounds:
            self.assertEqual((start.hour, end.astimezone(tz).hour), (0, 0))
        # Same-tzinfo subtraction is wall-clock in Python; compare real instants.
        start, end = (b.astimezone(UTC) for b in bounds[1])
        self.assertEqual(end - start, timedelta(hours=25))


class FlagsAndCombineTest(unittest.TestCase):
    def test_persistent_drop_flagged_once_it_outlives_the_window(self):
        # A summary-rating reset: 1000 ratings, then a fresh count from 40 that stays.
        stats = rr.compute(
            [
                snap(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 1000, "4.50000", "US"),
                snap(datetime(2026, 10, 2, 23, 0, tzinfo=UTC), 40, "3.10000", "US", version="5.6.0"),
                snap(datetime(2026, 10, 3, 23, 0, tzinfo=UTC), 41, "3.10000", "US", version="5.6.0"),
                snap(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 500, "4.60000", "GB"),
                snap(datetime(2026, 10, 2, 23, 0, tzinfo=UTC), 505, "4.59000", "GB"),
                snap(datetime(2026, 10, 3, 23, 0, tzinfo=UTC), 506, "4.59000", "GB"),
            ],
            [],
        )
        days = by_key(stats)
        # Within the 48 h window the drop looks like a stale cache answer: no change.
        self.assertEqual(days[("2026-10-02", "US")].new_ratings, 0)
        # Once the old count falls out of the window, the drop is real and flagged.
        us = days[("2026-10-03", "US")]
        self.assertIn("count_decreased", us.flags)
        self.assertIn("version_changed", us.flags)
        self.assertIsNone(us.new_average)
        total = days[("2026-10-03", rr.ALL)]
        self.assertIsNone(total.new_ratings)
        self.assertIsNone(total.star_only_estimate)
        self.assertIn("count_decreased", total.flags)

    def test_total_is_exact_combination_not_average_of_means(self):
        us1, us2 = rounded_average(45000, 10000), rounded_average(45000 + 100, 10100)  # 100 x 1★
        gb1, gb2 = rounded_average(4000, 1000), rounded_average(4000 + 50, 1010)  # 10 x 5★
        stats = rr.compute(
            [
                snap(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 10000, us1, "US"),
                snap(datetime(2026, 10, 2, 23, 0, tzinfo=UTC), 10100, us2, "US"),
                snap(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 1000, gb1, "GB"),
                snap(datetime(2026, 10, 2, 23, 0, tzinfo=UTC), 1010, gb2, "GB"),
            ],
            [],
        )
        total = by_key(stats)[("2026-10-02", rr.ALL)]
        self.assertEqual(total.new_ratings, 110)
        expected = Decimal(100 * 1 + 10 * 5) / 110
        self.assertLessEqual(abs(total.new_average - expected), total.new_average_error)

    def test_partial_first_period_counts_reviews_only_from_first_snapshot(self):
        snaps = [
            snap(datetime(2026, 10, 1, 16, 37, tzinfo=UTC), 1000, "4.50000"),
            snap(datetime(2026, 10, 1, 23, 7, tzinfo=UTC), 1005, "4.49000"),
            snap(datetime(2026, 10, 2, 0, 7, tzinfo=UTC), 1006, "4.49000"),
        ]
        reviews = [
            rr.Review(datetime(2026, 10, 1, 9, tzinfo=UTC), "US", 1),  # before collection started
            rr.Review(datetime(2026, 10, 1, 18, tzinfo=UTC), "US", 1),
        ]
        day = by_key(rr.compute(snaps, reviews))[("2026-10-01", "US")]
        self.assertIn("partial", day.flags)
        self.assertEqual(day.reviews, 1)

    def test_reviews_bucketed_by_period_country_and_star(self):
        snaps = [
            snap(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 1000, "4.50000"),
            snap(datetime(2026, 10, 2, 23, 0, tzinfo=UTC), 1020, "4.40000"),
        ]
        reviews = [
            rr.Review(datetime(2026, 10, 2, 5, tzinfo=UTC), "US", 1),
            rr.Review(datetime(2026, 10, 2, 6, tzinfo=UTC), "US", 1),
            rr.Review(datetime(2026, 10, 2, 7, tzinfo=UTC), "US", 5),
            rr.Review(datetime(2026, 10, 2, 7, tzinfo=UTC), "GB", 1),  # no GB snapshots
            rr.Review(datetime(2026, 10, 3, 1, tzinfo=UTC), "US", 1),  # next day
        ]
        day = by_key(rr.compute(snaps, reviews))[("2026-10-02", "US")]
        self.assertEqual(day.reviews_by_star, [2, 0, 0, 0, 1])
        self.assertEqual(day.star_only_estimate, 20 - 3)


class StaleCacheTest(unittest.TestCase):
    def test_stale_answers_neither_subtract_nor_double_count(self):
        # True count +10/day; every other hourly answer is a cache a day behind.
        start = datetime(2026, 10, 1, 0, 7, tzinfo=UTC)
        snaps = []
        for h in range(72):
            true = 1000 + 10 * ((h + 1) // 24)  # Apple updates once a day
            stale = 1000 + 10 * max(0, (h + 1) // 24 - 1)
            snaps.append(snap(start + timedelta(hours=h), stale if h % 2 else true, "4.50000"))
        days = by_key(rr.compute(snaps, []))
        self.assertEqual(days[("2026-10-02", "US")].new_ratings, 10)
        self.assertNotIn("count_decreased", days[("2026-10-02", "US")].flags)
        self.assertEqual(days[("2026-10-03", "US")].new_ratings, 10)

    def test_value_at_prefers_highest_recent_count(self):
        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        snaps = [snap(t0, 1000, "4.5"), snap(t0 + timedelta(hours=1), 1010, "4.4"),
                 snap(t0 + timedelta(hours=2), 1000, "4.5")]
        times = [s.at for s in snaps]
        self.assertEqual(rr.value_at(snaps, times, t0 + timedelta(hours=2), rr.DEFAULT_WINDOW).count, 1010)
        far = t0 + timedelta(hours=60)  # nothing in the window: fall back to the last answer
        self.assertEqual(rr.value_at(snaps, times, far, rr.DEFAULT_WINDOW).count, 1000)
        self.assertIsNone(rr.value_at(snaps, times, t0 - timedelta(hours=1), rr.DEFAULT_WINDOW))


class LoadingTest(unittest.TestCase):
    def test_spreadsheet_float_counts_accepted(self):
        rows = [{"snapshot_at": "2026-10-01T16:37:18.794Z", "country": "US",
                 "user_rating_count": "525034.0", "average_user_rating": "4.66287"},
                {"snapshot_at": "2026-10-01T17:07:00Z", "country": "US",
                 "user_rating_count": "525034.5", "average_user_rating": "4.66287"}]
        with contextlib.redirect_stderr(io.StringIO()):
            snaps = rr.load_snapshots(rows)
        self.assertEqual([s.count for s in snaps], [525034])

    def test_trailing_zero_decimals_kept_and_bad_rows_skipped(self):
        rows = [
            {"snapshot_at": "2026-10-01T12:07:00.000Z", "country": "us", "user_rating_count": "1,234",
             "average_user_rating": "4.70000", "app_version": "5.5.12"},
            {"snapshot_at": "2026-10-01T13:07:00.000Z", "country": "US", "user_rating_count": "",
             "average_user_rating": "4.7"},
            {"snapshot_at": "2026-10-01T14:07:00.000Z", "country": "US", "user_rating_count": "x",
             "average_user_rating": "4.7"},
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            snaps = rr.load_snapshots(rows)
        self.assertEqual(len(snaps), 1)
        self.assertEqual((snaps[0].country, snaps[0].count, snaps[0].decimals), ("US", 1234, 5))
        self.assertEqual(snaps[0].at, datetime(2026, 10, 1, 12, 7, tzinfo=UTC))

    def test_raw_lookup_double_expansion_reads_as_five_decimals(self):
        # Verbatim from the US Lookup response of 2026-10-01.
        rows = [{"snapshot_at": "2026-10-01T15:00:00Z", "country": "US", "user_rating_count": "525034",
                 "average_user_rating": "4.6628699999999998482280716416426002979278564453125"}]
        snap = rr.load_snapshots(rows)[0]
        self.assertEqual((snap.average, snap.decimals), (Decimal("4.66287"), 5))

    def test_review_dates_with_offsets_normalised_to_utc(self):
        rows = [{"date": "2026-09-29T17:42:23-07:00", "country": "US", "score": "1"}]
        self.assertEqual(rr.load_reviews(rows)[0].at, datetime(2026, 9, 30, 0, 42, 23, tzinfo=UTC))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.snapshots = os.path.join(self.dir.name, "snapshots.csv")
        self.reviews = os.path.join(self.dir.name, "reviews.csv")
        start = datetime(2026, 10, 1, 0, 7, tzinfo=UTC)
        with open(self.snapshots, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["snapshot_at", "country", "app_id", "app_version", "user_rating_count", "average_user_rating"])
            for h in range(72):
                t = (start + timedelta(hours=h)).isoformat().replace("+00:00", "Z")
                w.writerow([t, "US", "1209815023", "5.5.12", 1000 + (h // 6), "4.50000"])
        with open(self.reviews, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "date", "country", "score"])
            w.writerow(["1", "2026-10-02T10:00:00-07:00", "US", "1"])

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rr.main(list(args))
        return code, out.getvalue()

    def test_csv_output(self):
        code, out = self.run_main(self.snapshots, "--reviews", self.reviews, "--format", "csv")
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(io.StringIO(out)))
        self.assertEqual(list(rows[0].keys()), rr.COLUMNS)
        oct2 = next(r for r in rows if r["period_start"] == "2026-10-02")
        self.assertEqual((oct2["new_ratings"], oct2["written_reviews"], oct2["reviews_1"]), ("4", "1", "1"))

    def test_table_output(self):
        code, out = self.run_main(self.snapshots)
        self.assertEqual(code, 0)
        self.assertIn("2026-10-02", out)

    def test_diagnose(self):
        code, out = self.run_main(self.snapshots, "--diagnose")
        self.assertEqual(code, 0)
        self.assertIn("US: 72 snapshots", out)
        self.assertIn("average decimals seen: max 5", out)
        self.assertIn("longest gap between snapshots: 1.0 h", out)
        self.assertIn("stale answers: 0/72", out)
        self.assertIn("freshest count rose 11 times; hours between rises: median 6.0", out)

    def test_missing_column_is_a_clear_error(self):
        bad = os.path.join(self.dir.name, "bad.csv")
        with open(bad, "w") as f:
            f.write("snapshot_at,country\n2026-10-01T00:00:00Z,US\n")
        with self.assertRaises(SystemExit) as ctx:
            rr.main([bad])
        self.assertIn("user_rating_count", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
