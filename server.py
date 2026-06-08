#!/usr/bin/env python3
"""Autonomous integration resilience platform MVP.

This server intentionally uses only the Python standard library so the MVP can
run in restricted enterprise or demo environments without dependency installs.
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import random
import re
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
import urllib.error
import urllib.request
import subprocess


APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("AIP_DB_PATH", APP_DIR / "data" / "platform.db"))
STATIC_DIR = APP_DIR / "public"
SCHEMA_PATH = APP_DIR / "schema.sql"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
SOURCE_MODE = os.environ.get("AIP_SOURCE_MODE", "auto").strip().lower()
IBM_TELEMETRY_URL = os.environ.get("IBM_TELEMETRY_URL", "").strip()
IBM_TELEMETRY_TOKEN = os.environ.get("IBM_TELEMETRY_TOKEN") or os.environ.get("IBM_INSTANA_API_TOKEN", "")
IBM_APPLICATIONS = os.environ.get("IBM_APPLICATIONS", "").strip()
IBM_APPLICATION_URLS = os.environ.get("IBM_APPLICATION_URLS", "").strip()
IBM_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("IBM_REQUEST_TIMEOUT_SECONDS", "4"))
AIP_API_TOKEN = os.environ.get("AIP_API_TOKEN", "").strip()

DB_LOCK = threading.RLock()
RUNTIME_LOCK = threading.RLock()
RUNTIME_STATE: dict[int, dict[str, Any]] = {}
SOURCE_STATE: dict[str, Any] = {
    "configured": False,
    "active": "mock_fallback",
    "last_success": None,
    "last_error": None,
    "last_checked": None,
    "ibm_samples": 0,
    "mock_samples": 0,
}
STARTED_AT = time.time()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def query_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with DB_LOCK:
        with db_connect() as conn:
            return rows_to_dicts(conn.execute(sql, params).fetchall())


def query_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with DB_LOCK:
        with db_connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with DB_LOCK:
        with db_connect() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid)


def execute_many(sql: str, rows: list[tuple[Any, ...]]) -> None:
    with DB_LOCK:
        with db_connect() as conn:
            conn.executemany(sql, rows)


def add_audit(actor: str, event_type: str, entity_type: str, entity_id: int | None, details: dict[str, Any]) -> None:
    execute(
        """
        INSERT INTO audit_logs(created_at, actor, event_type, entity_type, entity_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (utc_now(), actor, event_type, entity_type, entity_id, json.dumps(details, sort_keys=True)),
    )


def table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def ensure_database_migrations() -> None:
    with DB_LOCK:
        with db_connect() as conn:
            workflow_columns = {
                "source": "TEXT NOT NULL DEFAULT 'mock'",
                "external_id": "TEXT",
                "telemetry_endpoint": "TEXT",
            }
            telemetry_columns = {
                "source": "TEXT NOT NULL DEFAULT 'mock'",
                "raw_payload": "TEXT",
            }
            for column, definition in workflow_columns.items():
                if not table_has_column(conn, "workflows", column):
                    conn.execute(f"ALTER TABLE workflows ADD COLUMN {column} {definition}")
            for column, definition in telemetry_columns.items():
                if not table_has_column(conn, "telemetry", column):
                    conn.execute(f"ALTER TABLE telemetry ADD COLUMN {column} {definition}")


def parse_json_env(name: str, fallback: Any) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        with RUNTIME_LOCK:
            SOURCE_STATE["last_error"] = f"{name} is not valid JSON: {exc}"
        return fallback


def configured_application_probes() -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    if IBM_APPLICATIONS:
        parsed = parse_json_env("IBM_APPLICATIONS", [])
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            for index, item in enumerate(parsed):
                if isinstance(item, str):
                    item = {"url": item}
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                url = str(item["url"]).strip()
                if not url:
                    continue
                parsed_url = urlparse(url)
                default_name = parsed_url.netloc or f"IBM Application {index + 1}"
                probes.append(
                    {
                        "name": str(item.get("name") or default_name),
                        "url": url,
                        "owner": str(item.get("owner") or "IBM Applications"),
                        "business_criticality": str(item.get("business_criticality") or item.get("criticality") or "high"),
                        "description": str(
                            item.get("description")
                            or "Real IBM/application endpoint monitored before simulator fallback."
                        ),
                        "routing_group": str(item.get("routing_group") or item.get("group") or "ibm-runtime"),
                        "sla_target_ms": int(item.get("sla_target_ms") or item.get("sla") or 1000),
                        "external_id": str(item.get("id") or item.get("external_id") or url),
                    }
                )

    for index, url in enumerate(part.strip() for part in IBM_APPLICATION_URLS.split(",") if part.strip()):
        parsed_url = urlparse(url)
        name = parsed_url.netloc or f"IBM Application URL {index + 1}"
        probes.append(
            {
                "name": name,
                "url": url,
                "owner": "IBM Applications",
                "business_criticality": "high",
                "description": "Real application URL monitored before simulator fallback.",
                "routing_group": "ibm-runtime",
                "sla_target_ms": 1000,
                "external_id": url,
            }
        )
    return probes


def runtime_real_source_count() -> int:
    try:
        row = query_one(
            """
            SELECT COUNT(*) AS total
            FROM workflows
            WHERE source IN ('ibm_application', 'ibm_telemetry')
            """
        )
        return int(row["total"]) if row else 0
    except sqlite3.Error:
        return 0


def ibm_source_configured() -> bool:
    return SOURCE_MODE != "mock" and bool(IBM_TELEMETRY_URL or configured_application_probes() or runtime_real_source_count())


def set_source_state(**updates: Any) -> None:
    with RUNTIME_LOCK:
        SOURCE_STATE.update(updates)


def get_version() -> dict[str, Any]:
    """Return a small version object: commit short hash from GIT_COMMIT env,
    or from the repository, or from a VERSION file."""
    commit = os.environ.get("GIT_COMMIT") or os.environ.get("AIP_GIT_COMMIT")
    if commit:
        return {"commit": str(commit)}
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=APP_DIR, stderr=subprocess.DEVNULL)
        return {"commit": out.decode().strip()}
    except Exception:
        try:
            vfile = APP_DIR / "VERSION"
            if vfile.exists():
                return {"commit": vfile.read_text().strip()}
        except Exception:
            pass
    return {"commit": None}


def source_status() -> dict[str, Any]:
    workflows = query_all(
        """
        SELECT source, COUNT(*) AS total
        FROM workflows
        GROUP BY source
        """
    )
    telemetry = query_all(
        """
        SELECT source, COUNT(*) AS total
        FROM telemetry
        WHERE id IN (SELECT id FROM telemetry ORDER BY id DESC LIMIT 120)
        GROUP BY source
        """
    )
    with RUNTIME_LOCK:
        status = dict(SOURCE_STATE)
    configured = ibm_source_configured()
    status["configured"] = configured
    status["mode"] = SOURCE_MODE
    status["workflow_sources"] = {row["source"]: row["total"] for row in workflows}
    status["recent_telemetry_sources"] = {row["source"]: row["total"] for row in telemetry}
    if status.get("active") == "ibm_real_time":
        label = "IBM real-time"
    elif configured:
        label = "Mock fallback"
    else:
        label = "Mock simulator"
    status["label"] = label
    return status


def upsert_ibm_workflow(app: dict[str, Any]) -> int:
    external_id = app["external_id"]
    existing = query_one(
        "SELECT id FROM workflows WHERE source = 'ibm_application' AND external_id = ?",
        (external_id,),
    )
    if existing:
        execute(
            """
            UPDATE workflows
            SET name = ?, owner = ?, business_criticality = ?, description = ?,
                routing_group = ?, sla_target_ms = ?, telemetry_endpoint = ?
            WHERE id = ?
            """,
            (
                app["name"],
                app["owner"],
                normalize_criticality(app["business_criticality"]),
                app["description"],
                app["routing_group"],
                app["sla_target_ms"],
                app["url"],
                existing["id"],
            ),
        )
        ensure_policy_for_workflow(int(existing["id"]), app["name"], app["business_criticality"])
        return int(existing["id"])

    workflow_id = execute(
        """
        INSERT INTO workflows(
            name, owner, business_criticality, description, routing_group, sla_target_ms,
            status, source, external_id, telemetry_endpoint, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'healthy', 'ibm_application', ?, ?, ?)
        """,
        (
            app["name"],
            app["owner"],
            normalize_criticality(app["business_criticality"]),
            app["description"],
            app["routing_group"],
            app["sla_target_ms"],
            external_id,
            app["url"],
            utc_now(),
        ),
    )
    add_audit(
        "system",
        "ibm_workflow_registered",
        "workflow",
        workflow_id,
        {"name": app["name"], "endpoint": app["url"], "source": "ibm_application"},
    )
    ensure_policy_for_workflow(workflow_id, app["name"], app["business_criticality"])
    return workflow_id


def ensure_policy_for_workflow(workflow_id: int, workflow_name: str, criticality: str) -> None:
    existing = query_one("SELECT id FROM policies WHERE workflow_id = ? LIMIT 1", (workflow_id,))
    if existing:
        return
    normalized = normalize_criticality(criticality)
    approval = 1 if normalized == "mission_critical" else 0
    threshold = 76 if normalized == "mission_critical" else 70
    execute(
        """
        INSERT INTO policies(name, workflow_id, enabled, risk_threshold, human_approval_required, actions, description)
        VALUES (?, ?, 1, ?, ?, ?, ?)
        """,
        (
            f"{workflow_name} live issue policy",
            workflow_id,
            threshold,
            approval,
            json.dumps(["traffic_reroute", "retry_orchestration", "queue_throttle", "adaptive_load_balance"]),
            "Runtime policy for real application issue monitoring.",
        ),
    )


def sync_ibm_workflows() -> None:
    for app in configured_application_probes():
        upsert_ibm_workflow(app)


def normalize_criticality(value: Any) -> str:
    normalized = str(value or "high").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"critical", "missioncritical", "mission_critical"}:
        return "mission_critical"
    if normalized in {"high", "medium", "low"}:
        return normalized
    return "high"


def init_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DB_LOCK:
        with db_connect() as conn:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_database_migrations()
    set_source_state(configured=ibm_source_configured())

    existing = query_one("SELECT COUNT(*) AS total FROM workflows")
    if existing and existing["total"]:
        sync_ibm_workflows()
        collapse_duplicate_pending_actions()
        return

    now = utc_now()
    workflows = [
        (
            "Order-to-Cash API Gateway",
            "Revenue Operations",
            "mission_critical",
            "Customer orders, pricing, invoicing, and fulfillment handoffs across ERP, CRM, and payment services.",
            "commerce-east",
            850,
            "healthy",
            now,
        ),
        (
            "Partner EDI Intake",
            "B2B Integrations",
            "high",
            "Inbound X12 and EDIFACT messages normalized into canonical order and shipment events.",
            "partner-edge",
            1200,
            "healthy",
            now,
        ),
        (
            "Real-Time Inventory Sync",
            "Supply Chain",
            "high",
            "Inventory deltas synchronized between warehouse management, ecommerce, and retail store systems.",
            "inventory-core",
            700,
            "healthy",
            now,
        ),
        (
            "Claims Payment Orchestrator",
            "Finance Platforms",
            "mission_critical",
            "Payment authorization workflow spanning policy, claims, fraud scoring, and banking adapters.",
            "payments-west",
            950,
            "healthy",
            now,
        ),
        (
            "Customer Identity Federation",
            "Identity Engineering",
            "medium",
            "Authentication, token exchange, and identity synchronization for employee and customer applications.",
            "identity-global",
            600,
            "healthy",
            now,
        ),
        (
            "Regulatory Reporting Pipeline",
            "Risk and Compliance",
            "high",
            "Batch and near-real-time compliance feeds across data lake, risk systems, and reporting gateways.",
            "risk-reporting",
            1600,
            "healthy",
            now,
        ),
    ]
    execute_many(
        """
        INSERT INTO workflows(name, owner, business_criticality, description, routing_group, sla_target_ms, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        workflows,
    )

    seeded = query_all("SELECT id, name, business_criticality FROM workflows")
    policies = []
    for workflow in seeded:
        approval = 1 if workflow["business_criticality"] == "mission_critical" else 0
        threshold = 76 if workflow["business_criticality"] == "mission_critical" else 70
        policies.append(
            (
                f"{workflow['name']} resilience policy",
                workflow["id"],
                1,
                threshold,
                approval,
                json.dumps(["traffic_reroute", "retry_orchestration", "queue_throttle", "adaptive_load_balance"]),
                "Policy used by the automation engine to decide when safe recovery actions can run.",
            )
        )

    policies.append(
        (
            "Global critical-action approval",
            None,
            1,
            88,
            1,
            json.dumps(["service_restart", "regional_failover"]),
            "Requires human approval for disruptive recovery actions.",
        )
    )
    execute_many(
        """
        INSERT INTO policies(name, workflow_id, enabled, risk_threshold, human_approval_required, actions, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        policies,
    )
    add_audit(
        "system",
        "seed_complete",
        "platform",
        None,
        {"workflows": len(workflows), "policies": len(policies), "database": str(DB_PATH)},
    )
    sync_ibm_workflows()


def collapse_duplicate_pending_actions() -> None:
    duplicate_groups = query_all(
        """
        SELECT workflow_id, COALESCE(incident_id, 0) AS incident_key, MAX(id) AS keep_id, COUNT(*) AS total
        FROM healing_actions
        WHERE status = 'pending_approval'
        GROUP BY workflow_id, COALESCE(incident_id, 0)
        HAVING COUNT(*) > 1
        """
    )
    for group in duplicate_groups:
        execute(
            """
            UPDATE healing_actions
            SET status = 'failed', result = 'Superseded by a newer pending approval request.'
            WHERE workflow_id = ?
              AND COALESCE(incident_id, 0) = ?
              AND status = 'pending_approval'
              AND id <> ?
            """,
            (group["workflow_id"], group["incident_key"], group["keep_id"]),
        )


def initial_runtime_state(workflow_id: int) -> dict[str, Any]:
    return {
        "stress": random.uniform(0.10, 0.42),
        "next_spike_at": time.time() + random.uniform(10, 40),
        "last_prediction_at": 0.0,
        "last_action_at": 0.0,
        "recovery_samples": 0,
    }


def runtime_for(workflow_id: int) -> dict[str, Any]:
    with RUNTIME_LOCK:
        if workflow_id not in RUNTIME_STATE:
            RUNTIME_STATE[workflow_id] = initial_runtime_state(workflow_id)
        return RUNTIME_STATE[workflow_id]


def reduce_runtime_stress(workflow_id: int, factor: float = 0.48) -> None:
    with RUNTIME_LOCK:
        state = runtime_for(workflow_id)
        state["stress"] = clamp(state["stress"] * factor, 0.08, 0.95)
        state["last_action_at"] = time.time()
        state["next_spike_at"] = time.time() + random.uniform(24, 68)


def workflow_policy(workflow_id: int) -> dict[str, Any]:
    policy = query_one(
        """
        SELECT * FROM policies
        WHERE enabled = 1 AND (workflow_id = ? OR workflow_id IS NULL)
        ORDER BY workflow_id IS NULL, risk_threshold ASC
        LIMIT 1
        """,
        (workflow_id,),
    )
    if not policy:
        return {
            "risk_threshold": 72,
            "human_approval_required": 0,
            "actions": ["traffic_reroute", "retry_orchestration", "queue_throttle", "adaptive_load_balance"],
        }
    policy["actions"] = json.loads(policy["actions"])
    return policy


def score_anomaly(metric: dict[str, Any], sla_target_ms: int) -> dict[str, Any]:
    latency_ratio = metric["latency_ms"] / max(sla_target_ms, 1)
    latency_component = clamp((latency_ratio - 0.55) / 1.15, 0, 1)
    error_component = clamp(metric["error_rate"] / 8.0, 0, 1)
    queue_component = clamp(metric["queue_depth"] / 180.0, 0, 1)
    cpu_component = clamp((metric["cpu_percent"] - 58.0) / 40.0, 0, 1)
    memory_component = clamp((metric["memory_percent"] - 62.0) / 38.0, 0, 1)

    components = {
        "latency": latency_component,
        "error_rate": error_component,
        "queue_depth": queue_component,
        "cpu_pressure": cpu_component,
        "memory_pressure": memory_component,
    }
    weighted = (
        latency_component * 0.30
        + error_component * 0.28
        + queue_component * 0.18
        + cpu_component * 0.13
        + memory_component * 0.11
    )
    leading_driver = max(components, key=components.get)
    burst_bonus = 0.0
    if metric["latency_ms"] > sla_target_ms and metric["error_rate"] > 3.5:
        burst_bonus = 8.0
    score = clamp(weighted * 100.0 + burst_bonus, 0.0, 100.0)

    if score >= 85:
        risk_level = "critical"
    elif score >= 65:
        risk_level = "high"
    elif score >= 40:
        risk_level = "medium"
    else:
        risk_level = "low"

    confidence = clamp(0.55 + (score / 210.0) + (max(components.values()) * 0.12), 0.55, 0.98)
    horizon = int(clamp(50 - score * 0.42, 5, 45))
    return {
        "score": round(score, 1),
        "risk_level": risk_level,
        "driver": leading_driver,
        "confidence": round(confidence, 2),
        "horizon_minutes": horizon,
    }


def generate_metric(workflow: dict[str, Any]) -> dict[str, Any]:
    workflow_id = int(workflow["id"])
    state = runtime_for(workflow_id)

    with RUNTIME_LOCK:
        now = time.time()
        if now >= state["next_spike_at"]:
            state["stress"] = max(state["stress"], random.uniform(0.72, 1.18))
            state["next_spike_at"] = now + random.uniform(42, 96)
        else:
            drift = random.uniform(-0.045, 0.060)
            natural_recovery = -0.020 if state["stress"] > 0.65 else 0.0
            state["stress"] = clamp(state["stress"] + drift + natural_recovery, 0.06, 1.25)

        stress = state["stress"]

    sla = int(workflow["sla_target_ms"])
    criticality_bias = {
        "mission_critical": 1.08,
        "high": 1.02,
        "medium": 0.92,
        "low": 0.86,
    }.get(workflow["business_criticality"], 1.0)

    latency_ms = sla * (0.32 + stress * 1.02) * criticality_bias + random.uniform(-45, 85)
    error_rate = max(0.0, (stress**2.2) * 7.0 + random.uniform(-0.45, 0.90))
    throughput = max(25.0, 470.0 * (1.0 - min(stress * 0.43, 0.68)) + random.uniform(-38, 58))
    queue_depth = max(0, int(stress * 155 + random.uniform(-18, 32)))
    cpu_percent = clamp(34.0 + stress * 47.0 + random.uniform(-5, 8), 12, 99)
    memory_percent = clamp(40.0 + stress * 39.0 + random.uniform(-4, 7), 18, 99)

    status = "healthy"
    if stress >= 0.92 or error_rate > 6.2:
        status = "failing"
    elif stress >= 0.58 or latency_ms > sla:
        status = "degraded"

    return {
        "workflow_id": workflow_id,
        "recorded_at": utc_now(),
        "latency_ms": round(max(60.0, latency_ms), 1),
        "error_rate": round(error_rate, 2),
        "throughput": round(throughput, 1),
        "queue_depth": queue_depth,
        "cpu_percent": round(cpu_percent, 1),
        "memory_percent": round(memory_percent, 1),
        "status": status,
        "source": "mock_simulator" if workflow.get("source") == "mock" else "mock_fallback",
        "raw_payload": None,
    }


def json_default(value: Any) -> str:
    return str(value)


def compact_json(value: Any, max_length: int = 2400) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, default=json_default)
    except TypeError:
        raw = str(value)
    return raw[:max_length]


def build_ibm_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    extra_headers = parse_json_env("IBM_TELEMETRY_HEADERS", {})
    if isinstance(extra_headers, dict):
        headers.update({str(key): str(value) for key, value in extra_headers.items()})
    if IBM_TELEMETRY_TOKEN and "Authorization" not in headers and "authorization" not in {key.lower() for key in headers}:
        scheme = os.environ.get("IBM_TELEMETRY_AUTH_SCHEME", "Bearer").strip()
        headers["Authorization"] = f"{scheme} {IBM_TELEMETRY_TOKEN}" if scheme else IBM_TELEMETRY_TOKEN
    return headers


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers=build_ibm_headers(), method="GET")
    with urllib.request.urlopen(request, timeout=IBM_REQUEST_TIMEOUT_SECONDS) as response:
        content = response.read(512_000)
    if not content:
        return {}
    return json.loads(content.decode("utf-8"))


def recursive_numbers(payload: Any, names: set[str]) -> float | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in names and isinstance(value, (int, float)):
                return float(value)
            if normalized in names and isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    pass
        for value in payload.values():
            found = recursive_numbers(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = recursive_numbers(item, names)
            if found is not None:
                return found
    return None


def recursive_text(payload: Any, names: set[str]) -> str | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in names and value is not None:
                return str(value)
        for value in payload.values():
            found = recursive_text(value, names)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = recursive_text(item, names)
            if found:
                return found
    return None


def normalize_metric_status(value: Any, latency_ms: float, error_rate: float, sla_target_ms: int) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"failing", "failed", "critical", "down", "unavailable", "red"}:
        return "failing"
    if raw in {"degraded", "warning", "warn", "yellow", "partial"}:
        return "degraded"
    if raw in {"healthy", "ok", "up", "green", "available", "normal"}:
        return "healthy"
    if error_rate >= 6.5 or latency_ms >= sla_target_ms * 1.45:
        return "failing"
    if error_rate >= 2.5 or latency_ms >= sla_target_ms:
        return "degraded"
    return "healthy"


def normalize_feed_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("telemetry", "applications", "workflows", "services", "items", "data", "events", "snapshots"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def workflow_from_feed_item(item: dict[str, Any]) -> dict[str, Any]:
    name = (
        item.get("workflow_name")
        or item.get("application_name")
        or item.get("applicationName")
        or item.get("service_name")
        or item.get("serviceName")
        or item.get("name")
        or "IBM Monitored Application"
    )
    external_id = (
        item.get("workflow_id")
        or item.get("application_id")
        or item.get("applicationId")
        or item.get("service_id")
        or item.get("serviceId")
        or item.get("id")
        or name
    )
    return {
        "name": str(name),
        "owner": str(item.get("owner") or "IBM Applications"),
        "business_criticality": normalize_criticality(item.get("business_criticality") or item.get("criticality") or "high"),
        "description": str(item.get("description") or "IBM real-time telemetry feed item."),
        "routing_group": str(item.get("routing_group") or item.get("group") or "ibm-telemetry"),
        "sla_target_ms": int(item.get("sla_target_ms") or item.get("sla") or 1000),
        "external_id": str(external_id),
    }


def upsert_feed_workflow(item: dict[str, Any]) -> dict[str, Any]:
    workflow = workflow_from_feed_item(item)
    existing = query_one(
        "SELECT * FROM workflows WHERE source = 'ibm_telemetry' AND external_id = ?",
        (workflow["external_id"],),
    )
    if existing:
        execute(
            """
            UPDATE workflows
            SET name = ?, owner = ?, business_criticality = ?, description = ?,
                routing_group = ?, sla_target_ms = ?
            WHERE id = ?
            """,
            (
                workflow["name"],
                workflow["owner"],
                workflow["business_criticality"],
                workflow["description"],
                workflow["routing_group"],
                workflow["sla_target_ms"],
                existing["id"],
            ),
        )
        return query_one("SELECT * FROM workflows WHERE id = ?", (existing["id"],)) or existing

    workflow_id = execute(
        """
        INSERT INTO workflows(
            name, owner, business_criticality, description, routing_group, sla_target_ms,
            status, source, external_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'healthy', 'ibm_telemetry', ?, ?)
        """,
        (
            workflow["name"],
            workflow["owner"],
            workflow["business_criticality"],
            workflow["description"],
            workflow["routing_group"],
            workflow["sla_target_ms"],
            workflow["external_id"],
            utc_now(),
        ),
    )
    add_audit(
        "system",
        "ibm_workflow_registered",
        "workflow",
        workflow_id,
        {"name": workflow["name"], "source": "ibm_telemetry"},
    )
    return query_one("SELECT * FROM workflows WHERE id = ?", (workflow_id,)) or {}


def metric_from_feed_item(item: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    sla = int(workflow["sla_target_ms"])
    latency_ms = recursive_numbers(
        item,
        {"latencyms", "latency", "responsetimems", "responsetime", "durationms", "duration", "averagelatency"},
    )
    error_rate = recursive_numbers(item, {"errorrate", "errorpercentage", "errors", "failureRate".lower()})
    throughput = recursive_numbers(item, {"throughput", "requestspersecond", "rps", "tps", "rate"})
    queue_depth = recursive_numbers(item, {"queuedepth", "backlog", "messagesqueued", "lag"})
    cpu_percent = recursive_numbers(item, {"cpupercent", "cpu", "cpuusage", "processorusage"})
    memory_percent = recursive_numbers(item, {"memorypercent", "memory", "memoryusage", "heapusage"})
    status_text = recursive_text(item, {"status", "health", "state", "availability"})

    latency_ms = float(latency_ms if latency_ms is not None else sla * 0.55)
    error_rate = float(error_rate if error_rate is not None else 0.1)
    throughput = float(throughput if throughput is not None else 420.0)
    queue_depth = int(queue_depth if queue_depth is not None else 12)
    cpu_percent = float(cpu_percent if cpu_percent is not None else 45.0)
    memory_percent = float(memory_percent if memory_percent is not None else 50.0)
    status = normalize_metric_status(status_text, latency_ms, error_rate, sla)

    return {
        "workflow_id": int(workflow["id"]),
        "recorded_at": str(item.get("recorded_at") or item.get("timestamp") or utc_now()),
        "latency_ms": round(max(1.0, latency_ms), 1),
        "error_rate": round(clamp(error_rate, 0.0, 100.0), 2),
        "throughput": round(max(0.0, throughput), 1),
        "queue_depth": max(0, int(queue_depth)),
        "cpu_percent": round(clamp(cpu_percent, 0.0, 100.0), 1),
        "memory_percent": round(clamp(memory_percent, 0.0, 100.0), 1),
        "status": status,
        "source": "ibm_telemetry",
        "raw_payload": compact_json(item),
    }


def collect_ibm_feed_metrics() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if SOURCE_MODE == "mock" or not IBM_TELEMETRY_URL:
        return []
    payload = fetch_json(IBM_TELEMETRY_URL)
    samples: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in normalize_feed_items(payload):
        workflow = upsert_feed_workflow(item)
        if workflow:
            samples.append((workflow, metric_from_feed_item(item, workflow)))
    return samples


def probe_application(workflow: dict[str, Any]) -> dict[str, Any]:
    url = workflow.get("telemetry_endpoint")
    if not url:
        raise ValueError(f"No telemetry endpoint configured for {workflow['name']}")

    started = time.perf_counter()
    status_code = 0
    payload: Any = {}
    try:
        request = urllib.request.Request(str(url), headers=build_ibm_headers(), method="GET")
        with urllib.request.urlopen(request, timeout=IBM_REQUEST_TIMEOUT_SECONDS) as response:
            status_code = int(response.status)
            content_type = response.headers.get("Content-Type", "")
            body = response.read(256_000)
        latency_ms = (time.perf_counter() - started) * 1000.0
        if body and "json" in content_type.lower():
            payload = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        latency_ms = (time.perf_counter() - started) * 1000.0
        try:
            payload = json.loads(exc.read(128_000).decode("utf-8"))
        except Exception:
            payload = {"status_code": status_code, "error": str(exc)}
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        payload = {
            "status_code": status_code,
            "error": str(exc),
            "endpoint": url,
            "issue": "connection_failed",
        }

    sla = int(workflow["sla_target_ms"])
    connection_failed = status_code == 0 or str(payload.get("issue", "")) == "connection_failed"
    http_failed = status_code >= 400
    measured_latency = recursive_numbers(payload, {"latencyms", "latency", "responsetime", "responsetimems"})
    latency = float(measured_latency if measured_latency is not None else latency_ms)
    if connection_failed:
        latency = max(latency, sla * 2.5)
    elif http_failed:
        latency = max(latency, sla * 1.7)
    error_rate = recursive_numbers(payload, {"errorrate", "errorpercentage", "errors"})
    if error_rate is None:
        error_rate = 12.0 if connection_failed else (8.5 if http_failed else 0.1)
    throughput = recursive_numbers(payload, {"throughput", "requestspersecond", "rps", "tps"})
    if throughput is None:
        throughput = 0.0 if connection_failed else (25.0 if http_failed else 430.0)
    queue_depth = recursive_numbers(payload, {"queuedepth", "backlog", "messagesqueued", "lag"})
    if queue_depth is None:
        queue_depth = 220 if connection_failed else (160 if http_failed else 8)
    cpu_percent = recursive_numbers(payload, {"cpupercent", "cpu", "cpuusage"})
    if cpu_percent is None:
        cpu_percent = 95.0 if connection_failed else (88.0 if http_failed else 45.0)
    memory_percent = recursive_numbers(payload, {"memorypercent", "memory", "memoryusage"})
    if memory_percent is None:
        memory_percent = 92.0 if connection_failed else (86.0 if http_failed else 52.0)
    status_text = recursive_text(payload, {"status", "health", "state", "availability"}) or (
        str(status_code) if status_code else "down"
    )
    status = normalize_metric_status(status_text, latency, float(error_rate), sla)

    return {
        "workflow_id": int(workflow["id"]),
        "recorded_at": utc_now(),
        "latency_ms": round(max(1.0, latency), 1),
        "error_rate": round(clamp(float(error_rate), 0.0, 100.0), 2),
        "throughput": round(float(throughput), 1),
        "queue_depth": max(0, int(queue_depth)),
        "cpu_percent": round(clamp(float(cpu_percent), 0.0, 100.0), 1),
        "memory_percent": round(clamp(float(memory_percent), 0.0, 100.0), 1),
        "status": status,
        "source": "ibm_application",
        "raw_payload": compact_json({"status_code": status_code, "endpoint": url, "body": payload}),
    }


def collect_application_probe_metrics() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if SOURCE_MODE == "mock":
        return []
    sync_ibm_workflows()
    workflows = query_all(
        """
        SELECT * FROM workflows
        WHERE source = 'ibm_application' AND telemetry_endpoint IS NOT NULL AND telemetry_endpoint <> ''
        ORDER BY id
        """
    )
    samples = []
    for workflow in workflows:
        samples.append((workflow, probe_application(workflow)))
    return samples


def collect_real_metrics() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if SOURCE_MODE == "mock":
        set_source_state(active="mock_simulator", last_checked=utc_now())
        return []
    samples: list[tuple[dict[str, Any], dict[str, Any]]] = []
    errors = []
    for collector in (collect_ibm_feed_metrics, collect_application_probe_metrics):
        try:
            samples.extend(collector())
        except Exception as exc:
            errors.append(str(exc))
    if samples:
        set_source_state(active="ibm_real_time", last_success=utc_now(), last_error=None, ibm_samples=SOURCE_STATE["ibm_samples"] + len(samples))
    elif errors:
        set_source_state(active="mock_fallback", last_error="; ".join(errors), last_checked=utc_now())
    else:
        active = "mock_fallback" if ibm_source_configured() else "mock_simulator"
        set_source_state(active=active, last_error=None, last_checked=utc_now())
    return samples


def action_for_driver(driver: str, allowed_actions: list[str]) -> str:
    preferred = {
        "latency": "traffic_reroute",
        "error_rate": "retry_orchestration",
        "queue_depth": "queue_throttle",
        "cpu_pressure": "adaptive_load_balance",
        "memory_pressure": "adaptive_load_balance",
    }.get(driver, "traffic_reroute")
    if preferred in allowed_actions:
        return preferred
    return allowed_actions[0] if allowed_actions else "traffic_reroute"


def action_label(action_type: str) -> str:
    labels = {
        "traffic_reroute": "Intelligent reroute",
        "retry_orchestration": "Retry orchestration",
        "queue_throttle": "Queue throttling",
        "adaptive_load_balance": "Adaptive load balancing",
        "service_restart": "Service recovery",
        "regional_failover": "Regional failover",
        "manual_reroute": "Manual reroute",
    }
    return labels.get(action_type, action_type.replace("_", " ").title())


def insert_prediction(workflow: dict[str, Any], risk: dict[str, Any]) -> None:
    workflow_id = int(workflow["id"])
    state = runtime_for(workflow_id)
    now = time.time()
    if risk["score"] < 35 and now - state["last_prediction_at"] < 16:
        return
    if risk["score"] >= 35 and now - state["last_prediction_at"] < 8:
        return

    execute(
        """
        INSERT INTO predictions(workflow_id, created_at, risk_score, risk_level, horizon_minutes, confidence, driver, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
        """,
        (
            workflow_id,
            utc_now(),
            risk["score"],
            risk["risk_level"],
            risk["horizon_minutes"],
            risk["confidence"],
            risk["driver"],
        ),
    )
    with RUNTIME_LOCK:
        state["last_prediction_at"] = now


def active_incident_for(workflow_id: int) -> dict[str, Any] | None:
    return query_one(
        """
        SELECT * FROM incidents
        WHERE workflow_id = ? AND status IN ('open', 'mitigating')
        ORDER BY opened_at DESC
        LIMIT 1
        """,
        (workflow_id,),
    )


def ensure_incident(workflow: dict[str, Any], risk: dict[str, Any], metric: dict[str, Any]) -> dict[str, Any]:
    workflow_id = int(workflow["id"])
    incident = active_incident_for(workflow_id)
    if incident:
        execute(
            """
            UPDATE incidents
            SET severity = ?, status = 'mitigating', sla_impact_minutes = ?
            WHERE id = ?
            """,
            (
                "critical" if risk["risk_level"] == "critical" else "high",
                round(max(0.0, metric["latency_ms"] - workflow["sla_target_ms"]) / 1000.0, 2),
                incident["id"],
            ),
        )
        incident["severity"] = "critical" if risk["risk_level"] == "critical" else "high"
        incident["status"] = "mitigating"
        return incident

    severity = "critical" if risk["risk_level"] == "critical" else "high"
    title = f"{workflow['name']} predicted {severity} failure"
    summary = (
        f"Risk score {risk['score']} driven by {risk['driver']} with "
        f"{risk['horizon_minutes']} minute failure horizon."
    )
    incident_id = execute(
        """
        INSERT INTO incidents(workflow_id, opened_at, severity, status, title, summary, sla_impact_minutes, detection_source)
        VALUES (?, ?, ?, 'open', ?, ?, ?, 'predictive_anomaly_detector')
        """,
        (
            workflow_id,
            utc_now(),
            severity,
            title,
            summary,
            round(max(0.0, metric["latency_ms"] - workflow["sla_target_ms"]) / 1000.0, 2),
        ),
    )
    execute("UPDATE workflows SET status = ? WHERE id = ?", ("failing" if severity == "critical" else "degraded", workflow_id))
    add_audit(
        "automation",
        "incident_opened",
        "incident",
        incident_id,
        {"workflow": workflow["name"], "risk_score": risk["score"], "driver": risk["driver"]},
    )
    return query_one("SELECT * FROM incidents WHERE id = ?", (incident_id,)) or {}


def execute_healing_action(
    workflow: dict[str, Any],
    incident: dict[str, Any] | None,
    action_type: str,
    risk: dict[str, Any],
    actor: str = "automation",
    force_complete: bool = False,
) -> dict[str, Any]:
    workflow_id = int(workflow["id"])
    policy = workflow_policy(workflow_id)
    high_risk_action = risk["risk_level"] == "critical" and action_type in {"service_restart", "regional_failover"}
    needs_approval = bool(policy.get("human_approval_required")) and (risk["score"] >= float(policy.get("risk_threshold", 80)))
    needs_approval = needs_approval or high_risk_action
    status = "completed" if force_complete or not needs_approval else "pending_approval"
    summary = f"{action_label(action_type)} selected for {workflow['name']}."
    if status == "pending_approval":
        result = "Awaiting approval because policy marks this as a high-risk recovery action."
    else:
        result = "Action executed and runtime pressure reduced by automation engine."

    action_id = execute(
        """
        INSERT INTO healing_actions(
            workflow_id, incident_id, created_at, action_type, status, confidence,
            human_approval_required, summary, result
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workflow_id,
            incident["id"] if incident else None,
            utc_now(),
            action_type,
            status,
            risk["confidence"],
            1 if needs_approval else 0,
            summary,
            result,
        ),
    )

    with RUNTIME_LOCK:
        runtime_for(workflow_id)["last_action_at"] = time.time()

    if status == "completed":
        reduce_runtime_stress(workflow_id)
        execute("UPDATE workflows SET status = 'recovering' WHERE id = ?", (workflow_id,))

    add_audit(
        actor,
        "healing_action_created",
        "healing_action",
        action_id,
        {
            "workflow": workflow["name"],
            "action_type": action_type,
            "status": status,
            "risk_score": risk["score"],
            "approval_required": bool(needs_approval),
        },
    )
    return query_one("SELECT * FROM healing_actions WHERE id = ?", (action_id,)) or {}


def maybe_heal(workflow: dict[str, Any], risk: dict[str, Any], incident: dict[str, Any]) -> None:
    workflow_id = int(workflow["id"])
    state = runtime_for(workflow_id)
    now = time.time()
    if now - state["last_action_at"] < 14:
        return

    policy = workflow_policy(workflow_id)
    threshold = float(policy.get("risk_threshold", 70))
    if risk["score"] < threshold:
        return

    pending = query_one(
        """
        SELECT id FROM healing_actions
        WHERE workflow_id = ? AND status = 'pending_approval'
        ORDER BY id DESC
        LIMIT 1
        """,
        (workflow_id,),
    )
    if pending:
        return

    allowed_actions = policy.get("actions", [])
    action_type = action_for_driver(risk["driver"], allowed_actions)
    if risk["risk_level"] == "critical" and random.random() < 0.25:
        action_type = "service_restart"
    execute_healing_action(workflow, incident, action_type, risk)


def maybe_close_incident(workflow: dict[str, Any], risk: dict[str, Any]) -> None:
    workflow_id = int(workflow["id"])
    incident = active_incident_for(workflow_id)
    if not incident:
        return

    state = runtime_for(workflow_id)
    with RUNTIME_LOCK:
        if risk["score"] < 38:
            state["recovery_samples"] += 1
        else:
            state["recovery_samples"] = 0

        ready = state["recovery_samples"] >= 3

    if not ready:
        return

    execute(
        """
        UPDATE incidents
        SET status = 'resolved', closed_at = ?
        WHERE id = ?
        """,
        (utc_now(), incident["id"]),
    )
    execute("UPDATE workflows SET status = 'healthy' WHERE id = ?", (workflow_id,))
    execute("UPDATE predictions SET status = 'mitigated' WHERE workflow_id = ? AND status = 'open'", (workflow_id,))
    add_audit(
        "automation",
        "incident_resolved",
        "incident",
        incident["id"],
        {"workflow": workflow["name"], "reason": "three stable telemetry samples"},
    )
    with RUNTIME_LOCK:
        state["recovery_samples"] = 0


def record_metric(workflow: dict[str, Any], metric: dict[str, Any] | None = None) -> None:
    metric = metric or generate_metric(workflow)
    risk = score_anomaly(metric, int(workflow["sla_target_ms"]))
    metric["anomaly_score"] = risk["score"]
    metric["driver"] = risk["driver"]

    execute(
        """
        INSERT INTO telemetry(
            workflow_id, recorded_at, latency_ms, error_rate, throughput, queue_depth,
            cpu_percent, memory_percent, status, anomaly_score, driver, source, raw_payload
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metric["workflow_id"],
            metric["recorded_at"],
            metric["latency_ms"],
            metric["error_rate"],
            metric["throughput"],
            metric["queue_depth"],
            metric["cpu_percent"],
            metric["memory_percent"],
            metric["status"],
            metric["anomaly_score"],
            metric["driver"],
            metric.get("source") or workflow.get("source") or "mock",
            metric.get("raw_payload"),
        ),
    )

    insert_prediction(workflow, risk)

    if risk["risk_level"] in {"high", "critical"}:
        incident = ensure_incident(workflow, risk, metric)
        maybe_heal(workflow, risk, incident)
    else:
        execute("UPDATE workflows SET status = ? WHERE id = ?", (metric["status"], workflow["id"]))
        maybe_close_incident(workflow, risk)


def prune_old_rows() -> None:
    execute(
        """
        DELETE FROM telemetry
        WHERE id NOT IN (SELECT id FROM telemetry ORDER BY id DESC LIMIT 2500)
        """
    )
    execute(
        """
        DELETE FROM predictions
        WHERE id NOT IN (SELECT id FROM predictions ORDER BY id DESC LIMIT 900)
        """
    )


def telemetry_worker(stop_event: threading.Event) -> None:
    cycle = 0
    while not stop_event.is_set():
        try:
            workflows = query_all("SELECT * FROM workflows ORDER BY id")
            real_samples = collect_real_metrics()
            real_workflow_ids = set()
            for workflow, metric in real_samples:
                real_workflow_ids.add(int(workflow["id"]))
                record_metric(workflow, metric)

            mock_allowed = SOURCE_MODE != "ibm_only"
            if real_samples:
                mock_workflows = [
                    workflow for workflow in workflows
                    if workflow.get("source") == "mock" and int(workflow["id"]) not in real_workflow_ids
                ]
            else:
                mock_workflows = workflows if mock_allowed else []

            for workflow in mock_workflows:
                record_metric(workflow)
            if mock_workflows:
                with RUNTIME_LOCK:
                    SOURCE_STATE["mock_samples"] += len(mock_workflows)
            cycle += 1
            if cycle % 40 == 0:
                prune_old_rows()
        except Exception as exc:  # pragma: no cover - kept visible for demo reliability.
            print(f"[telemetry-worker] {exc}")
            traceback.print_exc()
        stop_event.wait(2.0)


LATEST_TELEMETRY_JOIN = """
LEFT JOIN telemetry t ON t.id = (
    SELECT id FROM telemetry WHERE workflow_id = w.id ORDER BY id DESC LIMIT 1
)
"""

LATEST_PREDICTION_JOIN = """
LEFT JOIN predictions p ON p.id = (
    SELECT id FROM predictions WHERE workflow_id = w.id ORDER BY id DESC LIMIT 1
)
"""


def get_workflows() -> list[dict[str, Any]]:
    rows = query_all(
        f"""
        SELECT
            w.*,
            t.recorded_at AS telemetry_at,
            t.latency_ms,
            t.error_rate,
            t.throughput,
            t.queue_depth,
            t.cpu_percent,
            t.memory_percent,
            t.anomaly_score,
            t.anomaly_score AS risk_score,
            t.driver AS latest_driver,
            t.source AS telemetry_source,
            p.risk_score AS predicted_risk_score,
            p.risk_level AS predicted_risk_level,
            p.horizon_minutes,
            p.confidence,
            (
                SELECT COUNT(*) FROM incidents i
                WHERE i.workflow_id = w.id AND i.status IN ('open', 'mitigating')
            ) AS active_incidents
        FROM workflows w
        {LATEST_TELEMETRY_JOIN}
        {LATEST_PREDICTION_JOIN}
        ORDER BY
            CASE w.business_criticality
                WHEN 'mission_critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'medium' THEN 3
                ELSE 4
            END,
            w.name
        """
    )
    return rows


def get_summary() -> dict[str, Any]:
    workflows = get_workflows()
    active_incidents = query_one(
        "SELECT COUNT(*) AS total FROM incidents WHERE status IN ('open', 'mitigating')"
    )["total"]
    action_scope = "NOT (status = 'failed' AND result LIKE 'Superseded%')"
    actions_total = query_one(f"SELECT COUNT(*) AS total FROM healing_actions WHERE {action_scope}")["total"]
    actions_completed = query_one(
        f"SELECT COUNT(*) AS total FROM healing_actions WHERE status = 'completed' AND {action_scope}"
    )["total"]
    pending_approvals = query_one(
        "SELECT COUNT(*) AS total FROM healing_actions WHERE status = 'pending_approval'"
    )["total"]

    risk_scores = [float(item["risk_score"] or item["anomaly_score"] or 0) for item in workflows]
    avg_risk = round(sum(risk_scores) / len(risk_scores), 1) if risk_scores else 0
    avg_latency = round(
        sum(float(item["latency_ms"] or 0) for item in workflows) / len(workflows), 1
    ) if workflows else 0
    at_risk = sum(1 for item in workflows if float(item["risk_score"] or item["anomaly_score"] or 0) >= 65)
    status_counts: dict[str, int] = {"healthy": 0, "degraded": 0, "failing": 0, "recovering": 0}
    for item in workflows:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1

    success_rate = 100.0
    if actions_total:
        success_rate = round((actions_completed / actions_total) * 100.0, 1)

    return {
        "generated_at": utc_now(),
        "uptime_seconds": int(time.time() - STARTED_AT),
        "workflow_count": len(workflows),
        "active_incidents": int(active_incidents),
        "pending_approvals": int(pending_approvals),
        "avg_risk_score": avg_risk,
        "avg_latency_ms": avg_latency,
        "sla_at_risk": at_risk,
        "auto_heal_success_rate": success_rate,
        "status_counts": status_counts,
        "source_status": source_status(),
        "top_risk_workflows": sorted(
            workflows,
            key=lambda item: float(item["risk_score"] or item["anomaly_score"] or 0),
            reverse=True,
        )[:4],
    }


def get_telemetry(params: dict[str, list[str]]) -> list[dict[str, Any]]:
    limit = int(params.get("limit", ["120"])[0])
    limit = int(clamp(limit, 1, 500))
    workflow_id = params.get("workflow_id", [""])[0]

    where = ""
    bind: tuple[Any, ...]
    if workflow_id and workflow_id != "all":
        where = "WHERE t.workflow_id = ?"
        bind = (workflow_id, limit)
    else:
        bind = (limit,)

    return query_all(
        f"""
        SELECT t.*, w.name AS workflow_name, w.sla_target_ms, w.source AS workflow_source
        FROM telemetry t
        JOIN workflows w ON w.id = t.workflow_id
        {where}
        ORDER BY t.id DESC
        LIMIT ?
        """,
        bind,
    )


def get_predictions(limit: int = 40) -> list[dict[str, Any]]:
    return query_all(
        """
        SELECT p.*, w.name AS workflow_name, w.business_criticality
        FROM predictions p
        JOIN workflows w ON w.id = p.workflow_id
        ORDER BY p.id DESC
        LIMIT ?
        """,
        (limit,),
    )


def get_incidents(status: str | None = None) -> list[dict[str, Any]]:
    where = ""
    params: tuple[Any, ...] = ()
    if status and status != "all":
        where = "WHERE i.status = ?"
        params = (status,)
    return query_all(
        f"""
        SELECT i.*, w.name AS workflow_name, w.owner, w.routing_group
        FROM incidents i
        JOIN workflows w ON w.id = i.workflow_id
        {where}
        ORDER BY
            CASE i.status WHEN 'open' THEN 1 WHEN 'mitigating' THEN 2 ELSE 3 END,
            i.opened_at DESC
        LIMIT 80
        """,
        params,
    )


def get_actions(limit: int = 60) -> list[dict[str, Any]]:
    return query_all(
        """
        SELECT a.*, w.name AS workflow_name, i.title AS incident_title
        FROM healing_actions a
        JOIN workflows w ON w.id = a.workflow_id
        LEFT JOIN incidents i ON i.id = a.incident_id
        WHERE NOT (a.status = 'failed' AND a.result LIKE 'Superseded%')
        ORDER BY a.id DESC
        LIMIT ?
        """,
        (limit,),
    )


def get_policies() -> list[dict[str, Any]]:
    rows = query_all(
        """
        SELECT p.*, w.name AS workflow_name
        FROM policies p
        LEFT JOIN workflows w ON w.id = p.workflow_id
        ORDER BY p.workflow_id IS NULL, p.name
        """
    )
    for row in rows:
        row["actions"] = json.loads(row["actions"])
    return rows


def approve_action(action_id: int) -> dict[str, Any]:
    action = query_one(
        """
        SELECT a.*, w.name AS workflow_name
        FROM healing_actions a
        JOIN workflows w ON w.id = a.workflow_id
        WHERE a.id = ?
        """,
        (action_id,),
    )
    if not action:
        raise ApiError(404, "Healing action not found")
    if action["status"] != "pending_approval":
        return action

    execute(
        """
        UPDATE healing_actions
        SET status = 'completed', result = ?
        WHERE id = ?
        """,
        ("Approved by operator and executed successfully.", action_id),
    )
    reduce_runtime_stress(int(action["workflow_id"]))
    execute("UPDATE workflows SET status = 'recovering' WHERE id = ?", (action["workflow_id"],))
    add_audit(
        "operator",
        "healing_action_approved",
        "healing_action",
        action_id,
        {"workflow": action["workflow_name"], "action_type": action["action_type"]},
    )
    return query_one("SELECT * FROM healing_actions WHERE id = ?", (action_id,)) or {}


def remediate_incident(incident_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    incident = query_one(
        """
        SELECT *
        FROM incidents
        WHERE id = ?
        """,
        (incident_id,),
    )
    if not incident:
        raise ApiError(404, "Incident not found")

    workflow = query_one("SELECT * FROM workflows WHERE id = ?", (incident["workflow_id"],))
    if not workflow:
        raise ApiError(404, "Workflow not found")

    action_type = payload.get("action_type") or "manual_reroute"
    risk = {
        "risk_level": incident["severity"],
        "score": 88 if incident["severity"] == "critical" else 72,
        "confidence": 0.91,
    }
    action = execute_healing_action(
        workflow,
        incident,
        action_type,
        risk,
        actor="operator",
        force_complete=True,
    )
    execute("UPDATE incidents SET status = 'mitigating' WHERE id = ?", (incident_id,))
    return action


def toggle_policy(policy_id: int) -> dict[str, Any]:
    policy = query_one("SELECT * FROM policies WHERE id = ?", (policy_id,))
    if not policy:
        raise ApiError(404, "Policy not found")
    enabled = 0 if policy["enabled"] else 1
    execute("UPDATE policies SET enabled = ? WHERE id = ?", (enabled, policy_id))
    add_audit(
        "operator",
        "policy_toggled",
        "policy",
        policy_id,
        {"enabled": bool(enabled), "name": policy["name"]},
    )
    return query_one("SELECT * FROM policies WHERE id = ?", (policy_id,)) or {}


def get_application_monitors() -> list[dict[str, Any]]:
    return query_all(
        f"""
        SELECT
            w.*,
            t.recorded_at AS telemetry_at,
            t.latency_ms,
            t.error_rate,
            t.status AS telemetry_status,
            t.anomaly_score,
            t.driver,
            t.source AS telemetry_source,
            (
                SELECT COUNT(*) FROM incidents i
                WHERE i.workflow_id = w.id AND i.status IN ('open', 'mitigating')
            ) AS active_incidents
        FROM workflows w
        {LATEST_TELEMETRY_JOIN}
        WHERE w.source = 'ibm_application'
        ORDER BY w.created_at DESC
        """
    )


def create_application_monitor(payload: dict[str, Any]) -> dict[str, Any]:
    url = str(payload.get("url") or payload.get("telemetry_endpoint") or "").strip()
    if not url:
        raise ApiError(400, "Application URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError(400, "Application URL must be a valid http or https URL")

    name = str(payload.get("name") or parsed.netloc or "Real Application").strip()
    sla_target_ms = int(payload.get("sla_target_ms") or payload.get("sla") or 1000)
    if sla_target_ms < 50 or sla_target_ms > 60000:
        raise ApiError(400, "SLA target must be between 50 and 60000 milliseconds")

    app = {
        "name": name,
        "url": url,
        "owner": str(payload.get("owner") or "Application Operations").strip(),
        "business_criticality": normalize_criticality(payload.get("business_criticality") or payload.get("criticality") or "high"),
        "description": str(payload.get("description") or "Real application endpoint monitored for live issues."),
        "routing_group": str(payload.get("routing_group") or "real-application").strip(),
        "sla_target_ms": sla_target_ms,
        "external_id": str(payload.get("external_id") or url),
    }
    workflow_id = upsert_ibm_workflow(app)
    workflow = query_one("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
    if not workflow:
        raise ApiError(500, "Application monitor could not be created")

    metric = probe_application(workflow)
    record_metric(workflow, metric)
    set_source_state(active="ibm_real_time", configured=True, last_success=utc_now())
    add_audit(
        "operator",
        "application_monitor_created",
        "workflow",
        workflow_id,
        {"name": app["name"], "url": url, "sla_target_ms": sla_target_ms},
    )
    return query_one("SELECT * FROM workflows WHERE id = ?", (workflow_id,)) or workflow


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class PlatformHandler(BaseHTTPRequestHandler):
    server_version = "AutonomousIntegrationPlatform/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def _require_api_token(self, path: str) -> None:
        """Require a bearer token for POST endpoints when AIP_API_TOKEN is set.

        Public read endpoints remain accessible. Mutating endpoints (POST) require
        the header: Authorization: Bearer <token>
        """
        # Only enforce for mutating endpoints
        if not AIP_API_TOKEN:
            return
        # Allowed POST paths that are public could be enumerated here; for now require token for all POST
        auth = self.headers.get("Authorization", "") or ""
        if not auth.startswith("Bearer "):
            raise ApiError(401, "Authorization required")
        token = auth.split(None, 1)[1].strip()
        if token != AIP_API_TOKEN:
            raise ApiError(403, "Invalid API token")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/events":
            self.stream_events()
            return
        if parsed.path.startswith("/api/"):
            self.handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            # If an API token is configured, require it for mutating endpoints.
            # Place this inside the try block so ApiError is caught and returned
            # as a JSON error response instead of bubbling up.
            self._require_api_token(parsed.path)
            payload = self.read_json_body()
            incident_match = re.fullmatch(r"/api/incidents/(\d+)/remediate", parsed.path)
            action_match = re.fullmatch(r"/api/actions/(\d+)/approve", parsed.path)
            policy_match = re.fullmatch(r"/api/policies/(\d+)/toggle", parsed.path)
            application_match = parsed.path == "/api/applications"
            if incident_match:
                self.send_json(remediate_incident(int(incident_match.group(1)), payload))
                return
            if action_match:
                self.send_json(approve_action(int(action_match.group(1))))
                return
            if policy_match:
                self.send_json(toggle_policy(int(policy_match.group(1))))
                return
            if application_match:
                self.send_json(create_application_monitor(payload), 201)
                return
            raise ApiError(404, "Endpoint not found")
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": str(exc)}, 500)

    def handle_api_get(self, path: str, params: dict[str, list[str]]) -> None:
        try:
            if path == "/api/health":
                self.send_json(
                    {
                        "status": "ok",
                        "service": "autonomous-integration-platform",
                        "database": str(DB_PATH),
                        "uptime_seconds": int(time.time() - STARTED_AT),
                        "source_status": source_status(),
                    }
                )
            elif path == "/api/summary":
                self.send_json(get_summary())
            elif path == "/api/version":
                self.send_json(get_version())
            elif path == "/api/workflows":
                self.send_json(get_workflows())
            elif path == "/api/telemetry":
                self.send_json(get_telemetry(params))
            elif path == "/api/predictions":
                self.send_json(get_predictions())
            elif path == "/api/incidents":
                self.send_json(get_incidents(params.get("status", [None])[0]))
            elif path == "/api/actions":
                self.send_json(get_actions())
            elif path == "/api/policies":
                self.send_json(get_policies())
            elif path == "/api/applications":
                self.send_json(get_application_monitors())
            elif path == "/api/audit":
                self.send_json(
                    query_all(
                        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 80"
                    )
                )
            elif path == "/api/source":
                self.send_json(source_status())
            else:
                raise ApiError(404, "Endpoint not found")
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:
            traceback.print_exc()
            self.send_json({"error": str(exc)}, 500)

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def serve_static(self, raw_path: str) -> None:
        path = unquote(raw_path)
        if path == "/":
            path = "/index.html"
        candidate = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            self.send_error(403)
            return
        if not candidate.exists() or not candidate.is_file():
            candidate = STATIC_DIR / "index.html"
        content = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        while True:
            try:
                payload = {"summary": get_summary(), "workflows": get_workflows()}
                message = f"event: snapshot\ndata: {json.dumps(payload)}\n\n"
                self.wfile.write(message.encode("utf-8"))
                self.wfile.flush()
                time.sleep(2)
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception as exc:
                print(f"[sse] {exc}")
                break


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        exc_type, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    init_database()
    stop_event = threading.Event()
    worker = threading.Thread(target=telemetry_worker, args=(stop_event,), daemon=True)
    worker.start()
    server = QuietThreadingHTTPServer((HOST, PORT), PlatformHandler)
    print(f"Autonomous Integration Platform running at http://{HOST}:{PORT}")
    print(f"SQLite database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
