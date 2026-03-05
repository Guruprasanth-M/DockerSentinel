-- Docker Sentinel PostgreSQL Schema
-- This file runs automatically on first container start via docker-entrypoint-initdb.d

-- ─── Alerts Table ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    alert_id        VARCHAR(128) UNIQUE NOT NULL,
    severity        VARCHAR(16) NOT NULL DEFAULT 'medium',
    score           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    risk_level      VARCHAR(32) NOT NULL DEFAULT 'normal',
    anomaly_type    VARCHAR(64),
    policy_name     VARCHAR(128),
    source_ip       VARCHAR(64),
    action          VARCHAR(64) DEFAULT 'alert_only',
    message         TEXT,
    notify          BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts USING brin(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_score ON alerts(score);

-- ─── Actions Table ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS actions (
    id              SERIAL PRIMARY KEY,
    action_id       VARCHAR(128) UNIQUE NOT NULL,
    action          VARCHAR(64) NOT NULL,
    target          VARCHAR(256) NOT NULL,
    triggered_by    VARCHAR(64) DEFAULT 'policy',
    alert_id        VARCHAR(128),
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',
    message         TEXT,
    reversible      BOOLEAN DEFAULT FALSE,
    reversal_at     TIMESTAMPTZ,
    reversed        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_created_at ON actions USING brin(created_at);
CREATE INDEX IF NOT EXISTS idx_actions_alert_id ON actions(alert_id);

-- ─── Scores History Table ─────────────────────────────────
CREATE TABLE IF NOT EXISTS scores (
    id              SERIAL PRIMARY KEY,
    score           DOUBLE PRECISION NOT NULL,
    risk_level      VARCHAR(32) NOT NULL,
    anomaly_type    VARCHAR(64),
    features        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scores_created_at ON scores USING brin(created_at);
CREATE INDEX IF NOT EXISTS idx_scores_score ON scores(score);

-- ─── Audit Log Table ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id              SERIAL PRIMARY KEY,
    event_type      VARCHAR(64) NOT NULL,
    action_id       VARCHAR(128),
    action          VARCHAR(64),
    target          VARCHAR(256),
    triggered_by    VARCHAR(64),
    status          VARCHAR(32),
    details         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log USING brin(created_at);

-- ─── Webhook Deliveries Table ─────────────────────────────
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              SERIAL PRIMARY KEY,
    webhook_name    VARCHAR(128) NOT NULL,
    url             TEXT NOT NULL,
    event_type      VARCHAR(64),
    alert_id        VARCHAR(128),
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    payload         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_webhook_created_at ON webhook_deliveries USING brin(created_at);

-- ─── Host Metrics Snapshots ───────────────────────────────
CREATE TABLE IF NOT EXISTS host_metrics (
    id              SERIAL PRIMARY KEY,
    cpu_percent     DOUBLE PRECISION,
    memory_percent  DOUBLE PRECISION,
    memory_used_mb  DOUBLE PRECISION,
    memory_total_mb DOUBLE PRECISION,
    disk_percent    DOUBLE PRECISION,
    disk_used_gb    DOUBLE PRECISION,
    disk_total_gb   DOUBLE PRECISION,
    net_bytes_in    BIGINT DEFAULT 0,
    net_bytes_out   BIGINT DEFAULT 0,
    load_avg_1      DOUBLE PRECISION,
    load_avg_5      DOUBLE PRECISION,
    load_avg_15     DOUBLE PRECISION,
    features        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_host_metrics_created_at ON host_metrics USING brin(created_at);

-- ─── Data Retention: Auto-cleanup old rows ────────────────
-- Retention function: call via pg_cron or external cron daily
-- Usage: SELECT sentinel_retention_cleanup();
CREATE OR REPLACE FUNCTION sentinel_retention_cleanup()
RETURNS TABLE(table_name TEXT, rows_deleted BIGINT) AS $$
DECLARE
    _count BIGINT;
BEGIN
    -- Scores: keep 30 days
    DELETE FROM scores WHERE created_at < NOW() - INTERVAL '30 days';
    GET DIAGNOSTICS _count = ROW_COUNT;
    RETURN QUERY SELECT 'scores'::TEXT, _count;

    -- Host metrics: keep 30 days
    DELETE FROM host_metrics WHERE created_at < NOW() - INTERVAL '30 days';
    GET DIAGNOSTICS _count = ROW_COUNT;
    RETURN QUERY SELECT 'host_metrics'::TEXT, _count;

    -- Webhook deliveries: keep 90 days
    DELETE FROM webhook_deliveries WHERE created_at < NOW() - INTERVAL '90 days';
    GET DIAGNOSTICS _count = ROW_COUNT;
    RETURN QUERY SELECT 'webhook_deliveries'::TEXT, _count;

    -- Audit log: keep 180 days
    DELETE FROM audit_log WHERE created_at < NOW() - INTERVAL '180 days';
    GET DIAGNOSTICS _count = ROW_COUNT;
    RETURN QUERY SELECT 'audit_log'::TEXT, _count;

    -- Alerts: keep 180 days
    DELETE FROM alerts WHERE created_at < NOW() - INTERVAL '180 days';
    GET DIAGNOSTICS _count = ROW_COUNT;
    RETURN QUERY SELECT 'alerts'::TEXT, _count;

    -- Actions: keep 180 days
    DELETE FROM actions WHERE created_at < NOW() - INTERVAL '180 days';
    GET DIAGNOSTICS _count = ROW_COUNT;
    RETURN QUERY SELECT 'actions'::TEXT, _count;
END;
$$ LANGUAGE plpgsql;

-- To schedule daily: add to host crontab:
-- 0 3 * * * docker compose -f /path/to/docker-compose.yml exec -T db psql -U sentinel -c "SELECT sentinel_retention_cleanup();"

-- ─── Helper view: Recent alerts summary ───────────────────
CREATE OR REPLACE VIEW recent_alerts AS
SELECT
    alert_id, severity, score, risk_level, anomaly_type,
    policy_name, source_ip, action, message, created_at
FROM alerts
ORDER BY created_at DESC
LIMIT 100;

-- ─── Helper view: Action lifecycle ────────────────────────
CREATE OR REPLACE VIEW action_lifecycle AS
SELECT
    a.action_id, a.action, a.target, a.status, a.message,
    a.triggered_by, a.alert_id, a.reversible, a.reversed,
    a.reversal_at, a.created_at
FROM actions a
ORDER BY a.created_at DESC
LIMIT 100;
