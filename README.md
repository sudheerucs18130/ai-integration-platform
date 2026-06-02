# Autonomous Integration Platform MVP

An enterprise-style MVP for AI-driven integration resilience. It is IBM-first: when IBM/application telemetry is configured, the platform reads real-time data first; when no IBM source is available, it falls back to the mock simulator so the demo remains runnable.

## What is included

- Runnable dashboard UI served from the backend
- REST API for workflows, telemetry, predictions, incidents, recovery actions, policies, and audit logs
- SQLite database schema and seeded enterprise workflow data
- IBM-first real-time telemetry adapter with mock fallback
- Real application health probes for IBM App Connect, API Connect, MQ REST, Instana-backed health endpoints, or any HTTP health endpoint
- Mock real-time telemetry stream for fallback and demos
- Predictive anomaly scoring service
- Self-healing automation engine with policy checks
- Human approval flow for high-risk actions
- Server-sent events feed for live dashboard updates

## Run

```bash
cd outputs/autonomous-integration-platform
python3 server.py
```

Open:

```text
http://127.0.0.1:8000
```

Optional environment variables:

```bash
PORT=8010 python3 server.py
AIP_DB_PATH=/tmp/platform.db python3 server.py
```

## Docker

```bash
docker compose up --build
```

## DevOps

- Deployment guide: `docs/DEPLOYMENT.md`
- IBM real-time usage guide: `docs/IBM_REAL_TIME_USAGE.md`
- GitHub Actions workflow: `.github/workflows/ci-cd.yml`
- Kubernetes/OpenShift manifests: `deploy/kubernetes/`

## IBM-first telemetry

Default behavior:

```text
1. Try IBM real-time telemetry feed if IBM_TELEMETRY_URL is configured.
2. Try configured real application probes from IBM_APPLICATIONS or IBM_APPLICATION_URLS.
3. Fall back to the mock simulator when IBM sources are missing or temporarily unavailable.
```

You can also add real application monitors directly from the dashboard in the **Real Apps** section. Paste a health or API URL and the platform will probe it continuously. Slow responses, HTTP failures, and connection failures become live telemetry, anomaly scores, incidents, and recovery actions.

Use `AIP_SOURCE_MODE` to control source behavior:

```bash
AIP_SOURCE_MODE=auto       # IBM first, mock fallback. Default.
AIP_SOURCE_MODE=mock       # Force simulator only.
AIP_SOURCE_MODE=ibm_only   # IBM sources only, no fallback.
```

### Option 1: monitor IBM application endpoints

Use this when you have IBM App Connect flows, API Connect APIs, MQ REST endpoints, or service health URLs that can be checked over HTTP.

```bash
export IBM_APPLICATIONS='[
  {
    "name": "IBM App Connect Order Flow",
    "url": "https://example.ibm-app-connect.net/health",
    "owner": "Integration Platform",
    "business_criticality": "mission_critical",
    "routing_group": "ibm-app-connect",
    "sla_target_ms": 900
  },
  {
    "name": "IBM MQ Payments API",
    "url": "https://mq.example.com/ibmmq/rest/v1/admin/qmgr/QM1",
    "owner": "Messaging Operations",
    "business_criticality": "high",
    "routing_group": "ibm-mq",
    "sla_target_ms": 1200
  }
]'

python3 server.py
```

For simple comma-separated health URLs:

```bash
IBM_APPLICATION_URLS="https://app1.example.com/health,https://app2.example.com/health" python3 server.py
```

### Option 2: consume an IBM telemetry feed

Use this when Instana, Event Streams, Event Processing, or another IBM bridge exposes JSON telemetry to this MVP.

```bash
export IBM_TELEMETRY_URL="https://telemetry-bridge.example.com/integration-health"
export IBM_TELEMETRY_TOKEN="replace-with-token"
python3 server.py
```

Expected feed shape can be a list or an object containing `telemetry`, `applications`, `workflows`, `services`, `items`, `data`, `events`, or `snapshots`.

```json
{
  "telemetry": [
    {
      "applicationName": "Order-to-Cash API Gateway",
      "latency_ms": 740,
      "error_rate": 0.7,
      "throughput": 420,
      "queue_depth": 18,
      "cpu_percent": 48,
      "memory_percent": 55,
      "status": "healthy",
      "sla_target_ms": 850
    }
  ]
}
```

For custom auth headers:

```bash
export IBM_TELEMETRY_HEADERS='{"Authorization":"apiToken replace-with-instana-token"}'
```

## Architecture

```text
Browser dashboard
  |
  | REST + server-sent events
  v
Python standard-library HTTP server
  |
  |-- IBM telemetry feed adapter
  |-- IBM/application HTTP health probes
  |-- Mock telemetry simulator fallback
  |-- Anomaly scoring and prediction engine
  |-- Incident manager
  |-- Self-healing action engine
  |-- Policy and approval workflow
  v
SQLite database
```

## Core endpoints

```text
GET  /api/health
GET  /api/summary
GET  /api/workflows
GET  /api/telemetry?limit=120&workflow_id=all
GET  /api/predictions
GET  /api/incidents?status=all
GET  /api/actions
GET  /api/policies
GET  /api/audit
GET  /api/events
GET  /api/source
GET  /api/applications

POST /api/incidents/{id}/remediate
POST /api/actions/{id}/approve
POST /api/policies/{id}/toggle
POST /api/applications
```

## Data model

The schema is in `schema.sql` and includes:

- `workflows`
- `telemetry`
- `predictions`
- `incidents`
- `healing_actions`
- `policies`
- `audit_logs`

## MVP notes

This build avoids external dependencies so it can run in a locked-down demo workspace. In a production build, the same domain model can be moved to FastAPI or NestJS, PostgreSQL, Kafka, Redis, OpenTelemetry, and Kubernetes without changing the product flow.
