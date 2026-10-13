#!/usr/bin/env python3
"""Generate deterministic fictional data. Never reads production data or credentials."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

SNAPSHOT_HEADERS = [
    "snapshot_at", "country", "app_id", "app_version",
    "current_version_release_date", "user_rating_count", "average_user_rating",
    "user_rating_count_current_version", "average_user_rating_current_version",
]
REVIEW_HEADERS = ["id", "date", "country", "score", "title", "content"]


def generate(destination):
    destination.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 9, 21, tzinfo=timezone.utc)
    stop = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    countries = ["US", "GB", "CA", "AU", "BR", "IN"]
    snapshots = []
    for hour in range(int((stop - start).total_seconds() / 3600) + 1):
        at = start + timedelta(hours=hour)
        day = hour // 24
        for index, country in enumerate(countries):
            if country in ("BR", "IN") and at < datetime(2026, 10, 7, 12, tzinfo=timezone.utc):
                continue
            baseline = 10000 + index * 2000
            daily = 24 + index * 6
            previous = min(day, 14) * daily
            current = max(day - 14, 0) * daily * 2
            count = baseline + previous + current
            average = (Decimal(baseline) * Decimal("4.5")
                       + Decimal(previous) * Decimal("4.0")
                       + Decimal(current) * Decimal("4.2")) / count
            snapshots.append([at.isoformat().replace("+00:00", "Z"), country,
                              "DEMO_APP", "demo-1.0", "2026-09-21T00:00:00Z",
                              count, f"{average:.5f}", count, f"{average:.5f}"])
    reviews = [[f"demo-{i + 1}", "2026-10-09T12:00:00Z", country, 4,
                "Fictional demo review", "Synthetic example; not a customer review."]
               for i, country in enumerate(countries)]
    for name, headers, rows in [
        ("ratings_snapshots.csv", SNAPSHOT_HEADERS, snapshots),
        ("written_reviews.csv", REVIEW_HEADERS, reviews),
    ]:
        with (destination / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(headers)
            writer.writerows(rows)
    return len(snapshots), len(reviews)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("examples/synthetic"))
    args = parser.parse_args()
    snapshots, reviews = generate(args.output)
    print(f"Generated {snapshots} fictional snapshots and {reviews} fictional reviews in {args.output}")
