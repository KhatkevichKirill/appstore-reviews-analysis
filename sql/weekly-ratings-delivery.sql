-- Apply only in an approved database, outside preview. No live setup is performed by the workflow.
CREATE TABLE weekly_ratings_delivery (
    report_key text PRIMARY KEY,
    owner text NOT NULL,
    state text NOT NULL CHECK (state IN ('sending', 'sent', 'failed', 'ambiguous')),
    payload jsonb NOT NULL,
    slack_ts text,
    detail text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'sent') = (slack_ts IS NOT NULL))
);
