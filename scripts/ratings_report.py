#!/usr/bin/env python3
"""Per-period App Store rating stats from aggregate snapshots.

Apple never publishes a star-only rating as an individual record: the public
Lookup API gives, per storefront, only the running total (`userRatingCount`) and
the running average (`averageUserRating`). Diffing two snapshots of that pair
gives, for the period between them:

* new ratings      = N2 - N1
* their mean star  = (N2*A2 - N1*A1) / (N2 - N1)

The second number is only as good as the decimals Apple returns, so it comes
with a worst-case error bound: e * (N1 + N2) / (N2 - N1), where e is half a unit
in the last decimal place of the average.

Input is a CSV export of the `ratings_snapshots` sheet tab written by
n8n/app-store-ratings-snapshot.json; optionally also a CSV export of the reviews
sheet, to put written reviews next to the rating counts.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

ALL = "ALL"
SNAPSHOT_COLUMNS = {"snapshot_at", "country", "user_rating_count", "average_user_rating"}
REVIEW_COLUMNS = {"date", "country", "score"}


@dataclass(frozen=True)
class Snapshot:
    at: datetime  # aware, UTC
    country: str
    count: int
    average: Decimal
    decimals: int  # decimals in the average as Apple returned it
    version: str


@dataclass(frozen=True)
class Review:
    at: datetime  # aware, UTC
    country: str
    score: int


@dataclass
class PeriodStats:
    start: datetime
    country: str
    ratings_start: int | None = None
    ratings_end: int | None = None
    average_end: Decimal | None = None
    new_ratings: int | None = None
    new_average: Decimal | None = None
    new_average_error: Decimal | None = None
    reviews_by_star: list[int] = field(default_factory=lambda: [0] * 5)
    flags: list[str] = field(default_factory=list)
    # Kept so the ALL row can be combined exactly rather than averaged.
    weighted_delta: Decimal | None = None  # N2*A2 - N1*A1
    error_numerator: Decimal | None = None  # e * (N1 + N2)

    @property
    def reviews(self) -> int:
        return sum(self.reviews_by_star)

    @property
    def star_only_estimate(self) -> int | None:
        if self.new_ratings is None:
            return None
        return self.new_ratings - self.reviews


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def half_unit(decimals: int) -> Decimal:
    """Half a unit in the last place: the max rounding error of the average."""
    return Decimal(5).scaleb(-(decimals + 1))


def normalise_average(text: str) -> str:
    """The average as Apple stores it, so its decimals give the real precision.

    The raw Lookup response prints the double's full binary expansion:
    4.66287 arrives as 4.6628699999999998482280716416426002979278564453125.
    Taken literally that is 49 decimals and a bogus error bound of ~0. When the
    text differs from the double's shortest form it is such an expansion, so use
    the short form; otherwise keep the text (trailing zeros are real precision).
    """
    shortest = repr(float(text))
    return text if Decimal(text) == Decimal(shortest) else shortest


def require_columns(reader: csv.DictReader, required: set[str], what: str) -> None:
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise SystemExit(f"{what}: missing columns {', '.join(sorted(missing))}")


def load_snapshots(rows: Iterable[dict]) -> list[Snapshot]:
    snapshots = []
    for line, row in enumerate(rows, start=2):
        count_raw = (row.get("user_rating_count") or "").replace(",", "").strip()
        average_raw = (row.get("average_user_rating") or "").strip()
        if not count_raw or not average_raw:
            print(f"snapshots line {line}: no rating count/average, skipped", file=sys.stderr)
            continue
        try:
            average_text = normalise_average(average_raw)
            average = Decimal(average_text)
            count = int(count_raw)
        except (InvalidOperation, ValueError):
            print(f"snapshots line {line}: unparseable rating values, skipped", file=sys.stderr)
            continue
        decimals = len(average_text.split(".", 1)[1]) if "." in average_text else 0
        snapshots.append(
            Snapshot(
                at=parse_time(row["snapshot_at"]),
                country=row["country"].strip().upper(),
                count=count,
                average=average,
                decimals=decimals,
                version=(row.get("app_version") or "").strip(),
            )
        )
    return sorted(snapshots, key=lambda s: (s.country, s.at))


def load_reviews(rows: Iterable[dict]) -> list[Review]:
    reviews = []
    for line, row in enumerate(rows, start=2):
        try:
            score = int(float(row["score"]))
            at = parse_time(row["date"])
        except (KeyError, TypeError, ValueError):
            print(f"reviews line {line}: no usable date/score, skipped", file=sys.stderr)
            continue
        if not 1 <= score <= 5:
            continue
        reviews.append(Review(at=at, country=(row.get("country") or "").strip().upper(), score=score))
    return reviews


def period_start(moment: datetime, period: str, tz: ZoneInfo) -> datetime:
    local = moment.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        local -= timedelta(days=local.weekday())
    return local


def period_bounds(first: datetime, last: datetime, period: str, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    step = timedelta(days=7 if period == "week" else 1)
    start = period_start(first, period, tz)
    bounds = []
    while start <= last:
        # Re-anchor at local midnight so DST changes don't drift the boundary.
        end = period_start(start + step + timedelta(hours=12), period, tz)
        bounds.append((start, end))
        start = end
    return bounds


def _country_stats(
    snaps: Sequence[Snapshot],
    times: Sequence[datetime],
    start: datetime,
    end: datetime,
    error_per_rating: Decimal,
) -> PeriodStats:
    stats = PeriodStats(start=start, country=snaps[0].country)

    i = bisect_right(times, start)
    base = snaps[i - 1] if i else None
    if base is None:
        j = bisect_left(times, start)
        base = snaps[j] if j < len(snaps) and snaps[j].at < end else None
        stats.flags.append("partial")
    i = bisect_right(times, end)
    last = snaps[i - 1] if i else None
    if end > snaps[-1].at and "partial" not in stats.flags:
        stats.flags.append("partial")
    if base is None or last is None or last.at <= base.at:
        stats.flags.append("no_data")
        return stats

    stats.ratings_start = base.count
    stats.ratings_end = last.count
    stats.average_end = last.average
    stats.new_ratings = last.count - base.count
    if base.version != last.version:
        stats.flags.append("version_changed")

    if stats.new_ratings < 0:
        # A summary-rating reset or Apple removing ratings; the delta means nothing.
        stats.flags.append("count_decreased")
        return stats

    stats.weighted_delta = last.count * last.average - base.count * base.average
    stats.error_numerator = error_per_rating * (base.count + last.count)
    if stats.new_ratings > 0:
        stats.new_average = stats.weighted_delta / stats.new_ratings
        stats.new_average_error = stats.error_numerator / stats.new_ratings
        if not (1 - stats.new_average_error <= stats.new_average <= 5 + stats.new_average_error):
            # Count and average out of step (e.g. served from different caches).
            stats.flags.append("inconsistent")
    return stats


def _combine(start: datetime, rows: list[PeriodStats]) -> PeriodStats:
    combined = PeriodStats(start=start, country=ALL)
    usable = [r for r in rows if r.weighted_delta is not None]
    for r in rows:
        for i, n in enumerate(r.reviews_by_star):
            combined.reviews_by_star[i] += n
        combined.flags.extend(f for f in r.flags if f not in combined.flags)
    if not usable or len(usable) != len(rows):
        if "no_data" not in combined.flags:
            combined.flags.append("no_data")
        return combined

    combined.ratings_start = sum(r.ratings_start for r in usable)
    combined.ratings_end = sum(r.ratings_end for r in usable)
    combined.new_ratings = sum(r.new_ratings for r in usable)
    if combined.new_ratings > 0:
        combined.new_average = sum(r.weighted_delta for r in usable) / combined.new_ratings
        combined.new_average_error = sum(r.error_numerator for r in usable) / combined.new_ratings
    return combined


def compute(
    snapshots: Sequence[Snapshot],
    reviews: Sequence[Review],
    period: str = "day",
    tz: ZoneInfo | None = None,
) -> list[PeriodStats]:
    tz = tz or ZoneInfo("UTC")
    if not snapshots:
        return []

    by_country: dict[str, list[Snapshot]] = {}
    for s in snapshots:
        by_country.setdefault(s.country, []).append(s)
    for snaps in by_country.values():
        snaps.sort(key=lambda s: s.at)
    times = {c: [s.at for s in snaps] for c, snaps in by_country.items()}
    # The decimals Apple uses are stable; the max seen is the true precision
    # (a shorter string is just trailing zeros that JSON dropped).
    error_per_rating = {c: half_unit(max(s.decimals for s in snaps)) for c, snaps in by_country.items()}

    first = min(s.at for s in snapshots)
    last = max(s.at for s in snapshots)
    results: list[PeriodStats] = []
    for start, end in period_bounds(first, last, period, tz):
        rows = []
        for country in sorted(by_country):
            stats = _country_stats(by_country[country], times[country], start, end, error_per_rating[country])
            for r in reviews:
                if r.country == country and start <= r.at < end:
                    stats.reviews_by_star[r.score - 1] += 1
            rows.append(stats)
        results.extend(rows)
        if len(rows) > 1:
            results.append(_combine(start, rows))
    return results


def diagnose(snapshots: Sequence[Snapshot]) -> list[str]:
    """How often Apple's numbers actually move, and how precise they are."""
    lines = []
    by_country: dict[str, list[Snapshot]] = {}
    for s in snapshots:
        by_country.setdefault(s.country, []).append(s)
    for country in sorted(by_country):
        snaps = sorted(by_country[country], key=lambda s: s.at)
        lines.append(f"{country}: {len(snaps)} snapshots, {snaps[0].at:%Y-%m-%d %H:%M} .. {snaps[-1].at:%Y-%m-%d %H:%M} UTC")
        lines.append(f"  average decimals seen: max {max(s.decimals for s in snaps)}")
        if len(snaps) > 1:
            gap = max((b.at - a.at).total_seconds() / 3600 for a, b in zip(snaps, snaps[1:]))
            lines.append(f"  longest gap between snapshots: {gap:.1f} h")

        changes = [b for a, b in zip(snaps, snaps[1:]) if b.count != a.count]
        if len(changes) >= 2:
            gaps = [(b.at - a.at).total_seconds() / 3600 for a, b in zip(changes, changes[1:])]
            lines.append(
                f"  count changed {len(changes)} times; hours between changes: "
                f"median {median(gaps):.1f}, min {min(gaps):.1f}, max {max(gaps):.1f}"
            )
        else:
            lines.append(f"  count changed {len(changes)} times")

        for a, b in zip(snaps, snaps[1:]):
            if b.count < a.count:
                lines.append(f"  DECREASE {a.count} -> {b.count} at {b.at:%Y-%m-%d %H:%M} UTC")
            if b.version != a.version:
                lines.append(f"  version {a.version or '?'} -> {b.version or '?'} at {b.at:%Y-%m-%d %H:%M} UTC")

        dupes = [at for at, n in Counter(s.at for s in snaps).items() if n > 1]
        if dupes:
            lines.append(f"  {len(dupes)} duplicate snapshot timestamps")
    return lines


COLUMNS = [
    "period_start", "country", "ratings_start", "ratings_end", "new_ratings",
    "average_end", "new_ratings_mean", "new_ratings_mean_error",
    "written_reviews", "reviews_1", "reviews_2", "reviews_3", "reviews_4", "reviews_5",
    "star_only_estimate", "flags",
]


def _fmt(value, places: int | None = None) -> str:
    if value is None:
        return ""
    if places is not None:
        return f"{value:.{places}f}"
    return str(value)


def as_row(s: PeriodStats) -> list[str]:
    return [
        s.start.strftime("%Y-%m-%d"),
        s.country,
        _fmt(s.ratings_start),
        _fmt(s.ratings_end),
        _fmt(s.new_ratings),
        _fmt(s.average_end),
        _fmt(s.new_average, 2),
        _fmt(s.new_average_error, 2),
        str(s.reviews),
        *[str(n) for n in s.reviews_by_star],
        _fmt(s.star_only_estimate),
        ";".join(s.flags),
    ]


def print_table(stats: Sequence[PeriodStats], out) -> None:
    header = ["period", "country", "new", "mean★ (±err)", "reviews", "1★/2★/3★/4★/5★", "star-only≈", "flags"]
    rows = []
    for s in stats:
        mean = "" if s.new_average is None else f"{s.new_average:.2f} (±{s.new_average_error:.2f})"
        rows.append([
            s.start.strftime("%Y-%m-%d"),
            s.country,
            _fmt(s.new_ratings),
            mean,
            str(s.reviews),
            "/".join(str(n) for n in s.reviews_by_star),
            _fmt(s.star_only_estimate),
            ",".join(s.flags),
        ])
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for r in [header, *rows]:
        print("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip(), file=out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("snapshots", help="CSV export of the ratings_snapshots tab")
    parser.add_argument("--reviews", help="CSV export of the reviews tab (date, country, score)")
    parser.add_argument("--period", choices=["day", "week"], default="day")
    parser.add_argument("--tz", default="UTC", help="IANA zone for period boundaries, e.g. America/Sao_Paulo")
    parser.add_argument("--format", choices=["table", "csv"], default="table")
    parser.add_argument("--diagnose", action="store_true", help="report update cadence and precision instead")
    args = parser.parse_args(argv)

    with open(args.snapshots, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        require_columns(reader, SNAPSHOT_COLUMNS, args.snapshots)
        snapshots = load_snapshots(reader)
    if not snapshots:
        print("no usable snapshots", file=sys.stderr)
        return 1

    if args.diagnose:
        print("\n".join(diagnose(snapshots)))
        return 0

    reviews: list[Review] = []
    if args.reviews:
        with open(args.reviews, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            require_columns(reader, REVIEW_COLUMNS, args.reviews)
            reviews = load_reviews(reader)

    stats = compute(snapshots, reviews, args.period, ZoneInfo(args.tz))
    if args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(COLUMNS)
        writer.writerows(as_row(s) for s in stats)
    else:
        print_table(stats, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
