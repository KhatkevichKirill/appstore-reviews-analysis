# appstore-reviews-analysis

Tooling for the Speechify iOS app's App Store feedback: the existing written-review
feed, plus rating counts that include ratings left **without** a review.

Tracking issue: [AICP-172](https://linear.app/speechify-inc/issue/AICP-172/report-ios-app-store-star-ratings-not-just-written-reviews-on-a).

## The constraint everything here works around

Apple publishes **written reviews** individually (the public customer-reviews RSS
feed, App Store Connect `customerReviews`), but a **star-only rating** never appears
as a record anywhere. A review scraper therefore cannot count ratings, however it is
configured. What *is* public, per storefront, is the running aggregate from the iTunes
Lookup API (`https://itunes.apple.com/lookup?id=1209815023&country=us`):

- `userRatingCount`: total ratings so far
- `averageUserRating`: their average

The Lookup API has **no history**. Ratings data exists only from the moment snapshots
start, so the snapshot workflow should run before anything else is settled.

From two snapshots *(N1, A1)* and *(N2, A2)*:

| | formula |
|---|---|
| new ratings in between | N2 − N1 |
| mean star of those new ratings | (N2·A2 − N1·A1) / (N2 − N1) |
| worst-case error of that mean | e·(N1 + N2) / (N2 − N1), where e = half a unit in the last decimal of the average |

The error term is the catch: everything depends on how many decimals Apple returns.
For example, with ~100k ratings and 50 new per day, 5 decimals give ±0.02★, but 2
decimals give ±20★, which is useless. `ratings_report.py --diagnose` measures the actual
precision from collected snapshots, and the report prints the bound next to every mean.

There is **no 1★–5★ split** in this data. Getting one needs another source (the
customer-reviews histogram endpoint, App Store Connect, or a paid tracker); see
AICP-172.

## Layout

```
n8n/app-store-reviews-to-sheet-slack.json   current production workflow (sanitised export)
n8n/app-store-ratings-snapshot.json         NEW: hourly Lookup API snapshot -> sheet tab
scripts/ratings_report.py                   per-day/week stats from the snapshots (stdlib only)
tests/                                      python3 -m unittest discover -s tests
```

## Workflows

Both exports are sanitised for this public repo: credentials, the spreadsheet id, the
Slack channel id and instance metadata are stripped, and both import **inactive**.
After importing, select the credentials on each Google Sheets / Slack / Apify node and
replace every `REPLACE_WITH_…` value.

### `app-store-reviews-to-sheet-slack.json` (existing)

Every 6 h at :13 → read the reviews sheet → Apify
[`thewolves/appstore-reviews-scraper`](https://apify.com/thewolves/appstore-reviews-scraper)
for US, CA, GB, AU (`maxItems: 5` each) → drop review ids already in the sheet →
append to the sheet and post each new review to Slack.

Known limitations (kept as-is; this file mirrors production):

- `maxItems: 5` per storefront per run: anything beyond 20 reviews/day/storefront is
  dropped silently. That's harmless for the feed, but it undercounts written reviews in
  a report. The actor's `until` input (only reviews newer than a date) with a higher
  `maxItems` fixes it at negligible cost.
- The source is the RSS feed, which lags: reviews show up ~1–1.5 days after they are
  written, so the latest day's review count fills in late.
- The Slack "Open in App Store" button always links to the US page.

### `app-store-ratings-snapshot.json` (new)

Hourly at :07 → one Lookup request per storefront (US, GB, CA, AU; retried 3×) →
append one row per storefront to the `ratings_snapshots` tab. It writes rows only and
posts nothing to Slack.

Setup:

1. In the reviews spreadsheet, add a tab named `ratings_snapshots` with this header row:

   ```
   snapshot_at	country	app_id	app_version	current_version_release_date	user_rating_count	average_user_rating	user_rating_count_current_version	average_user_rating_current_version
   ```

2. Import the workflow, pick the Google service-account credential on
   **Append snapshot rows**, set the spreadsheet id, then activate.

The averages are written as **text** (`cellFormat: RAW`): if Sheets formats them as
numbers it may round away the decimals the mean-of-new-ratings maths depends on.

Hourly is deliberate for the first week: it shows how often Apple actually updates the
counts, so the schedule can drop to daily afterwards.

## `scripts/ratings_report.py`

Export the `ratings_snapshots` tab (and optionally the reviews tab) as CSV, then:

```
python3 scripts/ratings_report.py snapshots.csv --diagnose
python3 scripts/ratings_report.py snapshots.csv --reviews reviews.csv
python3 scripts/ratings_report.py snapshots.csv --reviews reviews.csv --period week --tz America/Sao_Paulo --format csv
```

- `--diagnose`: per storefront, the decimals Apple returns, how often the count
  changes, the longest gap between snapshots, count decreases and version changes.
  Run this first.
- The default output is one row per period × storefront, plus an `ALL` row that
  combines storefronts exactly (summed counts and weighted sums, not an average of
  means). The columns are: new ratings, mean★ of the new ratings with its error bound,
  written reviews by star, `star-only≈` (new ratings − written reviews), and flags.
- A period's value at time *t* comes from the last snapshot at or before *t*.

Flags:

| flag | meaning |
|---|---|
| `partial` | the period isn't fully covered by snapshots (first or current period) |
| `no_data` | not enough snapshots for the period (ALL: some storefront lacks data) |
| `count_decreased` | total went down (summary-rating reset or Apple removing ratings), so no delta |
| `version_changed` | a new app version went live during the period |
| `inconsistent` | the derived mean is outside 1–5 even with the error bound, so count and average are out of step |

`star-only≈` is only approximate. It assumes every written review also counts towards
`userRatingCount`, and it inherits the review feed's lag and the `maxItems` cap
described above.

## Still to verify (AICP-172)

- Decimal precision and update cadence of the Lookup numbers (`--diagnose` after a few
  days of snapshots).
- Whether a summary-rating reset on a version release affects `userRatingCount`.
- One period cross-checked against App Store Connect.
