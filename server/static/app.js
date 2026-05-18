const cameraSource = document.querySelector("#cameraSource");
const startCamera = document.querySelector("#startCamera");
const personName = document.querySelector("#personName");
const sampleCount = document.querySelector("#sampleCount");
const registerPerson = document.querySelector("#registerPerson");
const retrain = document.querySelector("#retrain");
const statusBox = document.querySelector("#statusBox");
const connectionStatus = document.querySelector("#connectionStatus");
const stream = document.querySelector("#stream");

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

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const connected = data.camera.connected;
    connectionStatus.textContent = connected ? "Camera connected" : "Waiting for camera";
    connectionStatus.classList.toggle("connected", connected);
    statusBox.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    connectionStatus.textContent = "Server error";
    connectionStatus.classList.remove("connected");
    statusBox.textContent = error.message;
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

refreshStatus();
setInterval(refreshStatus, 1500);
