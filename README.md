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
start (here: 2026-10-01 16:37 UTC).

From two snapshots *(N1, A1)* and *(N2, A2)*:

| | formula |
|---|---|
| new ratings in between | N2 − N1 |
| mean star of those new ratings | (N2·A2 − N1·A1) / (N2 − N1) |
| worst-case error of that mean | e·(N1 + N2) / (N2 − N1), where e = half a unit in the last decimal of the average |

The error term is the catch: everything depends on how many decimals Apple returns.
Apple returns 5, which keeps a weekly mean within about ±0.02★ in every storefront
(see the findings below).

There is **no 1★–5★ split** in this data. Getting one needs another source (the
customer-reviews histogram endpoint, App Store Connect, or a paid tracker); see
AICP-172. The requester agreed on 2026-10-01 that the average is enough for now.

## Layout

```
n8n/app-store-ratings-snapshot.json         hourly Lookup API snapshot -> `ratings_snapshots` tab (live since 2026-10-01)
n8n/app-store-ratings-weekly-report.json    weekly ratings message to Slack
n8n/app-store-reviews-to-sheet-slack.json   written-review feed (sanitised export, with proposed fixes)
scripts/ratings_report.py                   per-day/week stats and diagnostics from the snapshots (stdlib only)
tests/                                      python3 -m unittest discover -s tests
```

## Workflows

All exports are sanitised for this public repo: credentials, the spreadsheet id, the
Slack channel id and instance metadata are stripped, and every workflow imports
**inactive**. After importing, select the credentials on each Google Sheets / Slack /
Apify node and replace every `REPLACE_WITH_…` value.

### `app-store-ratings-snapshot.json`

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

Keep it **hourly**. About a third of the answers come from stale caches (see below),
and more samples mean every window contains a fresh one.

### `app-store-ratings-weekly-report.json`

Mondays at 12:17 (n8n instance time zone) → read `ratings_snapshots` and the reviews
tab → build one message for the last full week (Monday–Sunday, **UTC**) → post it to
Slack.

The message is a table with one row per storefront plus `All`. The columns are: new
ratings, change vs the previous week, mean★ of the new ratings with its error bound,
change vs the previous week, and written reviews with their average star. A footer
explains the columns, marks a partial week with `†`, and lists the all-time average.
The code is the same logic as `ratings_report.py`, and a test checks that the two
produce identical numbers.

Setup: import, pick the Google credential on both Sheets nodes and the Slack
credential, set the spreadsheet id and the channel id, and run it once by hand. Send
the first run to a test channel or DM to check the format before pointing it at the
real channel.

### `app-store-reviews-to-sheet-slack.json`

Every 6 h at :13 → read the reviews sheet → Apify
[`thewolves/appstore-reviews-scraper`](https://apify.com/thewolves/appstore-reviews-scraper)
for US, CA, GB, AU → drop review ids already in the sheet → append to the sheet and
post each new review to Slack.

This file carries two fixes that are **not in production yet**; apply them by hand in
n8n:

- `maxItems` raised from 5 to 10 per storefront per run. In September the busiest
  storefront (US) peaked at 11 written reviews a day, against a cap of 20/day, so
  nothing was obviously lost. The change only adds headroom. Raising it doesn't
  flood Slack: the extra items are either already in the sheet (deduplicated) or
  genuinely missed recent reviews.
- The Slack "Open in App Store" button now opens the review's own storefront instead
  of always the US page.

Other limitations:

- The source is the RSS feed, which lags: reviews show up ~1–1.5 days after they are
  written, so the latest day's review count fills in late.
- Some rows carry a date with no time (`2026-10-03T00:00:00.000Z`) instead of a
  timestamp. Weekly totals are unaffected; a daily split can be off by a day at the
  edges.

## `scripts/ratings_report.py`

Export the `ratings_snapshots` tab (and optionally the reviews tab) as CSV, then:

```
python3 scripts/ratings_report.py snapshots.csv --diagnose
python3 scripts/ratings_report.py snapshots.csv --reviews reviews.csv --period week
python3 scripts/ratings_report.py snapshots.csv --reviews reviews.csv --period day --tz America/Sao_Paulo --format csv
```

- `--diagnose`: per storefront, the decimals Apple returns, the longest gap between
  snapshots, how many answers were stale, how often the freshest count rises (the
  real update cadence), and any drop that persisted (a possible reset).
- The default output is one row per period × storefront, plus an `ALL` row that
  combines storefronts exactly (summed counts and weighted sums, not an average of
  means). The columns are: new ratings, mean★ of the new ratings with its error bound,
  written reviews by star, `star-only≈` (new ratings − written reviews), and flags.
- **The value at time *t* is the freshest one**: the highest count seen in the 48 h
  up to *t* (`--window-hours`). The latest snapshot alone would be wrong a third of
  the time, because of the stale caches.
- In a partial first period, written reviews are counted only from the first
  snapshot, so both sides cover the same span.

Flags:

| flag | meaning |
|---|---|
| `partial` | the period isn't fully covered by snapshots (first or current period) |
| `no_data` | not enough snapshots for the period (ALL: some storefront lacks data) |
| `count_decreased` | the freshest count stayed below an earlier one for 48 h (summary-rating reset or Apple removing ratings), so there is no delta |
| `version_changed` | a new app version went live during the period |
| `inconsistent` | the derived mean is outside 1–5 even with the error bound, so count and average are out of step |

`star-only≈` is only approximate. It assumes every written review also counts towards
`userRatingCount`, and it inherits the review feed's lag.

## Findings

### First manual snapshot (2026-10-01)

- **Precision is 5 decimals.** Apple prints the double's full binary expansion
  (`4.6628699999999998482…`), but the value behind it is `4.66287`. The n8n Code nodes
  and `ratings_report.py` both reduce it to that form. Read literally, it would look like
  49 decimals and the error bound would come out as zero, which is false precision.
- `*ForCurrentVersion` fields equal the all-time ones, even though 5.5.13 shipped the
  day before. They just mirror the totals, so per-version numbers aren't available
  here.

### Four days of hourly snapshots (2026-10-01 16:37 – 10-05 12:07 UTC, 93 per storefront)

- **Stale caches.** Between 24% and 38% of answers return a count lower than one
  already seen; the oldest value came back 27 h after it first appeared. The raw series
  flaps down and up. The highest count in any 24–48 h window never went down, so
  "freshest value in the last 48 h" is the right reading.
- **Apple updates the totals in steps**: the freshest count rose every 6–21 h
  (median 6 h in AU, 21 h in GB). A daily figure is lumpy (one update can land on
  either side of midnight); the weekly figure is the stable one.
- **Scale.** The US gets ~60–110 new ratings a day, GB/CA/AU ~2–13. Written reviews are
  about 1 in 12 ratings and skew heavily negative (mostly 1–2★), while the mean of all
  new ratings is around 4.2–4.4★ overall (US-dominated). Written reviews alone overstate
  the bad news.

## Still to verify (AICP-172)

- Whether a summary-rating reset on a version release affects `userRatingCount` (the
  `count_decreased` flag will show it).
- The weekly workflow's first manual run in n8n: how the Sheets node returns numbers
  and dates (the code accepts strings, numbers and Sheets serial dates).
