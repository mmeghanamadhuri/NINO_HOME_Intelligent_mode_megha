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
  stream.src = `/video_feed?t=${Date.now()}`;
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
    const repeat = alarm.next_repeat_at ? ` · repeats ${alarm.next_repeat_at}` : "";
    text.innerHTML = `<strong>${badge}${title}</strong><span>${alarm.spoken_time || alarm.fire_at}${who}${repeat}</span>`;

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

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const connected = data.camera.connected;
    connectionStatus.textContent = connected ? "Camera connected" : "Waiting for camera";
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

startCamera.addEventListener("click", async () => {
  startCamera.disabled = true;
  try {
    await api("/api/camera", {
      method: "POST",
      body: JSON.stringify({ source: cameraSource.value.trim() }),
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
