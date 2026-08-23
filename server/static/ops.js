const REFRESH_MS = 8000;
let firmwareBuilds = [];
let dashboardCache = null;
let activeTab = "overview";
let activeQueueFilter = null;
let focusDeviceId = null;
let fleetBotsCache = [];
let initialFocusTabHandled = false;

function parseFocusDeviceFromPage() {
  const fromBody = document.body?.getAttribute("data-focus-device")?.trim();
  if (fromBody) return fromBody;
  const match = window.location.pathname.match(/^\/ops\/device\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function deviceOpsUrl(deviceId) {
  const id = String(deviceId || "").trim();
  if (!id || id === "fleet" || id === "all") return "/ops";
  return `/ops/device/${encodeURIComponent(id)}`;
}

function navigateToDevice(deviceId) {
  const url = deviceOpsUrl(deviceId);
  if (window.location.pathname + window.location.search !== url) {
    window.location.href = url;
    return;
  }
  focusDeviceId = deviceId && deviceId !== "fleet" ? String(deviceId).trim() : null;
  loadDashboard().catch(console.error);
}

function esc(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function healthBadge(label, extraClass = "") {
  const value = String(label || "unknown").toLowerCase();
  return `<span class="ops-health-badge ops-health-${esc(value)} ${extraClass}">${esc(value)}</span>`;
}

function agentBadge(label) {
  const value = String(label || "idle").toLowerCase();
  return `<span class="ops-badge ops-badge-${esc(value)}">${esc(value.replaceAll("_", " "))}</span>`;
}

function issueKindBadge(kind) {
  const value = String(kind || "operational").toLowerCase();
  const labels = {
    soak_false_positive: "false alarm",
    agent_auto_fixed: "auto-fixed",
    agent_handling: "agent handling",
    developer_required: "developer",
    code_bug: "code bug",
    logic_bug: "logic bug",
    operational: "ops",
  };
  return `<span class="ops-badge ops-badge-${esc(value)}">${esc(labels[value] || value.replaceAll("_", " "))}</span>`;
}

function agentLabel(subsystem) {
  const labels = {
    llm: "LLM Agent",
    camera: "Camera Agent",
    memory: "Memory Agent",
    stt: "STT Agent",
    tts: "TTS Agent",
    voice: "Voice Agent",
    discovery: "Discovery Agent",
    bot: "Bot Agent",
  };
  return labels[String(subsystem || "").toLowerCase()] || `${subsystem || "Unknown"} Agent`;
}

function formatTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  }
  return data;
}

function switchTab(tabId) {
  if (!tabId) return;
  activeTab = tabId;
  document.querySelectorAll(".ops-tab").forEach((btn) => {
    const isActive = btn.getAttribute("data-tab") === tabId;
    btn.classList.toggle("ops-tab-active", isActive);
    btn.setAttribute("aria-selected", isActive ? "true" : "false");
    btn.tabIndex = isActive ? 0 : -1;
  });
  document.querySelectorAll(".ops-tab-panel").forEach((panel) => {
    const show = panel.id === `tab-${tabId}`;
    panel.hidden = !show;
    panel.classList.toggle("ops-tab-panel-active", show);
  });
  const panel = document.getElementById(`tab-${tabId}`);
  if (panel) {
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function highlightStatCard(filter) {
  activeQueueFilter = filter;
  document.querySelectorAll(".ops-stat-clickable").forEach((card) => {
    card.classList.toggle("ops-stat-active", card.getAttribute("data-stat-filter") === filter);
  });
  document.querySelectorAll(".ops-queue-card").forEach((card) => {
    card.classList.remove("ops-queue-highlight");
  });
  if (filter === "agent_working" || filter === "fixing") {
    document.getElementById("queueAgentWorking")?.classList.add("ops-queue-highlight");
  } else if (filter === "developer") {
    document.getElementById("queueDeveloper")?.classList.add("ops-queue-highlight");
  } else if (filter === "agent_resolved") {
    document.getElementById("queueResolved")?.classList.add("ops-queue-highlight");
  }
}

function closeStatInlineDetail() {
  activeQueueFilter = null;
  const panel = document.getElementById("statInlineDetail");
  if (panel) panel.hidden = true;
  document.querySelectorAll(".ops-stat-clickable").forEach((card) => {
    card.classList.remove("ops-stat-active");
  });
  document.querySelectorAll(".ops-queue-card").forEach((card) => {
    card.classList.remove("ops-queue-highlight");
  });
}

function renderStatInlineDetail(filter, data) {
  const panel = document.getElementById("statInlineDetail");
  const title = document.getElementById("statInlineTitle");
  const body = document.getElementById("statInlineBody");
  if (!panel || !title || !body) return;

  const queues = data.issue_queues || {};
  const bots = data.bots || [];
  const summary = data.summary || {};
  let rows = [];
  let heading = "Details";

  if (filter === "agent_working") {
    heading = "Agents working now";
    rows = queues.agent_working || [];
  } else if (filter === "fixing") {
    heading = "Fixing now";
    rows = (queues.agent_working || []).filter((row) => String(row.status) === "fixing");
  } else if (filter === "developer") {
    heading = "Needs developer";
    rows = queues.developer || [];
  } else if (filter === "agent_resolved") {
    heading = "Recently auto-fixed";
    rows = queues.agent_resolved || [];
  } else if (filter === "healthy") {
    heading = "Healthy bots";
    const healthyBots = bots.filter((b) => b.health === "healthy" && !(b.incidents || []).length);
    title.textContent = heading;
    body.innerHTML = healthyBots.length
      ? `<ul class="ops-debug-list">${healthyBots.map((b) => `<li>${esc(b.display_name || b.device_id)} — all checks passing</li>`).join("")}</ul>`
      : `<p class="ops-empty">No fully healthy bots right now. See Bot fleet tab for details.</p>`;
    panel.hidden = false;
    highlightStatCard(filter);
    return;
  } else if (filter === "overview") {
    heading = "Fleet summary";
    title.textContent = heading;
    body.innerHTML = `
      <div class="ops-runtime-grid">
        <div class="ops-agent-item"><strong>Bots online</strong><span>${esc(summary.total_bots ?? 0)}</span></div>
        <div class="ops-agent-item"><strong>Healthy</strong><span>${esc(summary.healthy_bots ?? 0)}</span></div>
        <div class="ops-agent-item"><strong>Agent working</strong><span>${esc(summary.agent_handling_incidents ?? 0)}</span></div>
        <div class="ops-agent-item"><strong>Developer needed</strong><span>${esc(summary.developer_needed_incidents ?? 0)}</span></div>
        <div class="ops-agent-item"><strong>Auto-fixed</strong><span>${esc(summary.agent_resolved_recent ?? 0)}</span></div>
        <div class="ops-agent-item"><strong>Server</strong><span>${esc(summary.server_health ?? "unknown")}</span></div>
      </div>`;
    panel.hidden = false;
    highlightStatCard(filter);
    return;
  }

  title.textContent = heading;
  const variant =
    filter === "developer" ? "developer" : filter === "agent_resolved" ? "agent_resolved" : "agent_working";
  body.innerHTML = rows.length
    ? rows.map((row) => renderQueueItem(row, variant)).join("")
    : `<p class="ops-empty">Nothing in this category right now.</p>`;
  panel.hidden = false;
  highlightStatCard(filter);
}

function bindStatCards() {
  document.querySelectorAll(".ops-stat-clickable").forEach((card) => {
    const filter = card.getAttribute("data-stat-filter");
    const activate = (event) => {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      if (!dashboardCache || !filter) return;
      if (activeQueueFilter === filter) {
        closeStatInlineDetail();
        return;
      }
      switchTab("overview");
      renderStatInlineDetail(filter, dashboardCache);
    };
    card.onclick = activate;
    card.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") {
        activate(event);
      }
    };
  });
  const closeBtn = document.getElementById("statInlineClose");
  if (closeBtn) {
    closeBtn.onclick = (event) => {
      event.preventDefault();
      closeStatInlineDetail();
    };
  }
}

function layerBadge(layer) {
  const value = String(layer || "ops").toLowerCase();
  return `<span class="ops-task-layer ops-task-layer-${esc(value)}">${esc(value.replaceAll("_", " "))}</span>`;
}

function renderProjectTasks(tasks) {
  const panel = document.getElementById("projectTasksPanel");
  if (!panel) return;
  const rows = tasks || [];
  if (!rows.length) {
    panel.innerHTML = `<p class="ops-empty">No task roster available.</p>`;
    return;
  }
  panel.innerHTML = `
    <div class="ops-task-grid">
      ${rows
        .map(
          (task) => `
            <article class="ops-task-card">
              <header class="ops-task-header">
                <h3>${esc(task.name)}</h3>
                ${layerBadge(task.layer)}
              </header>
              <div class="ops-task-interval">Every ${esc(task.interval || "—")}</div>
              <p class="ops-task-role">${esc(task.role)}</p>
              <p class="ops-task-handles"><strong>Handles:</strong> ${esc(task.handles)}</p>
            </article>
          `
        )
        .join("")}
    </div>
  `;
}

function renderDeviceNav(data) {
  const strip = document.getElementById("deviceNavStrip");
  const hint = document.getElementById("deviceNavHint");
  if (!strip) return;

  const server = data.server || {};
  const bots = fleetBotsCache.length ? fleetBotsCache : data.bots || [];
  const focus = data.focus || {};
  const currentFocus = focus.device_id || focusDeviceId || null;

  if (hint) {
    hint.textContent = currentFocus
      ? `${focus.display_name || currentFocus}`
      : `${bots.length} bot(s) + server`;
  }

  const chips = [
    {
      device_id: "fleet",
      display_name: "All fleet",
      health: data.summary?.server_health || "unknown",
      isFleet: true,
    },
    {
      device_id: "server",
      display_name: server.display_name || "NiNO Server",
      health: server.health || "unknown",
      agent_status: server.agent_status,
    },
    ...bots.map((bot) => ({
      device_id: bot.device_id,
      display_name: bot.display_name || bot.device_id,
      subtitle:
        bot.location_name && bot.location_name !== bot.display_name
          ? bot.location_name
          : bot.device_id !== bot.display_name
            ? bot.device_id
            : "",
      health: bot.health,
      agent_status: bot.agent_status,
      incident_count: (bot.incidents || []).length,
    })),
  ];

  strip.innerHTML = chips
    .map((chip) => {
      const active =
        chip.isFleet && !currentFocus
          ? true
          : !chip.isFleet && String(chip.device_id) === String(currentFocus);
      const issueDot =
        chip.incident_count > 0
          ? `<span class="ops-device-issue-dot" title="${esc(chip.incident_count)} open issue(s)"></span>`
          : "";
      return `
        <button
          type="button"
          class="ops-device-chip ops-device-chip-${esc(String(chip.health || "unknown").toLowerCase())}${active ? " ops-device-chip-active" : ""}"
          data-device-id="${esc(chip.device_id)}"
          aria-current="${active ? "true" : "false"}"
        >
          ${issueDot}
          <span class="ops-device-chip-text">
            <span class="ops-device-chip-name">${esc(chip.display_name)}</span>
            ${chip.subtitle ? `<span class="ops-device-chip-sub">${esc(chip.subtitle)}</span>` : ""}
          </span>
          ${healthBadge(chip.health, "ops-badge-sm")}
        </button>
      `;
    })
    .join("");

  strip.querySelectorAll(".ops-device-chip").forEach((btn) => {
    btn.onclick = (event) => {
      event.preventDefault();
      const deviceId = btn.getAttribute("data-device-id");
      if (deviceId === "fleet") {
        window.location.href = "/ops";
      } else {
        window.location.href = deviceOpsUrl(deviceId);
      }
    };
  });
}

function renderFocusBanner(data) {
  const banner = document.getElementById("focusBanner");
  const title = document.getElementById("focusBannerTitle");
  const meta = document.getElementById("focusBannerMeta");
  if (!banner || !title || !meta) return;

  const focus = data.focus || {};
  if (!focus.device_id || focus.mode === "fleet") {
    banner.hidden = true;
    return;
  }

  if (focus.mode === "missing") {
    banner.hidden = false;
    title.textContent = focus.display_name || focus.device_id;
    meta.textContent = " — device not found on network";
    return;
  }

  banner.hidden = false;
  title.textContent = focus.display_name || focus.device_id;
  const health = focus.health || "unknown";
  meta.innerHTML = ` · ${healthBadge(health)} · ${esc(focus.device_id)}`;
}

function renderBotFocusPanel(data) {
  const panel = document.getElementById("botFocusPanel");
  const title = document.getElementById("botFocusTitle");
  const healthBadgeEl = document.getElementById("botFocusHealth");
  const body = document.getElementById("botFocusBody");
  if (!panel || !body) return;

  const focus = data.focus || {};
  if (!focus.device_id || focus.mode === "fleet") {
    panel.hidden = true;
    return;
  }

  panel.hidden = false;

  if (focus.mode === "missing") {
    if (title) title.textContent = "Device not found";
    if (healthBadgeEl) healthBadgeEl.textContent = "unknown";
    body.innerHTML = `<p class="ops-empty">No bot with ID <code>${esc(focus.device_id)}</code> is currently discovered. Check LAN discovery or devices.json.</p>`;
    return;
  }

  if (focus.mode === "server") {
    if (title) title.textContent = "NiNO Server ops";
    const server = data.server || {};
    if (healthBadgeEl) {
      const health = String(server.health || "unknown").toLowerCase();
      healthBadgeEl.className = `ops-health-badge ops-health-${health}`;
      healthBadgeEl.textContent = health;
    }
    body.innerHTML = renderServerDetails(server, { expanded: true });
    return;
  }

  const bot = (data.bots || [])[0];
  if (!bot) {
    panel.hidden = true;
    return;
  }

  if (title) title.textContent = `${bot.display_name || bot.device_id} ops`;
  if (healthBadgeEl) {
    const health = String(bot.health || "unknown").toLowerCase();
    healthBadgeEl.className = `ops-health-badge ops-health-${health}`;
    healthBadgeEl.textContent = health;
  }
  body.innerHTML = renderBotDetails(bot, { expanded: true });
}

function renderServerDetails(server, { expanded = false } = {}) {
  const llm = server.llm || {};
  const memory = server.memory || {};
  const incidents = server.incidents || [];
  return `
    <div class="ops-bot-header">
      <div>
        <h3>${esc(server.display_name || "NiNO Server")}</h3>
        <div class="ops-bot-meta">Agent: ${agentBadge(server.agent_status)}</div>
      </div>
    </div>
    ${renderSubsystems(server.subsystems)}
    <div class="ops-bot-meta">
      LLM: ${esc(llm.reachable ? "reachable" : "down")}
      ${llm.model ? ` · ${esc(llm.model)}` : ""}
      ${memory.database_url_set ? ` · Memory: ${memory.ready ? "ready" : "not ready"}` : ""}
    </div>
    ${
      incidents.length
        ? `<div class="ops-incident-list" style="margin-top: 12px;">${incidents
            .map(
              (inc) => `
              <article class="ops-incident ops-incident-agent_working">
                <div class="ops-incident-title">${esc(inc.subsystem)} · ${esc(inc.status)}</div>
                <div class="ops-incident-error">${esc(inc.error)}</div>
              </article>`
            )
            .join("")}</div>`
        : `<div class="ops-incident-meta">All server checks passing</div>`
    }
  `;
}

function renderBotDetails(bot, { expanded = false } = {}) {
  const incidents = bot.incidents || [];
  const openCount = incidents.length;
  return `
    <div class="ops-bot-header">
      <div>
        <h3>${esc(bot.display_name || bot.device_id)}</h3>
        <div class="ops-bot-meta">${esc(bot.device_id)} · Agent ${agentBadge(bot.agent_status)}</div>
      </div>
    </div>
    <div class="ops-bot-meta">
      Camera: <span class="ops-camera-state ops-camera-${esc(String(bot.camera_state || "unknown").toLowerCase())}">${esc(cameraStateLabel(bot))}</span>
      ${bot.voice_pipeline_active ? " · voice active" : ""}
      ${bot.wifi_ssid ? `<div>Wi‑Fi: ${esc(bot.wifi_ssid)}${bot.wifi_rssi != null ? ` (${esc(bot.wifi_rssi)} dBm)` : ""}</div>` : ""}
      ${bot.base_url ? `<div>Base: <code>${esc(bot.base_url)}</code></div>` : ""}
      ${bot.camera_url ? `<div>Camera: <code>${esc(bot.camera_url)}</code></div>` : ""}
    </div>
    ${renderSubsystems(bot.subsystems)}
    ${
      openCount
        ? `<div class="ops-incident-list" style="margin-top: 12px;">${incidents
            .map(
              (inc) => `
              <article class="ops-incident ops-incident-agent_working">
                <div class="ops-incident-title">${esc(inc.subsystem)} · ${esc(inc.status)}</div>
                <div class="ops-incident-error">${esc(inc.error)}</div>
              </article>`
            )
            .join("")}</div>`
        : `<div class="ops-incident-meta">All checks passing</div>`
    }
    ${bot.camera_last_error ? `<div class="ops-incident-error">${esc(bot.camera_last_error)}</div>` : ""}
    ${
      bot.base_url && firmwareBuilds.length
        ? `<div class="ops-ota-row">
            <select class="ops-ota-select" data-device-id="${esc(bot.device_id)}">
              ${firmwareBuilds
                .map(
                  (b) =>
                    `<option value="${esc(b.filename)}">${esc(b.filename)} (${Math.round((b.size_bytes || 0) / 1024)} KB)</option>`
                )
                .join("")}
            </select>
            <button class="secondary ops-btn-inline ops-ota-deploy" data-device-id="${esc(bot.device_id)}" type="button">Update firmware</button>
          </div>`
        : ""
    }
  `;
}

function renderSummary(summary) {
  document.getElementById("statTotalBots").textContent = summary.total_bots ?? 0;
  document.getElementById("statHealthy").textContent = summary.healthy_bots ?? 0;
  document.getElementById("statAgentWorking").textContent = summary.agent_handling_incidents ?? 0;
  document.getElementById("statDeveloper").textContent = summary.developer_needed_incidents ?? 0;
  document.getElementById("statAutoFixed").textContent = summary.agent_resolved_recent ?? 0;
  document.getElementById("statFixing").textContent = summary.fixing_incidents ?? 0;
}

function renderPlainEnglish(ui) {
  if (!ui || !ui.plain_english) return "";
  const auto = ui.auto_resolves_on_tick
    ? `<div class="ops-incident-auto">Auto-resolves on next check — or click “Run check now”.</div>`
    : "";
  return `
    <div class="ops-plain-english">
      <strong>What this means</strong>
      <p>${esc(ui.plain_english)}</p>
      ${auto}
    </div>
  `;
}

function renderSoakReply(ui) {
  if (!ui || !ui.soak_reply_text) return "";
  return `
    <div class="ops-soak-reply">
      <strong>Bot reply</strong>
      <blockquote>${esc(ui.soak_reply_text)}</blockquote>
      ${ui.soak_reply_path ? `<span class="ops-incident-meta">Route: ${esc(ui.soak_reply_path)}</span>` : ""}
    </div>
  `;
}

function renderQueueItem(row, variant) {
  const ui = row.ui || {};
  const status = String(row.status || "").toLowerCase();
  const actionLine = row.current_action_label
    ? `<div class="ops-queue-action"><span class="ops-agent-pulse"></span> ${esc(row.agent)} — ${esc(row.current_action_label)}</div>`
    : status === "fixing"
      ? `<div class="ops-queue-action"><span class="ops-agent-pulse"></span> ${esc(row.agent)} — fixing now</div>`
      : `<div class="ops-queue-action">${esc(row.agent)} — queued</div>`;

  return `
    <details class="ops-queue-item ops-queue-item-${esc(variant)}" ${variant === "agent_working" && status === "fixing" ? "open" : ""}>
      <summary class="ops-queue-item-summary">
        <div class="ops-queue-item-title">
          <strong>${esc(row.display_name || row.device_id)}</strong>
          <span class="ops-queue-item-sub">${esc(row.subsystem)} · ${esc(row.agent)}</span>
        </div>
        <div class="ops-queue-item-badges">
          ${issueKindBadge(row.issue_kind || ui.issue_kind)}
          ${agentBadge(row.status)}
        </div>
      </summary>
      <div class="ops-queue-item-body">
        ${renderPlainEnglish(ui.plain_english ? ui : { plain_english: row.plain_english, auto_resolves_on_tick: ui.auto_resolves_on_tick })}
        ${renderSoakReply(ui)}
        ${variant !== "agent_resolved" ? actionLine : ""}
        <div class="ops-incident-error">${esc(row.error)}</div>
        ${
          row.last_fix_success != null && row.current_action
            ? `<div class="ops-incident-fix">Last: ${esc(row.current_action_label || row.current_action)} — ${
                row.last_fix_success ? "succeeded" : "failed"
              }</div>`
            : ""
        }
        <div class="ops-incident-meta">
          Detected ${esc(formatTime(row.detected_at))}
          ${row.resolved_at ? ` · Resolved ${esc(formatTime(row.resolved_at))}` : ""}
          · attempts ${esc(row.fix_attempts ?? 0)}
        </div>
      </div>
    </details>
  `;
}

function renderIssueQueues(queues) {
  const agent = queues?.agent_working || [];
  const developer = queues?.developer || [];
  const resolved = queues?.agent_resolved || [];

  document.getElementById("queueAgentCount").textContent = agent.length;
  document.getElementById("queueDeveloperCount").textContent = developer.length;
  document.getElementById("queueResolvedCount").textContent = resolved.length;

  document.getElementById("queueAgentBody").innerHTML = agent.length
    ? agent.map((row) => renderQueueItem(row, "agent_working")).join("")
    : `<p class="ops-empty">No active agent work — fleet looks healthy.</p>`;

  document.getElementById("queueDeveloperBody").innerHTML = developer.length
    ? developer.map((row) => renderQueueItem(row, "developer")).join("")
    : `<p class="ops-empty">No software bugs open. Intelligent Mode handles routine issues.</p>`;

  document.getElementById("queueResolvedBody").innerHTML = resolved.length
    ? resolved.map((row) => renderQueueItem(row, "agent_resolved")).join("")
    : `<p class="ops-empty">No recent auto-fixes yet.</p>`;

  if (activeQueueFilter) {
    highlightStatCard(activeQueueFilter);
  }
}

function renderAgentStatus(data) {
  const im = data.intelligent_mode || {};
  const enabled = im.enabled ? "Enabled" : "Disabled";
  const running = im.running ? "Running" : "Stopped";
  const email = im.email_configured ? "Configured" : "Not configured";
  const lastTick = data.last_tick || {};
  const smoke = lastTick.smoke_tests || {};
  const e2e = lastTick.e2e_tests || {};
  const soak = data.soak_test || im.soak_test || {};
  const runtime = data.server_runtime || im.server_runtime || {};
  const soakRunning = soak.running || soak.runner_alive;
  const lastCycle = soak.last_cycle || {};
  const liveEsp = soak.live_esp !== false;
  const soakDevice = soak.live_esp_device_name || soak.live_esp_device_id || "none";
  const activity = data.agent_activity || [];

  const imBadge = document.getElementById("imRunningBadge");
  if (imBadge) {
    imBadge.className = `ops-health-badge ops-health-${im.enabled && im.running ? "healthy" : "degraded"}`;
    imBadge.textContent = im.enabled && im.running ? "running" : "stopped";
  }

  const workingAgents = activity.filter((row) => String(row.status) === "fixing");
  const activityStrip =
    workingAgents.length > 0
      ? `<div class="ops-live-agents">
          <span class="ops-live-label">Live now</span>
          ${workingAgents
            .map(
              (row) =>
                `<span class="ops-live-chip"><span class="ops-agent-pulse"></span> ${esc(row.agent)} on ${esc(row.display_name || row.device_id)}${row.current_action_label ? ` — ${esc(row.current_action_label)}` : ""}</span>`
            )
            .join("")}
        </div>`
      : `<div class="ops-live-agents ops-live-idle"><span class="ops-live-label">Live now</span> No agents actively fixing — monitoring.</div>`;

  document.getElementById("agentStatus").innerHTML = `
    ${activityStrip}
    <div class="ops-agent-grid-inner">
      <div class="ops-agent-item">
        <strong>Mode</strong>
        <span>${esc(enabled)} · ${esc(running)}</span>
      </div>
      <div class="ops-agent-item">
        <strong>Server</strong>
        <span>PID ${esc(runtime.pid ?? "—")} · ${runtime.running ? "running" : "stopped"}</span>
        <span>Soak: ${soakRunning ? "running" : "stopped"} · cycles ${esc(soak.cycles_completed ?? 0)}</span>
      </div>
      <div class="ops-agent-item">
        <strong>Poll cycle</strong>
        <span>Every ${esc(im.poll_seconds ?? "—")}s · grace ${esc(im.grace_seconds ?? "—")}s</span>
        <span>Last tick: ${esc(formatTime(data.last_tick_at))}</span>
      </div>
      <div class="ops-agent-item">
        <strong>Last check</strong>
        <span>Smoke: ${esc(smoke.passed ?? "—")}/${esc(smoke.total ?? "—")}</span>
        <span>E2E: ${esc(e2e.passed ?? "—")}/${esc(e2e.total ?? "—")}</span>
        ${
          lastCycle.cycle_number
            ? `<span>Soak cycle ${esc(lastCycle.cycle_number)}: ${esc(lastCycle.passed)}/${esc(lastCycle.total)}</span>`
            : ""
        }
      </div>
      <div class="ops-agent-item">
        <strong>Voice soak</strong>
        <span>${liveEsp ? `Live ESP (${esc(soakDevice)})` : "Mock TTS"}</span>
      </div>
      <div class="ops-agent-item">
        <strong>Alerts</strong>
        <span>Email ${esc(email)} · ${esc(im.email_mode || "digest")}</span>
        <span>Pending: ${esc(im.email_pending ?? 0)}</span>
      </div>
    </div>
  `;
}

function renderSubsystems(subsystems) {
  const entries = Object.entries(subsystems || {});
  if (!entries.length) {
    return `<p class="ops-empty">No subsystem data.</p>`;
  }
  return `
    <div class="ops-subsystem-grid">
      ${entries
        .map(
          ([name, health]) => `
            <div class="ops-subsystem">
              <span>${esc(name)}</span>
              ${healthBadge(health, "ops-badge")}
            </div>
          `
        )
        .join("")}
    </div>
  `;
}

function renderServer(server) {
  const llm = server.llm || {};
  const memory = server.memory || {};
  const badge = document.getElementById("serverHealthBadge");
  if (badge) {
    const health = String(server.health || "unknown").toLowerCase();
    badge.className = `ops-health-badge ops-health-${health}`;
    badge.textContent = health;
  }
  document.getElementById("serverPanel").innerHTML = `
    <div class="ops-bot-header">
      <div>
        <h3>${esc(server.display_name || "NiNO Server")}</h3>
        <div class="ops-bot-meta">Agent: ${agentBadge(server.agent_status)}</div>
      </div>
    </div>
    ${renderSubsystems(server.subsystems)}
    <div class="ops-bot-meta">
      LLM: ${esc(llm.reachable ? "reachable" : "down")}
      ${llm.model ? ` · ${esc(llm.model)}` : ""}
      ${memory.database_url_set ? ` · Memory: ${memory.ready ? "ready" : "not ready"}` : ""}
    </div>
  `;
}

function cameraStateLabel(bot) {
  const state = String(bot.camera_state || "").toLowerCase();
  if (state === "live") return "live";
  if (state === "idle") return "idle";
  if (state === "in_session") return "starting";
  if (state === "fault") return "fault";
  return bot.camera_connected ? "live" : "down";
}

function renderBotCard(bot) {
  const incidents = bot.incidents || [];
  const openCount = incidents.length;
  const agentStatus = bot.agent_status || "idle";

  return `
    <details class="ops-bot-card ops-bot-card-compact">
      <summary class="ops-bot-summary">
        <div class="ops-bot-summary-main">
          <h3><a class="ops-bot-link" href="${esc(deviceOpsUrl(bot.device_id))}" onclick="event.stopPropagation()">${esc(bot.display_name || bot.device_id)}</a></h3>
          <div class="ops-bot-meta">${esc(bot.device_id)} · <a class="ops-bot-link-sub" href="${esc(deviceOpsUrl(bot.device_id))}" onclick="event.stopPropagation()">Open bot ops →</a></div>
        </div>
        <div class="ops-bot-summary-badges">
          ${healthBadge(bot.health)}
          ${agentBadge(agentStatus)}
          ${openCount ? `<span class="ops-bot-issue-count">${openCount} issue${openCount > 1 ? "s" : ""}</span>` : ""}
        </div>
      </summary>
      <div class="ops-bot-details">
        <div class="ops-bot-meta">
          Camera: <span class="ops-camera-state ops-camera-${esc(String(bot.camera_state || "unknown").toLowerCase())}">${esc(cameraStateLabel(bot))}</span>
          ${bot.voice_pipeline_active ? " · voice active" : ""}
          ${bot.wifi_ssid ? `<div>Wi‑Fi: ${esc(bot.wifi_ssid)}</div>` : ""}
          ${bot.base_url ? `<div>Base: <code>${esc(bot.base_url)}</code></div>` : ""}
        </div>
        ${renderSubsystems(bot.subsystems)}
        ${
          openCount
            ? `<div class="ops-incident-meta">${esc(incidents.map((i) => `${i.subsystem} (${i.status})`).join(", "))}</div>`
            : `<div class="ops-incident-meta">All checks passing</div>`
        }
        ${bot.camera_last_error ? `<div class="ops-incident-error">${esc(bot.camera_last_error)}</div>` : ""}
        ${
          bot.base_url && firmwareBuilds.length
            ? `<div class="ops-ota-row">
                <select class="ops-ota-select" data-device-id="${esc(bot.device_id)}">
                  ${firmwareBuilds
                    .map(
                      (b) =>
                        `<option value="${esc(b.filename)}">${esc(b.filename)} (${Math.round((b.size_bytes || 0) / 1024)} KB)</option>`
                    )
                    .join("")}
                </select>
                <button class="secondary ops-btn-inline ops-ota-deploy" data-device-id="${esc(bot.device_id)}" type="button">Update firmware</button>
              </div>`
            : ""
        }
      </div>
    </details>
  `;
}

function renderBots(bots) {
  document.getElementById("botFleetCount").textContent = `${bots.length} bot(s)`;
  if (!bots.length) {
    document.getElementById("botFleet").innerHTML =
      `<p class="ops-empty">No bots discovered yet. Check LAN discovery or devices.json.</p>`;
    return;
  }
  document.getElementById("botFleet").innerHTML = bots.map(renderBotCard).join("");
}

function renderDebugReport(debug, ui = {}) {
  if (!debug || typeof debug !== "object") return "";
  const issueKind = ui.issue_kind || "";
  const hideCodeBug = issueKind === "soak_false_positive" || issueKind === "agent_auto_fixed";
  const actions = (debug.suggested_actions || []).map((action) => `<li>${esc(action)}</li>`).join("");
  const evidence = (debug.evidence || []).slice(0, 4).map((item) => `<li>${esc(item)}</li>`).join("");
  const code = debug.code_bug && typeof debug.code_bug === "object" ? debug.code_bug : null;
  const codeBlock =
    !hideCodeBug && code && code.is_code_bug
      ? `
      <div class="ops-debug-box ops-debug-codebug">
        <div class="ops-incident-meta"><strong>Code bug</strong> · confidence ${esc(code.confidence || "low")}</div>
        <div class="ops-incident-error">${esc(code.bug_summary || "")}</div>
        <div class="ops-incident-fix">${esc(code.suggested_fix || "")}</div>
        ${
          (code.affected_files || []).length
            ? `<div class="ops-incident-meta">Files: ${esc((code.affected_files || []).join(", "))}</div>`
            : ""
        }
      </div>`
      : "";
  if (hideCodeBug && !debug.llm_analysis && !actions && !evidence) return "";
  return `
    <div class="ops-debug-box">
      <div class="ops-incident-meta">
        <strong>Self-debug:</strong> ${esc(debug.category || "unknown")} · confidence ${esc(debug.confidence || "low")}
      </div>
      ${hideCodeBug ? "" : `<div class="ops-incident-error">${esc(debug.root_cause || "")}</div>`}
      ${debug.llm_analysis ? `<div class="ops-incident-fix">LLM: ${esc(debug.llm_analysis)}</div>` : ""}
      ${actions ? `<ul class="ops-debug-list">${actions}</ul>` : ""}
      ${evidence ? `<ul class="ops-debug-evidence">${evidence}</ul>` : ""}
    </div>
    ${codeBlock}
  `;
}

function renderIncidents(incidents) {
  const active = incidents.active || [];
  document.getElementById("incidentCount").textContent = `${active.length} active`;
  if (!active.length) {
    document.getElementById("incidentPanel").innerHTML =
      `<p class="ops-empty">No active incidents. Fleet looks healthy.</p>`;
    return;
  }

  document.getElementById("incidentPanel").innerHTML = `
    <div class="ops-incident-list">
      ${active
        .map((inc) => {
          const ui = inc.ui || {};
          const queue = ui.queue || "agent_working";
          return `
            <details class="ops-incident ops-incident-${esc(queue)}">
              <summary class="ops-incident-top">
                <div class="ops-incident-title">${esc(inc.display_name || inc.device_id)} · ${esc(inc.subsystem)} · ${esc(agentLabel(inc.subsystem))}</div>
                <div>
                  ${issueKindBadge(ui.issue_kind)}
                  ${healthBadge(inc.severity)}
                  ${agentBadge(inc.status)}
                </div>
              </summary>
              <div class="ops-incident-body">
                <div class="ops-incident-meta">Detected ${esc(formatTime(inc.detected_at))} · attempts ${esc(inc.fix_attempts ?? 0)}</div>
                ${renderPlainEnglish(ui)}
                ${renderSoakReply(ui)}
                <div class="ops-incident-error">${esc(inc.error)}</div>
                ${renderDebugReport(inc.debug_report, ui)}
              </div>
            </details>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderSmoke(lastRun) {
  if (!lastRun) {
    document.getElementById("smokeRunSummary").textContent = "No runs yet";
    document.getElementById("smokePanel").innerHTML =
      `<p class="ops-empty">Smoke tests have not run yet. Enable Intelligent Mode or click “Run smoke tests”.</p>`;
    return;
  }

  document.getElementById("smokeRunSummary").textContent = `${lastRun.passed}/${lastRun.total} passed`;
  const rows = lastRun.results || [];
  document.getElementById("smokePanel").innerHTML = `
    <div class="ops-bot-meta" style="margin-bottom: 10px;">
      Run ${esc(lastRun.run_id)} · ${esc(formatTime(lastRun.finished_at || lastRun.started_at))}
    </div>
    <div class="ops-smoke-table-wrap">
      <table class="ops-smoke-table">
        <thead>
          <tr><th>Test</th><th>Device</th><th>Subsystem</th><th>Result</th><th>Message</th></tr>
        </thead>
        <tbody>
          ${rows
            .map(
              (row) => `
                <tr>
                  <td>${esc(row.name || row.test_id)}</td>
                  <td>${esc(row.device_id)}</td>
                  <td>${esc(row.subsystem)}</td>
                  <td>${row.passed ? healthBadge("healthy") : healthBadge("critical")}</td>
                  <td>${esc(row.message)}</td>
                </tr>
              `
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderCodingAgentProposals(proposals) {
  const pending = (proposals || []).filter((p) => p.status === "pending");
  const container = document.getElementById("codingAgentPanel");
  if (!container) return;
  if (!pending.length) {
    container.innerHTML = `<p class="ops-empty">No pending coding-agent fix proposals.</p>`;
    return;
  }
  container.innerHTML = `
    <div class="ops-incident-list">
      ${pending
        .map(
          (p) => `
            <details class="ops-incident ops-incident-developer" open>
              <summary class="ops-incident-top">
                <div class="ops-incident-title">${esc(p.display_name || p.device_id)} · ${esc(p.fix_type)} fix</div>
                <div>${healthBadge("warning")}</div>
              </summary>
              <div class="ops-incident-body">
                <div class="ops-incident-error">${esc(p.bug_summary || "")}</div>
                <div class="ops-incident-fix">${esc(p.root_cause || "")}</div>
                <div class="ops-incident-meta">Model: ${esc(p.model_used || "—")} · ${esc((p.changes || []).length)} change(s) · ${esc(p.confidence || "—")} confidence</div>
                <div class="ops-incident-meta">${p.validation_passed ? "✓ Validated" : "⚠ Needs review"} — ${esc(p.validation_detail || "")}</div>
                ${
                  (p.test_results || []).length
                    ? `<div class="ops-incident-meta">Tests: ${esc((p.test_results || []).join(" · "))}</div>`
                    : ""
                }
                ${
                  (p.changes || []).length
                    ? (p.changes || [])
                        .map(
                          (c) =>
                            `<pre class="ops-code-diff"><strong>${esc(c.file_path)}</strong>\n${esc(c.explanation || "")}\n--- BEFORE ---\n${esc(c.old_code || "")}\n--- AFTER ---\n${esc(c.new_code || "")}</pre>`
                        )
                        .join("")
                    : ""
                }
                <div class="ops-incident-actions">
                  <button class="ops-btn ops-btn-approve" data-proposal-id="${esc(p.proposal_id)}" type="button">Approve fix</button>
                  <button class="ops-btn ops-btn-reject" data-proposal-id="${esc(p.proposal_id)}" type="button">Reject</button>
                </div>
              </div>
            </details>
          `
        )
        .join("")}
    </div>
  `;
  container.querySelectorAll(".ops-btn-approve").forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.getAttribute("data-proposal-id");
      btn.disabled = true;
      try {
        await api(`/api/coding-agent/approve/${id}`, { method: "POST" });
        await loadExtraPanels();
      } catch (e) {
        btn.disabled = false;
        alert(String(e));
      }
    };
  });
  container.querySelectorAll(".ops-btn-reject").forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.getAttribute("data-proposal-id");
      btn.disabled = true;
      try {
        await api(`/api/coding-agent/reject/${id}`, { method: "POST" });
        await loadExtraPanels();
      } catch (e) {
        btn.disabled = false;
        alert(String(e));
      }
    };
  });
}

function renderDeveloperIssues(issues) {
  document.getElementById("developerIssueCount").textContent = `${issues.length} issue(s)`;
  if (!issues.length) {
    document.getElementById("developerPanel").innerHTML =
      `<p class="ops-empty">No developer issues open. WAV limits, STT retries, and soak false alarms are handled automatically.</p>`;
    return;
  }
  document.getElementById("developerPanel").innerHTML = `
    <div class="ops-incident-list">
      ${issues
        .map(
          (issue) => `
            <details class="ops-incident ops-incident-developer" open>
              <summary class="ops-incident-top">
                <div class="ops-incident-title">${esc(issue.display_name || issue.device_id)} · ${esc(issue.subsystem)}</div>
                <div>${issueKindBadge(issue.issue_kind || "developer_required")} ${healthBadge("critical")}</div>
              </summary>
              <div class="ops-incident-body">
                ${issue.plain_english ? `<div class="ops-plain-english"><p>${esc(issue.plain_english)}</p></div>` : ""}
                <div class="ops-incident-error">${esc(issue.error)}</div>
                <div class="ops-incident-fix">${esc(issue.root_cause || "")}</div>
                ${
                  (issue.affected_files || []).length
                    ? `<div class="ops-incident-meta">Files: ${esc((issue.affected_files || []).join(", "))}</div>`
                    : ""
                }
                ${
                  (issue.suggested_actions || []).length
                    ? `<ul class="ops-debug-list">${issue.suggested_actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>`
                    : ""
                }
              </div>
            </details>
          `
        )
        .join("")}
    </div>
  `;
}

function renderSoakPanel(soak) {
  const panel = document.getElementById("soakPanel");
  const summary = document.getElementById("soakRunSummary");
  if (!panel || !summary) return;

  const lastCycle = soak.last_cycle || {};
  const running = soak.running || soak.runner_alive;
  if (!lastCycle.cycle_number && !running) {
    summary.textContent = "Not running";
    panel.innerHTML = `<p class="ops-empty">Soak tests exercise live voice Q&amp;A on the robot every ~90s.</p>`;
    return;
  }

  const ok = lastCycle.ok !== false;
  summary.textContent = lastCycle.total
    ? `${lastCycle.passed}/${lastCycle.total} passed · cycle ${lastCycle.cycle_number}`
    : running
      ? "Running…"
      : "—";

  const scenarios = (lastCycle.scenarios || []).filter((row) => String(row.test_id || "").includes("soak:voice"));
  const failed = scenarios.filter((row) => row.passed === false);

  panel.innerHTML = `
    <div class="ops-bot-meta" style="margin-bottom: 10px;">
      Status: ${running ? "running" : "stopped"} · ${esc(soak.cycles_completed ?? 0)} cycles
      · ${soak.live_esp !== false ? "live ESP" : "mock TTS"}
    </div>
    <div class="ops-runtime-grid">
      <div class="ops-agent-item"><strong>Last cycle</strong><span>${ok ? "PASS" : "FAIL"} · ${esc(formatTime(lastCycle.finished_at))}</span></div>
      <div class="ops-agent-item"><strong>Voice tests</strong><span>${esc(scenarios.length)}</span></div>
      <div class="ops-agent-item"><strong>Failures</strong><span>${esc(failed.length)}</span></div>
    </div>
    ${
      failed.length
        ? `<div class="ops-smoke-table-wrap" style="margin-top: 12px;">
            <table class="ops-smoke-table">
              <thead><tr><th>Test</th><th>Result</th><th>Message</th></tr></thead>
              <tbody>
                ${failed
                  .map(
                    (row) => `
                      <tr>
                        <td>${esc(row.name || row.test_id)}</td>
                        <td>${healthBadge("critical")}</td>
                        <td>${esc(row.message)}</td>
                      </tr>`
                  )
                  .join("")}
              </tbody>
            </table>
          </div>`
        : `<p class="ops-empty" style="margin-top: 12px;">All voice scenarios passed in the last soak cycle.</p>`
    }
  `;
}

function renderFirmware(builds, pending) {
  firmwareBuilds = builds || [];
  document.getElementById("firmwareBuildCount").textContent = `${firmwareBuilds.length} build(s)`;
  if (!firmwareBuilds.length) {
    document.getElementById("firmwarePanel").innerHTML =
      `<p class="ops-empty">No firmware builds uploaded. Build with idf.py, then upload the .bin here.</p>`;
  } else {
    document.getElementById("firmwarePanel").innerHTML = `
      <ul class="ops-debug-list">
        ${firmwareBuilds
          .map(
            (b) =>
              `<li><code>${esc(b.filename)}</code> — ${Math.round((b.size_bytes || 0) / 1024)} KB · ${esc(formatTime(b.modified_at))}</li>`
          )
          .join("")}
      </ul>
    `;
  }
  const pendingRows = pending || [];
  document.getElementById("otaPendingPanel").innerHTML = pendingRows.length
    ? `<div class="ops-incident-list">${pendingRows
        .map(
          (p) => `
          <article class="ops-incident">
            <div class="ops-incident-title">${esc(p.device_id)} → ${esc(p.filename)}</div>
            <button class="secondary ops-btn-inline ops-ota-approve" data-approval-id="${esc(p.approval_id)}" type="button">Approve &amp; deploy</button>
          </article>`
        )
        .join("")}</div>`
    : "";
}

function bindTabs() {
  const nav = document.getElementById("opsTabs");
  if (!nav) return;
  nav.addEventListener("click", (event) => {
    const btn = event.target.closest(".ops-tab");
    if (!btn || !nav.contains(btn)) return;
    event.preventDefault();
    const tabId = btn.getAttribute("data-tab");
    if (!tabId) return;
    closeStatInlineDetail();
    switchTab(tabId);
  });
  nav.addEventListener("keydown", (event) => {
    const tabs = Array.from(nav.querySelectorAll(".ops-tab"));
    const currentIndex = tabs.findIndex((btn) => btn.classList.contains("ops-tab-active"));
    if (currentIndex < 0) return;
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    const tabId = tabs[nextIndex]?.getAttribute("data-tab");
    if (tabId) {
      closeStatInlineDetail();
      switchTab(tabId);
      tabs[nextIndex]?.focus();
    }
  });
}

function renderDashboard(data) {
  dashboardCache = data;
  renderSummary(data.summary || {});
  renderFocusBanner(data);
  renderDeviceNav(data);
  renderProjectTasks(data.project_tasks || []);
  renderBotFocusPanel(data);
  renderIssueQueues(data.issue_queues || {});
  renderAgentStatus(data);
  renderServer(data.server || {});
  renderBots(data.bots || []);
  renderSoakPanel(data.soak_test || data.intelligent_mode?.soak_test || {});
  renderIncidents(data.incidents || {});
  renderSmoke(data.last_smoke_run || null);
  document.getElementById("lastUpdated").textContent = `Updated ${formatTime(data.generated_at)}`;
  if (activeQueueFilter) {
    renderStatInlineDetail(activeQueueFilter, data);
  }
  const focus = data.focus || {};
  if (focus.device_id && focus.mode !== "fleet" && !initialFocusTabHandled) {
    initialFocusTabHandled = true;
    switchTab("bots");
  }
}

async function loadExtraPanels() {
  const [dev, fw, pending, proposals, agentStatus] = await Promise.all([
    api("/api/intelligent-mode/developer-issues").catch(() => ({ issues: [] })),
    api("/api/ota/firmware").catch(() => ({ builds: [] })),
    api("/api/ota/pending").catch(() => ({ pending: [] })),
    api("/api/coding-agent/proposals?status=pending").catch(() => ({ proposals: [] })),
    api("/api/coding-agent/status").catch(() => ({ enabled: false })),
  ]);
  renderDeveloperIssues(dev.issues || []);
  renderCodingAgentProposals(proposals.proposals || []);
  renderCodingAgentStatus(agentStatus);
  renderFirmware(fw.builds || [], pending.pending || []);
}

function renderCodingAgentStatus(status) {
  const el = document.getElementById("codingAgentStatus");
  if (!el) return;
  if (!status.enabled) {
    el.textContent = "Disabled — set CODING_AGENT_ENABLED=1 in .env";
    return;
  }
  const stats = status.stats || {};
  el.textContent = status.running
    ? `Running · ${status.model || "—"} · ${stats.in_flight || 0} in flight · ${status.pending_proposals || 0} pending · ${stats.proposals_created || 0} created`
    : "Stopped";
}

async function loadDashboard() {
  const statusPill = document.getElementById("connectionStatus");
  try {
    const fleetData = await api("/api/intelligent-mode/dashboard");
    fleetBotsCache = fleetData.bots || [];

    const dashboardPath =
      focusDeviceId && focusDeviceId !== "fleet"
        ? `/api/intelligent-mode/dashboard?device_id=${encodeURIComponent(focusDeviceId)}`
        : "/api/intelligent-mode/dashboard";
    const data =
      dashboardPath === "/api/intelligent-mode/dashboard"
        ? fleetData
        : await api(dashboardPath);

    await loadExtraPanels();
    renderDashboard(data);
    bindOtaActions();
    statusPill.textContent = "Live";
    statusPill.classList.add("connected");
  } catch (error) {
    statusPill.textContent = "Offline";
    statusPill.classList.remove("connected");
    throw error;
  }
}

function bindOtaActions() {
  document.querySelectorAll(".ops-ota-deploy").forEach((btn) => {
    btn.onclick = async () => {
      const deviceId = btn.getAttribute("data-device-id");
      const select = document.querySelector(`.ops-ota-select[data-device-id="${deviceId}"]`);
      const filename = select ? select.value : "";
      if (!filename) return;
      btn.disabled = true;
      try {
        await api(`/api/ota/deploy/${encodeURIComponent(deviceId)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename, require_approval: true }),
        });
        btn.textContent = "Queued";
        await loadExtraPanels();
      } catch (error) {
        btn.textContent = "Failed";
        console.error(error);
      }
    };
  });
  document.querySelectorAll(".ops-ota-approve").forEach((btn) => {
    btn.onclick = async () => {
      const approvalId = btn.getAttribute("data-approval-id");
      btn.disabled = true;
      try {
        await api(`/api/ota/approve/${encodeURIComponent(approvalId)}`, { method: "POST" });
        await loadExtraPanels();
      } catch (error) {
        console.error(error);
      }
    };
  });
}

async function runAction(button, path, successMessage) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Running…";
  try {
    await api(path, { method: "POST" });
    button.textContent = successMessage;
    await loadDashboard();
  } catch (error) {
    button.textContent = "Failed";
    console.error(error);
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = original;
    }, 1500);
  }
}

function initOpsDashboard() {
  focusDeviceId = parseFocusDeviceFromPage();
  bindTabs();
  bindStatCards();

  document.getElementById("refreshBtn")?.addEventListener("click", () => {
    loadDashboard().catch(console.error);
  });

  document.getElementById("runTickBtn")?.addEventListener("click", (event) => {
    runAction(event.currentTarget, "/api/intelligent-mode/run", "Done").catch(console.error);
  });

  document.getElementById("runSmokeBtn")?.addEventListener("click", (event) => {
    runAction(event.currentTarget, "/api/intelligent-mode/tests/run", "Done").catch(console.error);
  });

  document.getElementById("firmwareUploadBtn")?.addEventListener("click", async () => {
    const input = document.getElementById("firmwareUploadInput");
    const btn = document.getElementById("firmwareUploadBtn");
    if (!input || !btn) return;
    if (!input.files || !input.files.length) {
      btn.textContent = "Choose .bin first";
      return;
    }
    const file = input.files[0];
    const form = new FormData();
    form.append("file", file);
    btn.disabled = true;
    btn.textContent = "Uploading…";
    try {
      const res = await fetch("/api/ota/firmware/upload", { method: "POST", body: form });
      if (!res.ok) throw new Error(await res.text());
      btn.textContent = "Uploaded";
      input.value = "";
      await loadExtraPanels();
      const data = await api("/api/intelligent-mode/dashboard");
      renderDashboard(data);
      bindOtaActions();
    } catch (error) {
      btn.textContent = "Upload failed";
      console.error(error);
    } finally {
      window.setTimeout(() => {
        btn.disabled = false;
        btn.textContent = "Upload .bin";
      }, 1500);
    }
  });

  loadDashboard().catch(console.error);
  window.setInterval(() => {
    loadDashboard().catch(console.error);
  }, REFRESH_MS);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initOpsDashboard);
} else {
  initOpsDashboard();
}
