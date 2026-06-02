# Using The Prototype With Real-Time IBM Applications

This application can monitor real IBM or enterprise integration endpoints in two ways:

1. Register application health/API URLs from the dashboard.
2. Configure an IBM telemetry feed through environment variables.

## Fastest path: dashboard monitor

1. Start the app.
2. Open `http://127.0.0.1:8000`.
3. Go to **Real Apps**.
4. Enter:
   - Name: `IBM App Connect Order Flow`
   - Health or API URL: your IBM/application health endpoint
   - SLA ms: expected response time, for example `900`
   - Criticality: high or mission critical
5. Click **Monitor**.

The app will continuously probe the URL. If it is down, slow, or returns unhealthy status data, the issue becomes:

- telemetry
- risk score
- prediction
- incident
- recovery action
- audit log entry

## IBM App Connect examples

Use any endpoint that reflects the health of the flow or integration API:

```text
https://your-app-connect-domain/health
https://your-app-connect-domain/orders/status
https://your-api-connect-gateway.example.com/order-api/health
```

If the endpoint returns JSON, the app will read common fields such as:

```json
{
  "status": "healthy",
  "latency_ms": 410,
  "error_rate": 0.2,
  "throughput": 250,
  "queue_depth": 12,
  "cpu_percent": 45,
  "memory_percent": 52
}
```

If the endpoint is not JSON, the app still uses HTTP status and response time.

## IBM MQ examples

For IBM MQ, monitor a REST endpoint, queue manager endpoint, or a small bridge service that exposes queue health:

```json
{
  "name": "IBM MQ Payments Queue",
  "url": "https://mq.example.com/health/payments",
  "owner": "Messaging Operations",
  "business_criticality": "mission_critical",
  "routing_group": "ibm-mq",
  "sla_target_ms": 1200
}
```

Recommended fields for an MQ bridge:

```json
{
  "status": "healthy",
  "latency_ms": 120,
  "error_rate": 0.1,
  "throughput": 800,
  "queue_depth": 42,
  "backlog": 42
}
```

## IBM Instana or Event Streams telemetry bridge

For production-style integration, configure a telemetry bridge that exposes a JSON feed:

```bash
export IBM_TELEMETRY_URL="https://telemetry-bridge.example.com/integration-health"
export IBM_TELEMETRY_TOKEN="replace-with-token"
python3 server.py
```

Expected feed:

```json
{
  "telemetry": [
    {
      "applicationName": "IBM App Connect Order Flow",
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

## Environment variable setup

```bash
export AIP_SOURCE_MODE=auto
export IBM_APPLICATIONS='[
  {
    "name": "IBM App Connect Order Flow",
    "url": "https://application.example.com/health",
    "owner": "Integration Platform",
    "business_criticality": "mission_critical",
    "routing_group": "ibm-app-connect",
    "sla_target_ms": 900
  }
]'
python3 server.py
```

Modes:

```text
AIP_SOURCE_MODE=auto      IBM first, mock fallback
AIP_SOURCE_MODE=mock      simulator only
AIP_SOURCE_MODE=ibm_only  IBM sources only
```

## What counts as an issue

The application treats these as real-time issues:

- endpoint connection refused
- timeout
- HTTP 4xx or 5xx
- response time above SLA
- JSON status such as `down`, `unavailable`, `failing`, `critical`, or `degraded`
- high error rate
- high queue depth
- CPU or memory pressure

## What to use in a real IBM environment

Best practical setup:

```text
IBM App Connect / API Connect / MQ / Event Streams / Instana
        ↓
Health endpoint or telemetry bridge
        ↓
This prototype
        ↓
Prediction, incident, self-healing, audit dashboard
```

For a production rollout, replace SQLite with PostgreSQL, add enterprise authentication, secure secrets through Vault or cloud secrets, and connect recovery actions to real IBM App Connect, MQ, Kubernetes/OpenShift, or runbook APIs.
