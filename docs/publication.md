# Publication pack

## Telegram

I wanted a simple weekly view of App Store feedback. One catch: counting written reviews misses people who only leave stars.

So I built a small tracker with n8n and Google Sheets. It saves Apple's public rating totals by country, measures their weekly change, derives the mean rating for that change, and sends a report to Slack.

No paid scraper is needed for the ratings path. If you already run n8n, there is no extra service to deploy; Slack delivery uses n8n's own Data Table.

Two details mattered: skip comparisons when either week is incomplete, and keep a delivery journal so rerunning a confirmed report does not post it again.

The code, setup guide and a fictional sample table are available below. Hosting can still cost money, and history only starts when you begin collecting snapshots.

Repository: https://github.com/KhatkevichKirill/appstore-reviews-analysis
Demo: https://docs.google.com/spreadsheets/d/1_tkPAO0I9o1VIxYZCRpid_KEnWc5xzF5TRoMq9rkQTc/edit

## LinkedIn

Counting App Store reviews doesn't tell you how many people rated your app. Some leave stars without writing anything.

I built a small reporting workflow to track that broader signal using n8n, Google Sheets and Apple's public rating aggregates.

It collects snapshots by storefront, calculates the weekly change in rating count, derives the mean rating for that change, and delivers a report to Slack. Written reviews can be added as a separate dataset.

The interesting work was making the report trustworthy:

- Compare weeks only when both have complete coverage.
- Show a rounding-error bound for the derived mean.
- Keep a delivery journal in n8n's native Data Table to suppress repeats of confirmed sends.
- Hold uncertain deliveries for review instead of blindly retrying.

We checked overlapping runs and a real Slack send followed by a repeat in an isolated test setup.

The ratings path needs no paid scraper or external database service. Self-hosted n8n Community Edition is free; infrastructure or n8n Cloud may still cost money. This tracks changes in cumulative totals, not individual rating events, and cannot backfill history from before collection started.

I've shared the workflow exports and setup guide, with a public synthetic example so you can explore the output without exposing customer data.

Repository: https://github.com/KhatkevichKirill/appstore-reviews-analysis
Demo: https://docs.google.com/spreadsheets/d/1_tkPAO0I9o1VIxYZCRpid_KEnWc5xzF5TRoMq9rkQTc/edit

## Before publishing

Merge the publication documentation PR before using these posts, so the repository's main page includes the guide and examples. The repository is MIT-licensed; service terms still apply to the services you connect.

Attach a screenshot of the demo's `weekly_report` tab. Keep the synthetic-data label and reporting dates visible. Describe it as an example, not production results. Link the public demo rather than a working spreadsheet or delivery table.

The main message is “track App Store rating changes without a paid scraper,” not a promise to collect every review, reconstruct old ratings, infer a full star histogram, or provide free hosting. Both drafts are prepared copy; publishing them to social platforms is a separate action.
