# Track App Store rating changes with n8n and Google Sheets

Collect an app's public rating count and average by storefront, then generate a weekly report. The ratings path needs **no paid scraper, Apple API key, or separate database service**. Self-hosted n8n Community Edition is free; hosting and n8n Cloud can cost money. See [n8n hosting options](https://docs.n8n.io/hosting/).

Ratings include people who leave stars without writing a review. Written reviews are a separate, optional dataset. This workflow measures changes in Apple's cumulative totals; it does not fetch individual star-only ratings or reconstruct past history.

## Try the example

Open the [public synthetic Google Sheet](https://docs.google.com/spreadsheets/d/1_tkPAO0I9o1VIxYZCRpid_KEnWc5xzF5TRoMq9rkQTc/edit). It contains fictional data, six storefronts, and a frozen report for October 5–11, 2026. BR/IN begin mid-week: their comparisons and the aggregate comparison are omitted. No customer or production exports are included.

For a local report, install Python 3.10+ and run:

```bash
git clone https://github.com/KhatkevichKirill/appstore-reviews-analysis.git
cd appstore-reviews-analysis
python3 scripts/ratings_report.py examples/synthetic/ratings_snapshots.csv \
  --reviews examples/synthetic/written_reviews.csv --period week --tz UTC
```

No Python package dependencies are required. The CLI shows historical periods, flags and rounding bounds; the n8n workflow adds comparisons and Slack formatting. Regenerate fictional inputs with `python3 scripts/generate_demo.py`.

## 1. Prepare a private working spreadsheet

Use **File → Make a copy** on the demo. Keep your working copy private. Clear rows 2 onward in `ratings_snapshots` and `Sheet1`, retaining their header rows. Remove the static `weekly_report` tab if you do not need the illustration. That tab is a frozen workflow result, not a live Sheets formula report.

Use **one app per spreadsheet**: the report groups by country and does not filter rows by `app_id`. Do not mix demo rows, several apps, or multiple collection workflows in one input tab.

Required snapshot headers:

```text
snapshot_at,country,app_id,app_version,current_version_release_date,user_rating_count,average_user_rating,user_rating_count_current_version,average_user_rating_current_version
```

Keep both average columns as **plain text**. The collector writes using **RAW** to preserve Apple's returned precision. Timestamps are ISO UTC strings; counts are numbers. Do not shorten stored averages to the App Store's visible 4.7-style display.

The report also reads `Sheet1`. For a ratings-only setup, leave that tab empty except for headers; `Written = 0` then means no written-review data was supplied. Minimum review headers: `date,country,score`; the demo also includes `id,title,content`.

## 2. Connect Google Sheets

Use an n8n instance with **Data Tables** available. Confirm it recognizes all imported Code, Sheets, Data Table and HTTP Request nodes.

Set up [Google Sheets OAuth2](https://docs.n8n.io/integrations/builtin/credentials/google/oauth-single-service/) or an existing [Google service-account credential](https://docs.n8n.io/integrations/builtin/credentials/google/service-account/). The exports default to service-account authentication. For OAuth2, change the authentication selection on **all three Sheets nodes** before selecting your credential. That account needs access to your working spreadsheet; a service account needs the appropriate file/shared-drive access.

Enable Google Sheets and Google Drive APIs if your credential setup requires your own Google Cloud project. Keep credentials in n8n, outside Git and exported JSON.

## 3. Start collecting snapshots

Import `n8n/app-store-ratings-snapshot.json` as a new inactive workflow.

1. In **Countries**, replace `APP_ID = '1209815023'` with your app's numeric ID: the number after `id` in its App Store URL.
2. Edit `COUNTRIES` if needed, using lowercase storefront codes such as `['us', 'gb', 'ca']`.
3. In **Append snapshot rows**, select your credential, replace the document ID placeholder with your copy's ID, select `ratings_snapshots`, and retain **RAW** formatting.
4. Execute once. Verify a row per selected country with the correct app ID, timestamp, count and precise average. Inspect any unavailable app/storefront response before scheduling it.
5. Activate/publish the collector. Its default schedule runs hourly at minute :07.

Keep collecting to build history. A first weekly report may be partial; a valid comparison needs two fully covered weeks. Apple's updates can be irregular and responses stale. The report uses the highest count in the preceding 48-hour window as a heuristic, not a guarantee of freshness.

## 4. Preview the report without sending

Import `n8n/app-store-ratings-weekly-report.json` as another inactive workflow.

1. Configure **Read snapshots** and **Read reviews** with the same working spreadsheet and credential.
2. In **Build weekly report**, synchronize the uppercase `COUNTRIES` list with the collector and replace `Speechify iOS` in the title with your app's name.
3. In **Delivery settings**, set the same app ID and leave `deliveryEnabled: false`.
4. Execute **Manual preview**. Inspect `Build weekly report.slackBody` and `report`. Preview does not access the delivery table or Slack. Do not execute downstream sender nodes individually.

The report covers the last completed Monday–Sunday week in **UTC**. Both count and mean comparisons require complete, usable current and previous weeks. A partial storefront suppresses the comparison for `All`; complete storefronts can still compare independently.

## 5. Enable Slack delivery using an n8n Data Table

First use a dedicated test Slack channel.

1. Create a Data Table in the workflow's project using the [seven-column schema](weekly-report-safety.md#one-built-in-data-table-no-external-database). Import `templates/weekly-ratings-delivery-seed.csv` **once**. There must be exactly one control row; n8n creates the native `id` itself.
2. Select that same table in **Read delivery state**, **Claim delivery**, and **Persist delivery result**. All copies of this sender in the project must share it.
3. Select a [Slack OAuth2 credential](https://docs.n8n.io/integrations/builtin/credentials/slack/) on **Post to Slack**. Give the app posting permission (`chat:write`), invite it to the channel, and replace `channelId` in **Delivery settings** with the concrete channel ID.
4. When replacing an existing sender, stop its old schedule, back up its workflow/state, and migrate confirmed previously sent app/week/channel keys into `ledger`. Follow [migration and recovery guidance](weekly-report-safety.md). An empty ledger can resend old reports.
5. Set `deliveryEnabled: true` and run from the workflow trigger. Verify one message and a `sent` ledger entry with its Slack timestamp. Repeat the same app/week/channel: no second message should appear. Check overlapping runs in your n8n instance before relying on concurrency protection.
6. Switch to the intended channel and activate/publish the weekly workflow. Changing channel changes the key: one report can legitimately send once to each channel. The default schedule is Monday 12:17 **in the workflow/instance timezone**; reporting boundaries remain UTC. Check the schedule locally.

The journal claims an attempt before contacting Slack. Confirmed sends are suppressed on repeats. An uncertain response becomes `ambiguous` and is not automatically retried; a crash can leave the singleton locked. Read the [recovery procedure](weekly-report-safety.md) before unlocking it. Back up the table and retain its ledger. This may need manual recovery and is not a universal exactly-once guarantee. The [validation record](isolated-n8n-validation.md) documents the isolated concurrency and real-Slack cases.

## Optional written reviews

The ratings setup needs no Apify. `n8n/app-store-reviews-to-sheet-slack.json` is a separate Apify-based feed requiring its own account/credentials; scraper charges may apply. Item limits and publication lag can make review coverage incomplete. Configure it separately and validate completeness before calling its `Written` count all reviews.

## Read the results correctly

- **Net ratings:** `N2 − N1`; removals or resets can affect totals, so this is not an exact count of rating events.
- **Derived mean:** `(N2 × A2 − N1 × A1) / (N2 − N1)`, when usable. Source rounding introduces error; the report shows a bound instead of promising fixed accuracy.
- **Written:** reviews present in the supplied feed over the covered span.
- **Unavailable comparisons:** `—`, not zero growth.

The public Lookup response provides neither a star-only 1–5★ histogram nor historical snapshots. See [Apple's Lookup documentation](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/LookupExamples.html) and the README's methodology and dated observations.

## Verify the code

Python 3.10+ and Node.js are needed for the complete regression suite:

```bash
python3 -m unittest discover -s tests -v
```

Local checks do not replace preview and isolated delivery verification in a different n8n environment.
