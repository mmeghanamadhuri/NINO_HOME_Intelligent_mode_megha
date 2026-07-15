const cameraSource = document.querySelector("#cameraSource");
const startCamera = document.querySelector("#startCamera");
const personName = document.querySelector("#personName");
const sampleCount = document.querySelector("#sampleCount");
const registerPerson = document.querySelector("#registerPerson");
const retrain = document.querySelector("#retrain");
const statusBox = document.querySelector("#statusBox");
const connectionStatus = document.querySelector("#connectionStatus");
const stream = document.querySelector("#stream");
const alarmList = document.querySelector("#alarmList");
const clearAllAlarms = document.querySelector("#clearAllAlarms");
const deviceSelect = document.querySelector("#deviceSelect");
const snapshotLink = document.querySelector("#snapshotLink");

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

function populateDeviceSelect(devices, selectedId) {
  if (!deviceSelect || !Array.isArray(devices) || devices.length === 0) {
    return;
  }
  const previous = selectedId || currentDeviceId();
  deviceSelect.innerHTML = "";
  for (const d of devices) {
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
    if (data.devices?.devices) {
      populateDeviceSelect(data.devices.devices, data.device_id || id);
    }
    const connected = data.camera?.connected;
    connectionStatus.textContent = connected
      ? `Camera connected (${data.device_id || id})`
      : `Waiting for camera (${data.device_id || id})`;
    connectionStatus.classList.toggle("connected", connected);
    statusBox.textContent = JSON.stringify(data, null, 2);
    renderAlarms(data.alarms?.pending || [], data.alarms?.awaiting_ack || []);
  } catch (error) {
    connectionStatus.textContent = "Server error";
    connectionStatus.classList.remove("connected");
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

stream.addEventListener("error", () => {
  window.setTimeout(refreshStream, 1500);
});

refreshStream();
refreshStatus();
setInterval(refreshStatus, 1500);
