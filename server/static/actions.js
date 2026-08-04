const deviceSelect = document.querySelector("#deviceSelect");
const connectionStatus = document.querySelector("#connectionStatus");
const botBaseHint = document.querySelector("#botBaseHint");
const liveTilt = document.querySelector("#liveTilt");
const livePan = document.querySelector("#livePan");
const liveMode = document.querySelector("#liveMode");
const motorTilt = document.querySelector("#motorTilt");
const motorPan = document.querySelector("#motorPan");
const actionName = document.querySelector("#actionName");
const defaultHold = document.querySelector("#defaultHold");
const frameStrip = document.querySelector("#frameStrip");
const frameDetail = document.querySelector("#frameDetail");
const actionList = document.querySelector("#actionList");
const editorStatus = document.querySelector("#editorStatus");

const STORAGE_KEY = "nino_servo_actions_v1";

let pollTimer = null;
let selectedIndex = -1;
/** @type {{hold_ms:number, p: Record<string, number>}[]} */
let frames = [];
/** @type {string|null} */
let editingId = null;

function deviceId() {
  return (deviceSelect?.value || "default").trim() || "default";
}

function setStatus(text, ok = true) {
  if (connectionStatus) {
    connectionStatus.textContent = text;
    connectionStatus.style.opacity = ok ? "1" : "0.85";
  }
}

function setEditorStatus(text) {
  if (editorStatus) editorStatus.textContent = text || "";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.error || data.message || "Request failed");
  }
  return data;
}

function selectedMotorIds() {
  const ids = [];
  if (motorTilt?.checked) ids.push(1);
  if (motorPan?.checked) ids.push(2);
  return ids;
}

function snapshotFromLive(live) {
  const ids = selectedMotorIds();
  if (!ids.length) throw new Error("Select at least one motor");
  const p = {};
  for (const id of ids) {
    const servo = (live.servos || []).find((s) => s.id === id);
    if (!servo || servo.ok === false) {
      throw new Error(`Could not read position for ID${id}`);
    }
    p[String(id)] = Number(servo.position);
  }
  const hold = Math.max(0, Number(defaultHold.value) || 500);
  return { hold_ms: frames.length === 0 ? 0 : hold, p };
}

function reindex() {
  /* frames array order is the join order */
}

function renderFrames() {
  frameStrip.innerHTML = "";
  if (!frames.length) {
    frameStrip.innerHTML = `<p class="hint">No frames yet — Start edit, move, Add Frame.</p>`;
    frameDetail.innerHTML = `<p class="hint">Select a frame to edit hold time.</p>`;
    return;
  }
  frames.forEach((f, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "frame-chip" + (i === selectedIndex ? " selected" : "");
    const parts = Object.entries(f.p)
      .map(([id, pos]) => `ID${id}:${pos}`)
      .join(" ");
    btn.innerHTML = `<span class="idx">F${i}</span>${parts}<br/>${f.hold_ms}ms`;
    btn.addEventListener("click", () => {
      selectedIndex = i;
      renderFrames();
    });
    frameStrip.appendChild(btn);
  });

  if (selectedIndex < 0 || selectedIndex >= frames.length) {
    frameDetail.innerHTML = `<p class="hint">Select a frame to edit hold time.</p>`;
    return;
  }
  const f = frames[selectedIndex];
  const pose = Object.entries(f.p)
    .map(([id, pos]) => `ID${id} = ${pos}`)
    .join(", ");
  frameDetail.innerHTML = `
    <p><strong>Frame ${selectedIndex}</strong> — ${pose}</p>
    <div class="field hold-field">
      <label for="frameHold">Hold (ms)</label>
      <input id="frameHold" type="number" min="0" max="60000" step="50" value="${f.hold_ms}" />
    </div>`;
  const holdInput = document.querySelector("#frameHold");
  holdInput?.addEventListener("change", () => {
    frames[selectedIndex].hold_ms = Math.max(0, Number(holdInput.value) || 0);
    renderFrames();
  });
}

function loadStore() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveStore(list) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
}

function durationMs(list) {
  return list.reduce((sum, f) => sum + (Number(f.hold_ms) || 0), 0);
}

function renderActionList() {
  const list = loadStore();
  actionList.innerHTML = "";
  if (!list.length) {
    actionList.innerHTML = `<li class="hint">No saved actions yet.</li>`;
    return;
  }
  for (const act of list) {
    const li = document.createElement("li");
    li.className = "action-item";
    const motors = (act.motors || []).join(",");
    li.innerHTML = `
      <div>
        <strong>${escapeHtml(act.name || "Untitled")}</strong>
        <span class="meta">${(act.frames || []).length} frames · ${durationMs(act.frames || [])} ms · motors [${motors}]</span>
      </div>
      <div class="row-actions"></div>`;
    const row = li.querySelector(".row-actions");

    const play = document.createElement("button");
    play.type = "button";
    play.textContent = "Play";
    play.addEventListener("click", () => playFrames(act.frames || [], act.name));

    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "secondary";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => {
      editingId = act.id;
      actionName.value = act.name || "Untitled";
      frames = structuredClone(act.frames || []);
      selectedIndex = frames.length ? 0 : -1;
      setEditorStatus(`Editing “${act.name}”`);
      renderFrames();
    });

    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "secondary";
    rename.textContent = "Rename";
    rename.addEventListener("click", () => {
      const next = prompt("Rename action", act.name || "");
      if (next == null) return;
      const trimmed = next.trim();
      if (!trimmed) return;
      act.name = trimmed;
      act.updated_at = new Date().toISOString();
      const all = loadStore().map((a) => (a.id === act.id ? act : a));
      saveStore(all);
      if (editingId === act.id) actionName.value = trimmed;
      renderActionList();
    });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "danger";
    del.textContent = "Delete";
    del.addEventListener("click", () => {
      if (!confirm(`Delete “${act.name}”?`)) return;
      saveStore(loadStore().filter((a) => a.id !== act.id));
      if (editingId === act.id) {
        editingId = null;
        frames = [];
        selectedIndex = -1;
        renderFrames();
      }
      renderActionList();
    });

    row.append(play, edit, rename, del);
    actionList.appendChild(li);
  }
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function pollPosition() {
  try {
    const data = await api(`/api/servo/position?device_id=${encodeURIComponent(deviceId())}`);
    const s1 = (data.servos || []).find((s) => s.id === 1);
    const s2 = (data.servos || []).find((s) => s.id === 2);
    liveTilt.textContent = s1?.ok === false ? "err" : String(s1?.position ?? "—");
    livePan.textContent = s2?.ok === false ? "err" : String(s2?.position ?? "—");
    liveMode.textContent = data.mode || "idle";
    setStatus(data.ready ? "Servos ready" : "Bus open / not ready");
    botBaseHint.textContent = data._base_url
      ? `Bot: ${data._base_url}`
      : "Bot URL from device registry / ESP_PLAY_WAV_URL";
    return data;
  } catch (err) {
    setStatus(String(err.message || err), false);
    liveMode.textContent = "—";
    return null;
  }
}

async function startRecord() {
  const ids = selectedMotorIds();
  if (!ids.length) {
    setEditorStatus("Select at least one motor");
    return;
  }
  await api(`/api/servo/record?device_id=${encodeURIComponent(deviceId())}`, {
    method: "POST",
    body: JSON.stringify({ action: "start", ids, torque_off: true }),
  });
  setEditorStatus("Edit mode on — move head, then Add Frame");
  await pollPosition();
}

async function stopRecord() {
  await api(`/api/servo/record?device_id=${encodeURIComponent(deviceId())}`, {
    method: "POST",
    body: JSON.stringify({ action: "stop" }),
  });
  setEditorStatus("Edit mode off");
  await pollPosition();
}

async function addFrameAt(mode) {
  const live = await pollPosition();
  if (!live) throw new Error("No live positions");
  const frame = snapshotFromLive(live);
  if (mode === "append" || selectedIndex < 0 || !frames.length) {
    frames.push(frame);
    selectedIndex = frames.length - 1;
  } else if (mode === "before") {
    frames.splice(selectedIndex, 0, frame);
  } else if (mode === "after") {
    frames.splice(selectedIndex + 1, 0, frame);
    selectedIndex += 1;
  } else if (mode === "replace") {
    frame.hold_ms = frames[selectedIndex].hold_ms;
    frames[selectedIndex] = frame;
  }
  reindex();
  renderFrames();
  setEditorStatus(`Frames: ${frames.length}`);
}

async function playFrames(frameList, name) {
  if (!frameList?.length) {
    setEditorStatus("No frames to play");
    return;
  }
  const payload = {
    name: name || actionName.value || "action",
    speed: 22,
    frames: frameList.map((f) => ({
      hold_ms: Number(f.hold_ms) || 0,
      p: f.p,
    })),
  };
  const data = await api(`/api/servo/play?device_id=${encodeURIComponent(deviceId())}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
  setEditorStatus(`Play started (${data.frames || frameList.length} frames)`);
  await pollPosition();
}

function saveCurrentAction() {
  const name = (actionName.value || "").trim();
  if (!name) {
    setEditorStatus("Name is required");
    return;
  }
  if (!frames.length) {
    setEditorStatus("Add at least one frame");
    return;
  }
  const now = new Date().toISOString();
  const motors = [
    ...new Set(frames.flatMap((f) => Object.keys(f.p || {}).map(Number))),
  ].sort();
  const all = loadStore();
  if (editingId) {
    const idx = all.findIndex((a) => a.id === editingId);
    if (idx >= 0) {
      all[idx] = {
        ...all[idx],
        name,
        updated_at: now,
        motors,
        frames: structuredClone(frames),
      };
    }
  } else {
    editingId = `act_${Date.now()}`;
    all.unshift({
      id: editingId,
      name,
      created_at: now,
      updated_at: now,
      motors,
      frames: structuredClone(frames),
    });
  }
  saveStore(all);
  setEditorStatus(`Saved “${name}”`);
  renderActionList();
}

function newAction() {
  editingId = null;
  frames = [];
  selectedIndex = -1;
  actionName.value = "New action";
  setEditorStatus("New action");
  renderFrames();
}

document.querySelector("#btnRecordStart")?.addEventListener("click", () => {
  startRecord().catch((e) => setEditorStatus(String(e.message || e)));
});
document.querySelector("#btnRecordStop")?.addEventListener("click", () => {
  stopRecord().catch((e) => setEditorStatus(String(e.message || e)));
});
document.querySelector("#btnAddFrame")?.addEventListener("click", () => {
  addFrameAt("append").catch((e) => setEditorStatus(String(e.message || e)));
});
document.querySelector("#btnInsertBefore")?.addEventListener("click", () => {
  addFrameAt("before").catch((e) => setEditorStatus(String(e.message || e)));
});
document.querySelector("#btnInsertAfter")?.addEventListener("click", () => {
  addFrameAt("after").catch((e) => setEditorStatus(String(e.message || e)));
});
document.querySelector("#btnReplaceFrame")?.addEventListener("click", () => {
  if (selectedIndex < 0) {
    setEditorStatus("Select a frame to replace");
    return;
  }
  addFrameAt("replace").catch((e) => setEditorStatus(String(e.message || e)));
});
document.querySelector("#btnDeleteFrame")?.addEventListener("click", () => {
  if (selectedIndex < 0 || !frames.length) return;
  frames.splice(selectedIndex, 1);
  if (selectedIndex >= frames.length) selectedIndex = frames.length - 1;
  renderFrames();
  setEditorStatus(`Frames: ${frames.length}`);
});
document.querySelector("#btnSaveAction")?.addEventListener("click", saveCurrentAction);
document.querySelector("#btnPlayAction")?.addEventListener("click", () => {
  playFrames(frames, actionName.value).catch((e) => setEditorStatus(String(e.message || e)));
});
document.querySelector("#btnStopPlay")?.addEventListener("click", () => {
  api(`/api/servo/play?device_id=${encodeURIComponent(deviceId())}`, {
    method: "POST",
    body: JSON.stringify({ action: "stop" }),
  })
    .then(() => setEditorStatus("Stop requested"))
    .catch((e) => setEditorStatus(String(e.message || e)));
});
document.querySelector("#btnNewAction")?.addEventListener("click", newAction);
document.querySelector("#btnRename")?.addEventListener("click", () => {
  const next = prompt("Action name", actionName.value || "");
  if (next == null) return;
  const trimmed = next.trim();
  if (!trimmed) return;
  actionName.value = trimmed;
  if (editingId) {
    const all = loadStore();
    const idx = all.findIndex((a) => a.id === editingId);
    if (idx >= 0) {
      all[idx].name = trimmed;
      all[idx].updated_at = new Date().toISOString();
      saveStore(all);
      renderActionList();
    }
  }
  setEditorStatus(`Named “${trimmed}”`);
});

deviceSelect?.addEventListener("change", () => {
  pollPosition();
});

renderFrames();
renderActionList();
pollPosition();
pollTimer = setInterval(pollPosition, 500);
