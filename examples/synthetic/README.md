# Synthetic publication example

All values here are generated fictional data, not Speechify/customer data or Apple exports. `DEMO_APP` deliberately is not a real App Store ID.

- `ratings_snapshots.csv`: 2,454 hourly snapshots, September 21–October 13, 2026. BR/IN start October 7 at 12:00 UTC.
- `written_reviews.csv`: six explicitly fictional reviews.
- `weekly-report.json`: frozen output of the n8n report Code node at October 12, 2026, 12:17 UTC, checked against the Python implementation. Four countries compare complete weeks; BR/IN and All omit comparisons.
- [Public Google Sheet](https://docs.google.com/spreadsheets/d/1_tkPAO0I9o1VIxYZCRpid_KEnWc5xzF5TRoMq9rkQTc/edit): readable presentation of the same inputs and frozen result.

Regenerate the CSVs with `python3 scripts/generate_demo.py`. Calculate historical periods with:

```bash
python3 scripts/ratings_report.py examples/synthetic/ratings_snapshots.csv \
  --reviews examples/synthetic/written_reviews.csv --period week --tz UTC
```

The public Sheet is a frozen example. An n8n preview uses its execution date, so running these fixed dates later can show no data. Use the Python command for a date-independent historical demonstration. Start a real tracker from a private copy with demo input rows cleared; follow [the launch guide](../../docs/quickstart.md).
