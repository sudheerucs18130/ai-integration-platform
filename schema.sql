PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner TEXT NOT NULL,
    business_criticality TEXT NOT NULL CHECK (business_criticality IN ('low', 'medium', 'high', 'mission_critical')),
    description TEXT NOT NULL,
    routing_group TEXT NOT NULL,
    sla_target_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'healthy' CHECK (status IN ('healthy', 'degraded', 'failing', 'recovering')),
    source TEXT NOT NULL DEFAULT 'mock',
    external_id TEXT,
    telemetry_endpoint TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    error_rate REAL NOT NULL,
    throughput REAL NOT NULL,
    queue_depth INTEGER NOT NULL,
    cpu_percent REAL NOT NULL,
    memory_percent REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'failing')),
    anomaly_score REAL NOT NULL,
    driver TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'mock',
    raw_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_workflow_time ON telemetry(workflow_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    risk_score REAL NOT NULL,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    horizon_minutes INTEGER NOT NULL,
    confidence REAL NOT NULL,
    driver TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'mitigated', 'expired'))
);

CREATE INDEX IF NOT EXISTS idx_predictions_workflow_time ON predictions(workflow_id, created_at DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('medium', 'high', 'critical')),
    status TEXT NOT NULL CHECK (status IN ('open', 'mitigating', 'resolved')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    sla_impact_minutes REAL NOT NULL DEFAULT 0,
    detection_source TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, opened_at DESC);

CREATE TABLE IF NOT EXISTS healing_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    incident_id INTEGER REFERENCES incidents(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending_approval', 'running', 'completed', 'failed')),
    confidence REAL NOT NULL,
    human_approval_required INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL,
    result TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_healing_actions_time ON healing_actions(created_at DESC);

CREATE TABLE IF NOT EXISTS policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    workflow_id INTEGER REFERENCES workflows(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    risk_threshold REAL NOT NULL,
    human_approval_required INTEGER NOT NULL DEFAULT 0,
    actions TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_time ON audit_logs(created_at DESC);
