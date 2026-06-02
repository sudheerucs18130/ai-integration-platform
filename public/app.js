const state = {
  summary: null,
  workflows: [],
  telemetry: [],
  incidents: [],
  predictions: [],
  actions: [],
  audit: [],
  policies: [],
  applications: [],
  selectedWorkflow: "all",
  loading: false
};

const els = {
  streamStatus: document.getElementById("streamStatus"),
  refreshBtn: document.getElementById("refreshBtn"),
  sourceStatus: document.getElementById("sourceStatus"),
  kpiGrid: document.getElementById("overview"),
  workflowFilter: document.getElementById("workflowFilter"),
  applicationForm: document.getElementById("applicationForm"),
  applicationList: document.getElementById("applicationList"),
  workflowTable: document.getElementById("workflowTable"),
  incidentList: document.getElementById("incidentList"),
  predictionTable: document.getElementById("predictionTable"),
  actionList: document.getElementById("actionList"),
  policyList: document.getElementById("policyList"),
  auditList: document.getElementById("auditList"),
  riskCanvas: document.getElementById("riskCanvas"),
  telemetryCanvas: document.getElementById("telemetryCanvas")
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, digits = 0) {
  const number = Number(value || 0);
  return number.toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits
  });
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function titleCase(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function sourceLabel(value) {
  const labels = {
    ibm_real_time: "IBM real-time",
    ibm_application: "IBM app",
    ibm_telemetry: "IBM telemetry",
    mock_fallback: "Mock fallback",
    mock_simulator: "Mock simulator",
    mock: "Mock"
  };
  return labels[value] || titleCase(value || "unknown");
}

function statusClass(value) {
  return String(value || "").toLowerCase().replaceAll(" ", "_");
}

function riskColor(score) {
  const value = Number(score || 0);
  if (value >= 85) return "#b42318";
  if (value >= 65) return "#d6453d";
  if (value >= 40) return "#b7791f";
  return "#16803c";
}

function riskMeter(score) {
  const value = Math.max(0, Math.min(100, Number(score || 0)));
  return `
    <div class="risk-meter" style="--risk-width: ${value}%; --risk-color: ${riskColor(value)}">
      <div class="risk-track"><div class="risk-fill"></div></div>
      <span>${formatNumber(value, 0)}</span>
    </div>
  `;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  if (!response.ok) {
    let message = `Request failed: ${response.status}`;
    try {
      const body = await response.json();
      message = body.error || message;
    } catch {
      // Keep the generic message when a response is not JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

async function loadAll() {
  if (state.loading) return;
  state.loading = true;
  try {
    const [
      summary,
      workflows,
      incidents,
      predictions,
      actions,
      audit,
      policies,
      applications,
      telemetry
    ] = await Promise.all([
      api("/api/summary"),
      api("/api/workflows"),
      api("/api/incidents?status=all"),
      api("/api/predictions"),
      api("/api/actions"),
      api("/api/audit"),
      api("/api/policies"),
      api("/api/applications"),
      api(`/api/telemetry?limit=120&workflow_id=${encodeURIComponent(state.selectedWorkflow)}`)
    ]);
    Object.assign(state, {
      summary,
      workflows,
      incidents,
      predictions,
      actions,
      audit,
      policies,
      applications,
      telemetry
    });
    renderAll();
    setStreamState("connected");
  } catch (error) {
    setStreamState("error", error.message);
  } finally {
    state.loading = false;
  }
}

function setStreamState(status, message) {
  els.streamStatus.className = `stream-pill ${status}`;
  if (status === "connected") {
    els.streamStatus.textContent = "Live";
    const source = state.summary?.source_status;
    if (source) {
      els.sourceStatus.textContent = source.label || sourceLabel(source.active);
      els.sourceStatus.className = `source-pill ${statusClass(source.active)}`;
      els.sourceStatus.title = source.last_error || "";
    }
  } else if (status === "error") {
    els.streamStatus.textContent = message ? "Offline" : "Disconnected";
  } else {
    els.streamStatus.textContent = "Connecting";
  }
}

function renderAll() {
  renderKpis();
  renderFilter();
  renderWorkflowTable();
  renderIncidents();
  renderPredictions();
  renderActions();
  renderPolicies();
  renderApplications();
  renderAudit();
  drawRiskCanvas();
  drawTelemetryCanvas();
}

function renderKpis() {
  const summary = state.summary || {};
  const healthy = summary.status_counts?.healthy || 0;
  const degraded = summary.status_counts?.degraded || 0;
  const failing = summary.status_counts?.failing || 0;
  const recovering = summary.status_counts?.recovering || 0;
  const source = summary.source_status || {};
  const items = [
    ["Telemetry Source", source.label || "Mock simulator", source.configured ? "IBM-first with fallback" : "Configure IBM endpoints to go live"],
    ["Workflows", summary.workflow_count || 0, `${healthy} healthy, ${degraded + failing + recovering} needs attention`],
    ["Active Incidents", summary.active_incidents || 0, `${summary.pending_approvals || 0} pending approval`],
    ["Average Risk", formatNumber(summary.avg_risk_score || 0, 1), `${summary.sla_at_risk || 0} workflows at risk`],
    ["Average Latency", `${formatNumber(summary.avg_latency_ms || 0, 0)} ms`, "Latest telemetry sample"],
    ["Recovery Success", `${formatNumber(summary.auto_heal_success_rate || 100, 1)}%`, "Completed automation actions"]
  ];

  els.kpiGrid.innerHTML = items
    .map(
      ([label, value, detail]) => `
        <article class="kpi">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
          <small>${escapeHtml(detail)}</small>
        </article>
      `
    )
    .join("");
}

function renderFilter() {
  const current = state.selectedWorkflow;
  const options = [
    `<option value="all">All workflows</option>`,
    ...state.workflows.map((workflow) => {
      const selected = String(workflow.id) === String(current) ? "selected" : "";
      return `<option value="${workflow.id}" ${selected}>${escapeHtml(workflow.name)}</option>`;
    })
  ];
  els.workflowFilter.innerHTML = options.join("");
  els.workflowFilter.value = current;
}

function renderWorkflowTable() {
  els.workflowTable.innerHTML = state.workflows
    .map((workflow) => {
      const risk = workflow.risk_score ?? workflow.anomaly_score ?? 0;
      return `
        <tr>
          <td>
            <div class="workflow-name">
              <strong>${escapeHtml(workflow.name)}</strong>
              <small>${escapeHtml(workflow.routing_group)} | ${escapeHtml(titleCase(workflow.business_criticality))}</small>
            </div>
          </td>
          <td>${escapeHtml(workflow.owner)}</td>
          <td><span class="status ${statusClass(workflow.telemetry_source || workflow.source)}">${escapeHtml(sourceLabel(workflow.telemetry_source || workflow.source))}</span></td>
          <td><span class="status ${statusClass(workflow.status)}">${escapeHtml(workflow.status)}</span></td>
          <td class="nowrap">${formatNumber(workflow.sla_target_ms, 0)} ms</td>
          <td class="nowrap">${formatNumber(workflow.latency_ms, 0)} ms</td>
          <td class="nowrap">${formatNumber(workflow.error_rate, 2)}%</td>
          <td>${riskMeter(risk)}</td>
          <td>${escapeHtml(titleCase(workflow.latest_driver || "normal"))}</td>
        </tr>
      `;
    })
    .join("");
}

function renderIncidents() {
  const active = state.incidents.filter((incident) => incident.status !== "resolved");
  if (!active.length) {
    els.incidentList.innerHTML = `<div class="empty">No active incidents</div>`;
    return;
  }

  els.incidentList.innerHTML = active
    .slice(0, 8)
    .map(
      (incident) => `
        <article class="list-row">
          <header>
            <h3>${escapeHtml(incident.title)}</h3>
            <span class="status ${statusClass(incident.severity)}">${escapeHtml(incident.severity)}</span>
          </header>
          <p>${escapeHtml(incident.summary)}</p>
          <div class="meta-row">
            <span class="pill">${escapeHtml(incident.workflow_name)}</span>
            <span class="pill">${escapeHtml(incident.routing_group)}</span>
            <span class="pill">${formatTime(incident.opened_at)}</span>
          </div>
          <div>
            <button class="button small" type="button" data-incident-action="${incident.id}">Recover</button>
          </div>
        </article>
      `
    )
    .join("");
}

function renderPredictions() {
  if (!state.predictions.length) {
    els.predictionTable.innerHTML = `<tr><td colspan="4">No predictions yet</td></tr>`;
    return;
  }
  els.predictionTable.innerHTML = state.predictions
    .slice(0, 12)
    .map(
      (prediction) => `
        <tr>
          <td>
            <div class="workflow-name">
              <strong>${escapeHtml(prediction.workflow_name)}</strong>
              <small>${formatTime(prediction.created_at)}</small>
            </div>
          </td>
          <td>
            <span class="status ${statusClass(prediction.risk_level)}">${escapeHtml(prediction.risk_level)}</span>
            ${riskMeter(prediction.risk_score)}
          </td>
          <td class="nowrap">${formatNumber(prediction.horizon_minutes, 0)} min</td>
          <td class="nowrap">${formatNumber(Number(prediction.confidence) * 100, 0)}%</td>
        </tr>
      `
    )
    .join("");
}

function renderActions() {
  if (!state.actions.length) {
    els.actionList.innerHTML = `<div class="empty">No recovery actions yet</div>`;
    return;
  }

  els.actionList.innerHTML = state.actions
    .slice(0, 10)
    .map((action) => {
      const approveButton =
        action.status === "pending_approval"
          ? `<button class="button small secondary" type="button" data-approve-action="${action.id}">Approve</button>`
          : "";
      return `
        <article class="list-row">
          <header>
            <h3>${escapeHtml(titleCase(action.action_type))}</h3>
            <span class="status ${statusClass(action.status)}">${escapeHtml(titleCase(action.status))}</span>
          </header>
          <p>${escapeHtml(action.summary)}</p>
          <div class="meta-row">
            <span class="pill">${escapeHtml(action.workflow_name)}</span>
            <span class="pill">${formatNumber(Number(action.confidence) * 100, 0)}% confidence</span>
            <span class="pill">${formatTime(action.created_at)}</span>
          </div>
          <p>${escapeHtml(action.result)}</p>
          ${approveButton}
        </article>
      `;
    })
    .join("");
}

function renderPolicies() {
  if (!state.policies.length) {
    els.policyList.innerHTML = `<div class="empty">No policies configured</div>`;
    return;
  }

  els.policyList.innerHTML = state.policies
    .map(
      (policy) => `
        <article class="list-row">
          <header>
            <h3>${escapeHtml(policy.name)}</h3>
            <span class="status ${policy.enabled ? "healthy" : "failed"}">${policy.enabled ? "Enabled" : "Disabled"}</span>
          </header>
          <div class="meta-row">
            <span class="pill">${escapeHtml(policy.workflow_name || "Global")}</span>
            <span class="pill">Risk >= ${formatNumber(policy.risk_threshold, 0)}</span>
            <span class="pill">${policy.human_approval_required ? "Approval required" : "Auto approved"}</span>
          </div>
          <p>${escapeHtml(policy.actions.map(titleCase).join(", "))}</p>
          <div>
            <button class="button small secondary" type="button" data-policy-toggle="${policy.id}">
              ${policy.enabled ? "Disable" : "Enable"}
            </button>
          </div>
        </article>
      `
    )
    .join("");
}

function renderApplications() {
  if (!state.applications.length) {
    els.applicationList.innerHTML = `<div class="empty">No real application monitors registered</div>`;
    return;
  }

  els.applicationList.innerHTML = state.applications
    .map((application) => {
      const risk = application.anomaly_score || 0;
      const incidents = Number(application.active_incidents || 0);
      return `
        <article class="list-row">
          <header>
            <h3>${escapeHtml(application.name)}</h3>
            <span class="status ${statusClass(application.telemetry_status || application.status)}">${escapeHtml(application.telemetry_status || application.status)}</span>
          </header>
          <p>${escapeHtml(application.telemetry_endpoint)}</p>
          <div class="meta-row">
            <span class="pill">${escapeHtml(sourceLabel(application.telemetry_source || application.source))}</span>
            <span class="pill">${formatNumber(application.latency_ms, 0)} ms</span>
            <span class="pill">${formatNumber(application.error_rate, 2)}% errors</span>
            <span class="pill">${incidents} active incidents</span>
          </div>
          ${riskMeter(risk)}
        </article>
      `;
    })
    .join("");
}

function renderAudit() {
  if (!state.audit.length) {
    els.auditList.innerHTML = `<div class="empty">No audit events yet</div>`;
    return;
  }

  els.auditList.innerHTML = state.audit
    .slice(0, 24)
    .map((entry) => {
      let detail = entry.details || "";
      try {
        const parsed = JSON.parse(detail);
        detail = Object.entries(parsed)
          .map(([key, value]) => `${titleCase(key)}: ${value}`)
          .join(" | ");
      } catch {
        // Keep raw detail.
      }
      return `
        <article class="audit-item">
          <span class="audit-time">${formatTime(entry.created_at)}</span>
          <div class="audit-body">
            <strong>${escapeHtml(titleCase(entry.event_type))} by ${escapeHtml(entry.actor)}</strong>
            <span>${escapeHtml(detail)}</span>
          </div>
        </article>
      `;
    })
    .join("");
}

function prepareCanvas(canvas, targetHeight) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, rect.width || canvas.clientWidth || 640);
  const height = targetHeight;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function drawRiskCanvas() {
  const { ctx, width, height } = prepareCanvas(els.riskCanvas, 240);
  const workflows = state.workflows.slice(0, 8);
  ctx.fillStyle = "#fcfdff";
  ctx.fillRect(0, 0, width, height);
  ctx.font = "700 13px Inter, sans-serif";
  ctx.fillStyle = "#17202a";
  ctx.fillText("Current failure risk by workflow", 18, 28);

  const left = 190;
  const top = 50;
  const rowGap = 26;
  const barWidth = Math.max(160, width - left - 72);

  ctx.strokeStyle = "#d9e1e7";
  ctx.lineWidth = 1;
  [40, 65, 85].forEach((threshold) => {
    const x = left + (barWidth * threshold) / 100;
    ctx.beginPath();
    ctx.moveTo(x, top - 12);
    ctx.lineTo(x, height - 22);
    ctx.stroke();
  });

  workflows.forEach((workflow, index) => {
    const y = top + index * rowGap;
    const score = Number(workflow.risk_score ?? workflow.anomaly_score ?? 0);
    const label = workflow.name.length > 23 ? `${workflow.name.slice(0, 22)}...` : workflow.name;
    ctx.fillStyle = "#62717f";
    ctx.font = "12px Inter, sans-serif";
    ctx.fillText(label, 18, y + 10);
    ctx.fillStyle = "#e6edf1";
    ctx.fillRect(left, y, barWidth, 12);
    ctx.fillStyle = riskColor(score);
    ctx.fillRect(left, y, (barWidth * score) / 100, 12);
    ctx.fillStyle = "#17202a";
    ctx.font = "700 12px Inter, sans-serif";
    ctx.fillText(formatNumber(score, 0), left + barWidth + 12, y + 10);
  });
}

function drawTelemetryCanvas() {
  const { ctx, width, height } = prepareCanvas(els.telemetryCanvas, 220);
  const points = [...state.telemetry].reverse();
  ctx.fillStyle = "#fcfdff";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#17202a";
  ctx.font = "700 13px Inter, sans-serif";

  const selected = state.selectedWorkflow !== "all";
  const title = selected ? "Latency trend against SLA" : "Anomaly risk trend";
  ctx.fillText(title, 18, 28);

  if (points.length < 2) {
    ctx.fillStyle = "#62717f";
    ctx.font = "13px Inter, sans-serif";
    ctx.fillText("Waiting for telemetry samples", 18, 72);
    return;
  }

  const left = 48;
  const right = width - 20;
  const top = 48;
  const bottom = height - 34;
  const values = points.map((point) => Number(selected ? point.latency_ms : point.anomaly_score));
  const thresholds = points.map((point) => Number(selected ? point.sla_target_ms : 65));
  const maxValue = Math.max(...values, ...thresholds, 100);
  const yFor = (value) => bottom - (value / maxValue) * (bottom - top);
  const xFor = (index) => left + (index / Math.max(1, points.length - 1)) * (right - left);

  ctx.strokeStyle = "#d9e1e7";
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const y = top + (i / 3) * (bottom - top);
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
  }

  ctx.strokeStyle = selected ? "#b7791f" : "#d6453d";
  ctx.setLineDash([6, 6]);
  ctx.beginPath();
  thresholds.forEach((threshold, index) => {
    const x = xFor(index);
    const y = yFor(threshold);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.strokeStyle = selected ? "#2563eb" : "#0f766e";
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = xFor(index);
    const y = yFor(value);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  const latest = values[values.length - 1];
  ctx.fillStyle = "#62717f";
  ctx.font = "12px Inter, sans-serif";
  ctx.fillText(`Latest: ${formatNumber(latest, selected ? 0 : 1)}${selected ? " ms" : ""}`, left, height - 12);
}

function connectEvents() {
  try {
    const events = new EventSource("/api/events");
    events.addEventListener("open", () => setStreamState("connected"));
    events.addEventListener("snapshot", (event) => {
      const payload = JSON.parse(event.data);
      state.summary = payload.summary;
      state.workflows = payload.workflows;
      renderKpis();
      renderFilter();
      renderWorkflowTable();
      drawRiskCanvas();
      setStreamState("connected");
    });
    events.addEventListener("error", () => setStreamState("error"));
  } catch {
    setStreamState("error");
  }
}

els.refreshBtn.addEventListener("click", loadAll);
els.workflowFilter.addEventListener("change", (event) => {
  state.selectedWorkflow = event.target.value;
  loadAll();
});

els.applicationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitButton = els.applicationForm.querySelector("button[type='submit']");
  submitButton.disabled = true;
  const formData = new FormData(els.applicationForm);
  const payload = {
    name: String(formData.get("name") || "").trim(),
    url: String(formData.get("url") || "").trim(),
    sla_target_ms: Number(formData.get("sla_target_ms") || 1000),
    business_criticality: String(formData.get("business_criticality") || "high")
  };
  try {
    await api("/api/applications", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    els.applicationForm.reset();
    document.getElementById("appSla").value = "1000";
    await loadAll();
  } catch (error) {
    setStreamState("error", error.message);
  } finally {
    submitButton.disabled = false;
  }
});

document.addEventListener("click", async (event) => {
  const incidentButton = event.target.closest("[data-incident-action]");
  const approveButton = event.target.closest("[data-approve-action]");
  const policyButton = event.target.closest("[data-policy-toggle]");

  try {
    if (incidentButton) {
      incidentButton.disabled = true;
      await api(`/api/incidents/${incidentButton.dataset.incidentAction}/remediate`, {
        method: "POST",
        body: JSON.stringify({ action_type: "manual_reroute" })
      });
      await loadAll();
    }
    if (approveButton) {
      approveButton.disabled = true;
      await api(`/api/actions/${approveButton.dataset.approveAction}/approve`, {
        method: "POST",
        body: JSON.stringify({})
      });
      await loadAll();
    }
    if (policyButton) {
      policyButton.disabled = true;
      await api(`/api/policies/${policyButton.dataset.policyToggle}/toggle`, {
        method: "POST",
        body: JSON.stringify({})
      });
      await loadAll();
    }
  } catch (error) {
    setStreamState("error", error.message);
  }
});

window.addEventListener("resize", () => {
  drawRiskCanvas();
  drawTelemetryCanvas();
});

connectEvents();
loadAll();
setInterval(loadAll, 5000);
