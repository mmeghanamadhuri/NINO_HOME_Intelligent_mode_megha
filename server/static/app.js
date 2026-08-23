const cameraSource = document.querySelector("#cameraSource");
const cameraOrientation = document.querySelector("#cameraOrientation");
const startCamera = document.querySelector("#startCamera");
const personName = document.querySelector("#personName");
const sampleCount = document.querySelector("#sampleCount");
const registerPerson = document.querySelector("#registerPerson");
const retrain = document.querySelector("#retrain");
const statusBox = document.querySelector("#statusBox");
const connectionStatus = document.querySelector("#connectionStatus");
const stream = document.querySelector("#stream");
const streamOffline = document.querySelector("#streamOffline");
const streamOfflineDetail = document.querySelector("#streamOfflineDetail");
const alarmList = document.querySelector("#alarmList");
const clearAllAlarms = document.querySelector("#clearAllAlarms");
const deviceSelect = document.querySelector("#deviceSelect");
const snapshotLink = document.querySelector("#snapshotLink");

let cameraConnected = false;

function currentDeviceId() {
  if (deviceSelect && deviceSelect.value) {
    return deviceSelect.value.trim();
  }
  return (stream?.dataset?.deviceId || "default").trim() || "default";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Request failed");
  }
  return data;
}

function refreshStream() {
  const id = currentDeviceId();
  stream.dataset.deviceId = id;
  stream.src = `/video_feed?device_id=${encodeURIComponent(id)}&t=${Date.now()}`;
  if (snapshotLink) {
    snapshotLink.href = `/snapshot.jpg?device_id=${encodeURIComponent(id)}`;
  }
}

function updateStreamOffline(connected, cameraError) {
  if (!stream || !streamOffline) {
    return;
  }
  const offline = !connected;
  streamOffline.hidden = !offline;
  stream.classList.toggle("stream-hidden", offline);
  if (streamOfflineDetail) {
    streamOfflineDetail.textContent = offline && cameraError ? cameraError : "";
  }
}

function renderAlarms(pending, awaiting) {
  alarmList.innerHTML = "";
  const rows = [
    ...awaiting.map((a) => ({ ...a, _awaiting: true })),
    ...pending,
  ];
  for (const alarm of rows) {
    const li = document.createElement("li");
    li.className = "alarm-item";

    const text = document.createElement("div");
    const p0 = alarm.priority === 0 || alarm.category === "medical";
    const title = alarm._awaiting
      ? `${alarm.label || "Medication"} — awaiting confirmation`
      : alarm.label || (p0 ? "Medical reminder" : "Alarm");
    const badge = p0 ? "P0 · " : "";
    const who = alarm.person_name ? ` · ${alarm.person_name}` : "";
    const device = alarm.device_id ? ` · ${alarm.device_id}` : "";
    const repeat = alarm.next_repeat_at ? ` · repeats ${alarm.next_repeat_at}` : "";
    text.innerHTML = `<strong>${badge}${title}</strong><span>${alarm.spoken_time || alarm.fire_at}${who}${device}${repeat}</span>`;

    const actions = document.createElement("div");
    actions.className = "alarm-actions";

    if (alarm._awaiting) {
      const yes = document.createElement("button");
      yes.type = "button";
      yes.textContent = "Yes";
      yes.className = "ack-yes";
      yes.addEventListener("click", async () => {
        yes.disabled = true;
        try {
          await api(`/api/alarms/${alarm.id}/ack`, {
            method: "POST",
            body: JSON.stringify({ response: "yes" }),
          });
          await refreshStatus();
        } catch (error) {
          alert(error.message);
        }
      });

      const no = document.createElement("button");
      no.type = "button";
      no.textContent = "No";
      no.className = "ack-no";
      no.addEventListener("click", async () => {
        no.disabled = true;
        try {
          const res = await api(`/api/alarms/${alarm.id}/ack`, {
            method: "POST",
            body: JSON.stringify({ response: "no" }),
          });
          if (res.message) alert(res.message);
          await refreshStatus();
        } catch (error) {
          alert(error.message);
        }
      });

      actions.append(yes, no);
    }

    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "Delete";
    del.className = "delete-btn";
    del.addEventListener("click", async () => {
      del.disabled = true;
      try {
        await api(`/api/alarms/${alarm.id}`, { method: "DELETE" });
        await refreshStatus();
      } catch (error) {
        alert(error.message);
      } finally {
        del.disabled = false;
      }
    });

    actions.append(del);
    li.append(text, actions);
    alarmList.append(li);
  }
}

function setDeviceCount(count) {
  const el = document.querySelector("#deviceCount");
  if (!el) {
    return;
  }
  const n = Number(count) || 0;
  el.textContent = n === 1 ? "1 robot" : `${n} robots`;
}

function populateDeviceSelect(devices, selectedId) {
  const list = Array.isArray(devices) ? devices : [];
  setDeviceCount(list.length);
  if (!deviceSelect) {
    return;
  }
  const previous = selectedId || currentDeviceId();
  const same =
    deviceSelect.options.length === list.length &&
    list.every((d, i) => deviceSelect.options[i]?.value === d.device_id);
  if (same && list.length > 0) {
    return;
  }
  deviceSelect.innerHTML = "";
  if (list.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No robots found";
    deviceSelect.append(opt);
    return;
  }
  for (const d of list) {
    const opt = document.createElement("option");
    opt.value = d.device_id;
    opt.textContent = d.display_name || d.device_id;
    if (d.device_id === previous) {
      opt.selected = true;
    }
    deviceSelect.append(opt);
  }
  if (![...deviceSelect.options].some((o) => o.selected) && deviceSelect.options.length) {
    deviceSelect.options[0].selected = true;
  }
}

async function refreshStatus() {
  try {
    const id = currentDeviceId();
    const data = await api(`/api/status?device_id=${encodeURIComponent(id)}`);
    const devices = data.devices?.devices || [];
    populateDeviceSelect(devices, data.device_id || id);
    if (typeof data.devices?.count === "number") {
      setDeviceCount(data.devices.count);
    }
    const connected = Boolean(data.camera?.connected);
    cameraConnected = connected;
    const cameraError = String(data.camera?.last_error || "").trim();
    if (cameraOrientation && data.camera?.rotation) {
      cameraOrientation.value = data.camera.rotation;
    }
    connectionStatus.textContent = connected
      ? `Camera connected (${data.device_id || id})`
      : `Camera offline — wake robot or start stream (${data.device_id || id})`;
    connectionStatus.classList.toggle("connected", connected);
    updateStreamOffline(connected, cameraError);
    statusBox.textContent = JSON.stringify(data, null, 2);
    renderAlarms(data.alarms?.pending || [], data.alarms?.awaiting_ack || []);
  } catch (error) {
    connectionStatus.textContent = "Server error";
    connectionStatus.classList.remove("connected");
    updateStreamOffline(false, error.message);
    statusBox.textContent = error.message;
    renderAlarms([], []);
  }
}

if (deviceSelect) {
  deviceSelect.addEventListener("change", async () => {
    const id = currentDeviceId();
    try {
      await api("/api/device", {
        method: "POST",
        body: JSON.stringify({ device_id: id }),
      });
      refreshStream();
      await refreshStatus();
    } catch (error) {
      alert(error.message);
    }
  });
}

startCamera.addEventListener("click", async () => {
  startCamera.disabled = true;
  try {
    await api("/api/camera", {
      method: "POST",
      body: JSON.stringify({
        source: cameraSource.value.trim(),
        device_id: currentDeviceId(),
      }),
    });
    refreshStream();
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  } finally {
    startCamera.disabled = false;
  }
});

cameraOrientation?.addEventListener("change", async () => {
  const rotation = cameraOrientation.value;
  cameraOrientation.disabled = true;
  try {
    await api("/api/camera/orientation", {
      method: "POST",
      body: JSON.stringify({
        rotation,
        device_id: currentDeviceId(),
      }),
    });
    refreshStream();
    await refreshStatus();
  } catch (error) {
    alert(error.message);
    await refreshStatus();
  } finally {
    cameraOrientation.disabled = false;
  }
});

registerPerson.addEventListener("click", async () => {
  registerPerson.disabled = true;
  registerPerson.textContent = "Capturing...";
  try {
    const result = await api("/api/register", {
      method: "POST",
      body: JSON.stringify({
        name: personName.value.trim(),
        samples: Number(sampleCount.value),
        device_id: currentDeviceId(),
      }),
    });
    alert(`Registered ${result.saved_samples} samples`);
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  } finally {
    registerPerson.disabled = false;
    registerPerson.textContent = "Capture Samples and Train";
  }
});

clearAllAlarms.addEventListener("click", async () => {
  if (!confirm("Delete all pending alarms?")) {
    return;
  }
  clearAllAlarms.disabled = true;
  try {
    await api("/api/alarms", { method: "DELETE" });
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  } finally {
    clearAllAlarms.disabled = false;
  }
});

retrain.addEventListener("click", async () => {
  retrain.disabled = true;
  try {
    await api("/api/retrain", { method: "POST", body: "{}" });
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  } finally {
    retrain.disabled = false;
  }
});

stream.addEventListener("load", () => {
  if (!cameraConnected) {
    return;
  }
  stream.classList.remove("stream-hidden");
  if (streamOffline) {
    streamOffline.hidden = true;
  }
});

stream.addEventListener("error", () => {
  updateStreamOffline(false, "Live feed unavailable");
  window.setTimeout(refreshStream, 1500);
});

refreshStream();
refreshStatus();
setInterval(refreshStatus, 1500);
