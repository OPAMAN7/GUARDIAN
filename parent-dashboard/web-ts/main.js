/**
 * Guardian Shield — Dashboard SPA
 * Single-file JavaScript app with login, live view, devices, blocklist, alerts.
 * Connects to the server via REST API and Socket.IO for real-time updates.
 */

import { io } from "socket.io-client";

const DISPLAY_SETTINGS_KEY = "gs_display_settings";

function getInitialDisplaySettings() {
  const defaults = {
    fit_mode: "contain",
    zoom: 100,
    brightness: 100,
    contrast: 100,
  };

  try {
    const raw = localStorage.getItem(DISPLAY_SETTINGS_KEY);
    if (!raw) return defaults;
    const parsed = JSON.parse(raw);
    return {
      fit_mode: parsed.fit_mode === "cover" ? "cover" : "contain",
      zoom: Math.max(80, Math.min(150, parseInt(parsed.zoom || 100, 10))),
      brightness: Math.max(70, Math.min(140, parseInt(parsed.brightness || 100, 10))),
      contrast: Math.max(70, Math.min(140, parseInt(parsed.contrast || 100, 10))),
    };
  } catch (_err) {
    return defaults;
  }
}

// ─── State ──────────────────────────────────────────────────────────────────

const state = {
  token: localStorage.getItem("gs_token") || null,
  currentPage: "live",
  presentationMode: new URLSearchParams(window.location.search).get("presentation") === "1",
  controllerMode: new URLSearchParams(window.location.search).get("controller") === "1",
  audioReady: false,
  streamTargetFps: 20,
  streamTimer: null,
  pendingLiveFrame: null,
  frameRenderToken: 0,
  streamConfig: {
    target_fps: 20,
    screenshot_quality: 50,
    max_width: 1280,
    low_latency_mode: true,
  },
  displaySettings: getInitialDisplaySettings(),
  devices: [],
  selectedDeviceId: null,
  alerts: [],
  unreadAlerts: 0,
  blocklist: [],
  socket: null,
  connected: false,
  latestScreenshots: {},
  deviceTelemetry: {},
  telemetryPollTimer: null,
  lastTelemetryUiRenderAt: 0,
};

// ─── API Helper ─────────────────────────────────────────────────────────────

const API_BASE = "/api";

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  const data = await res.json();

  if (res.status === 401) {
    state.token = null;
    localStorage.removeItem("gs_token");
    render();
    return null;
  }

  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

// ─── Toast Notifications ────────────────────────────────────────────────────

function showToast(message, type = "info") {
  const container =
    document.querySelector(".toast-container") || createToastContainer();
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `${
    type === "success" ? "✅" : type === "error" ? "❌" : type === "warning" ? "⚠️" : "ℹ️"
  } ${message}`;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

function createToastContainer() {
  const c = document.createElement("div");
  c.className = "toast-container";
  document.body.appendChild(c);
  return c;
}

let audioContext = null;

function unlockDashboardAudio() {
  if (state.audioReady) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    audioContext = new Ctx();
    if (audioContext.state === "suspended") {
      audioContext.resume();
    }
    state.audioReady = true;
  } catch (err) {
    console.debug("Audio unlock skipped:", err);
  }
}

function playDashboardBuzzer(level = 2) {
  if (!audioContext || !state.audioReady) return;

  const steps = Math.max(1, Math.min(level, 4));
  const now = audioContext.currentTime;
  for (let i = 0; i < steps; i++) {
    const start = now + i * 0.16;
    const osc = audioContext.createOscillator();
    const gain = audioContext.createGain();
    osc.type = "square";
    osc.frequency.value = i % 2 === 0 ? 1320 : 880;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.16, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.13);
    osc.connect(gain);
    gain.connect(audioContext.destination);
    osc.start(start);
    osc.stop(start + 0.14);
  }
}

function scheduleLiveFrameRender(frameData) {
  state.pendingLiveFrame = frameData;
  if (state.streamTimer) return;

  const intervalMs = Math.max(20, Math.floor(1000 / state.streamTargetFps));
  state.streamTimer = setTimeout(() => {
    state.streamTimer = null;
    const nextFrame = state.pendingLiveFrame;
    state.pendingLiveFrame = null;

    if (nextFrame) {
      updateScreenView(nextFrame);
    }
    if (state.pendingLiveFrame) {
      scheduleLiveFrameRender(state.pendingLiveFrame);
    }
  }, intervalMs);
}

function parseFrameTimestamp(payload) {
  if (!payload) return null;

  const raw = payload.timestamp || payload.captured_at || null;
  if (raw == null) return null;

  if (typeof raw === "number") {
    // Agent sends unix seconds.
    return raw > 1000000000000 ? raw : Math.floor(raw * 1000);
  }

  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? null : parsed;
}

function isFreshFrame(payload, maxAgeMs = 15000) {
  const ts = parseFrameTimestamp(payload);
  if (!ts) return false;
  return Date.now() - ts <= maxAgeMs;
}

function normalizeDomainInput(rawValue) {
  const input = String(rawValue || "").trim().toLowerCase();
  if (!input) return "";

  try {
    const withProto = /^[a-z][a-z0-9+.-]*:\/\//i.test(input) ? input : `http://${input}`;
    const parsed = new URL(withProto);
    return (parsed.hostname || "").replace(/\.+$/, "").toLowerCase();
  } catch (_err) {
    return input.replace(/^https?:\/\//, "").split(/[\/?#:]/)[0].replace(/\.+$/, "").toLowerCase();
  }
}

function ensureSelectedDeviceIsOnline(preferredId = null) {
  const onlineDevices = state.devices.filter((d) => d.status === "online");
  if (onlineDevices.length === 0) {
    state.selectedDeviceId = null;
    return;
  }

  const preferred = preferredId
    ? onlineDevices.find((d) => d.id === preferredId)
    : null;
  if (preferred) {
    state.selectedDeviceId = preferred.id;
    return;
  }

  const existing = onlineDevices.find((d) => d.id === state.selectedDeviceId);
  if (existing) return;

  state.selectedDeviceId = onlineDevices[0].id;
}

function applyStreamProfile(profileName) {
  if (profileName === "p24") {
    state.streamConfig.target_fps = 24;
    state.streamConfig.screenshot_quality = 48;
    state.streamConfig.max_width = 1280;
  } else if (profileName === "p30") {
    state.streamConfig.target_fps = 30;
    state.streamConfig.screenshot_quality = 42;
    state.streamConfig.max_width = 960;
  } else {
    state.streamConfig.target_fps = 20;
    state.streamConfig.screenshot_quality = 50;
    state.streamConfig.max_width = 1280;
  }

  state.streamTargetFps = state.streamConfig.target_fps;
  syncStreamControlsUI();
}

function syncStreamControlsUI() {
  const fps = document.getElementById("stream-fps");
  const quality = document.getElementById("stream-quality");
  const width = document.getElementById("stream-width");
  const fpsVal = document.getElementById("stream-fps-value");
  const qualityVal = document.getElementById("stream-quality-value");

  if (fps) fps.value = String(state.streamConfig.target_fps);
  if (quality) quality.value = String(state.streamConfig.screenshot_quality);
  if (width) width.value = String(state.streamConfig.max_width);
  if (fpsVal) fpsVal.textContent = `${state.streamConfig.target_fps} FPS`;
  if (qualityVal) qualityVal.textContent = `${state.streamConfig.screenshot_quality}%`;
}

function applyDisplaySettingsToView() {
  const img = document.getElementById("live-screen-img");
  if (!img) return;

  img.style.objectFit = state.displaySettings.fit_mode;
  img.style.transform = `scale(${state.displaySettings.zoom / 100})`;
  img.style.transformOrigin = "center center";
  img.style.filter = `brightness(${state.displaySettings.brightness}%) contrast(${state.displaySettings.contrast}%)`;
}

function syncDisplayControlsUI() {
  const fit = document.getElementById("display-fit-mode");
  const zoom = document.getElementById("display-zoom");
  const bright = document.getElementById("display-brightness");
  const contrast = document.getElementById("display-contrast");
  const zoomVal = document.getElementById("display-zoom-value");
  const brightVal = document.getElementById("display-brightness-value");
  const contrastVal = document.getElementById("display-contrast-value");

  if (fit) fit.value = state.displaySettings.fit_mode;
  if (zoom) zoom.value = String(state.displaySettings.zoom);
  if (bright) bright.value = String(state.displaySettings.brightness);
  if (contrast) contrast.value = String(state.displaySettings.contrast);
  if (zoomVal) zoomVal.textContent = `${state.displaySettings.zoom}%`;
  if (brightVal) brightVal.textContent = `${state.displaySettings.brightness}%`;
  if (contrastVal) contrastVal.textContent = `${state.displaySettings.contrast}%`;
}

function persistDisplaySettings() {
  localStorage.setItem(DISPLAY_SETTINGS_KEY, JSON.stringify(state.displaySettings));
  applyDisplaySettingsToView();
  syncDisplayControlsUI();
}

function openOutputControllerWindow() {
  window.open(
    `${window.location.origin}/?controller=1`,
    "guardian-output-controller",
    "popup=yes,width=740,height=860,resizable=yes,scrollbars=yes"
  );
}

function pushStreamConfig(silent = false) {
  if (!state.socket || !state.socket.connected) {
    if (!silent) showToast("Not connected to server", "warning");
    return;
  }
  if (!state.selectedDeviceId) {
    if (!silent) showToast("Select a device first", "warning");
    return;
  }

  const selected = state.devices.find((d) => d.id === state.selectedDeviceId);
  if (!selected || selected.status !== "online") {
    if (!silent) showToast("Select an online device first", "warning");
    return;
  }

  state.streamTargetFps = state.streamConfig.target_fps;
  state.socket.emit("stream_config", {
    device_id: state.selectedDeviceId,
    config: state.streamConfig,
  });
}

// ─── Socket.IO Setup ────────────────────────────────────────────────────────

function initSocket() {
  if (state.socket) state.socket.disconnect();

  state.socket = io({ transports: ["websocket", "polling"] });

  state.socket.on("connect", () => {
    state.connected = true;
    updateConnectionStatus();
  });

  state.socket.on("disconnect", () => {
    state.connected = false;
    updateConnectionStatus();
  });

  state.socket.on("live_screenshot", (data) => {
    state.latestScreenshots[data.device_id] = data;
    if (data?.meta && typeof data.meta === "object") {
      state.deviceTelemetry[data.device_id] = data.meta;
      const now = Date.now();
      if (state.currentPage === "live" && now - state.lastTelemetryUiRenderAt >= 2000) {
        state.lastTelemetryUiRenderAt = now;
        renderDevicesList();
      }
    }
    if (data.device_id === state.selectedDeviceId && state.currentPage === "live") {
      scheduleLiveFrameRender(data);
    }
  });

  state.socket.on("device_telemetry", (data) => {
    if (!data || !data.device_id) return;
    state.deviceTelemetry[data.device_id] = data;
    const now = Date.now();
    if (state.currentPage === "live" && now - state.lastTelemetryUiRenderAt >= 2000) {
      state.lastTelemetryUiRenderAt = now;
      renderDevicesList();
    }
  });

  state.socket.on("device_status_changed", (data) => {
    const device = state.devices.find((d) => d.id === data.device_id);
    if (device) {
      device.status = data.status;
    } else {
      loadDevices();
    }

    ensureSelectedDeviceIsOnline();
    if (state.currentPage === "live" || state.currentPage === "devices") {
      renderDevicesList();
    }
    if (data.status === "online") {
      if (data.device_id === state.selectedDeviceId) {
        pushStreamConfig(true);
        loadLatestScreenshotForDevice(data.device_id);
      }
      showToast(`${data.device_name} came online`, "success");
    } else {
      showToast(`${data.device_name} went offline`, "warning");
    }
  });

  state.socket.on("new_alert", (data) => {
    state.unreadAlerts++;
    updateAlertBadge();
    showToast(`Alert: ${data.message}`, data.type === "malware" ? "error" : "warning");
    playDashboardBuzzer(data.type === "malware" ? 4 : data.type === "dangerous_file" ? 3 : 2);
    if (state.currentPage === "alerts") loadAlerts();
  });

  state.socket.on("stream_config_applied", (data) => {
    if (!data) return;
    if ((data.delivered || 0) > 0) {
      showToast(`Stream settings applied to ${data.delivered} device(s)`, "success");
    } else {
      showToast("No target device received stream settings", "warning");
    }
  });
}

function updateConnectionStatus() {
  const el = document.querySelector(".connection-status");
  if (!el) return;
  if (state.connected) {
    el.className = "connection-status";
    el.innerHTML = '<span class="status-dot online"></span> Connected';
  } else {
    el.className = "connection-status disconnected";
    el.innerHTML = '<span class="status-dot offline"></span> Disconnected';
  }
}

function resolveImageSource(imagePayload) {
  if (!imagePayload || typeof imagePayload !== "string") return "";
  if (imagePayload.startsWith("data:image/")) return imagePayload;
  return `data:image/jpeg;base64,${imagePayload}`;
}

function updatePresentationOverlay(meta = null) {
  const viewer = document.querySelector(".screen-viewer");
  const overlay = document.getElementById("presentation-overlay");
  const showOverlay = state.presentationMode || Boolean(meta?.presentation);

  if (viewer) {
    viewer.classList.toggle("presentation-active", showOverlay);
  }
  if (!overlay) return;

  if (!showOverlay) {
    overlay.style.display = "none";
    return;
  }

  overlay.style.display = "grid";
  const verdict = (meta?.verdict || "MONITOR").toString().toUpperCase();
  const user = meta?.user || "Demo User";
  const focus = meta?.focus || "active session";
  const risk = Number.isFinite(meta?.riskScore) ? Math.max(0, Math.min(100, meta.riskScore)) : 42;

  const verdictEl = document.getElementById("presentation-verdict");
  const userEl = document.getElementById("presentation-user");
  const focusEl = document.getElementById("presentation-focus");
  const riskEl = document.getElementById("presentation-risk");

  if (verdictEl) {
    verdictEl.textContent = verdict;
    verdictEl.className = `demo-chip verdict-${verdict.toLowerCase()}`;
  }
  if (userEl) userEl.textContent = user;
  if (focusEl) focusEl.textContent = focus;
  if (riskEl) riskEl.textContent = `${risk}/100`;
}

function updateScreenView(payload) {
  const imagePayload = typeof payload === "string" ? payload : payload?.image;
  const meta = typeof payload === "object" ? payload?.meta : null;
  const img = document.getElementById("live-screen-img");
  const placeholder = document.getElementById("screen-placeholder");

  if (img && imagePayload) {
    const nextSrc = resolveImageSource(imagePayload);
    const token = ++state.frameRenderToken;
    const preload = new Image();
    preload.onload = () => {
      if (token !== state.frameRenderToken) return;
      img.src = nextSrc;
      img.style.display = "block";
      applyDisplaySettingsToView();
      if (placeholder) placeholder.style.display = "none";
      updatePresentationOverlay(meta);
    };
    preload.onerror = () => {
      if (token !== state.frameRenderToken) return;
      updatePresentationOverlay(null);
    };
    preload.src = nextSrc;
  } else {
    updatePresentationOverlay(null);
  }
}

function formatCpuTemp(value) {
  return Number.isFinite(value) ? `${value.toFixed(1)}°C` : "--";
}

function getSelectedDeviceTelemetry() {
  if (!state.selectedDeviceId) return null;
  return state.deviceTelemetry[state.selectedDeviceId] || null;
}

// ─── Data Loaders ───────────────────────────────────────────────────────────

async function loadDevices() {
  try {
    state.devices = (await api("/devices")) || [];
    ensureSelectedDeviceIsOnline();
  } catch (e) {
    console.error("Failed to load devices:", e);
  }
}

async function loadAlerts() {
  try {
    state.alerts = (await api("/alerts")) || [];
    const countData = await api("/alerts/unread-count");
    state.unreadAlerts = countData?.count || 0;
    updateAlertBadge();
    if (state.currentPage === "alerts") renderAlertsPage();
  } catch (e) {
    console.error("Failed to load alerts:", e);
  }
}

async function loadBlocklist() {
  try {
    state.blocklist = (await api("/blocklist")) || [];
    if (state.currentPage === "blocklist") renderBlocklistPage();
    if (state.currentPage === "rules") renderRulesPage();
  } catch (e) {
    console.error("Failed to load blocklist:", e);
  }
}

async function loadLatestScreenshotForDevice(deviceId) {
  if (!deviceId) return;
  try {
    const data = await api(`/screenshots/latest/${deviceId}`);
    if (data && data.image) {
      state.latestScreenshots[deviceId] = data;
      if (state.currentPage === "live" && state.selectedDeviceId === deviceId) {
        scheduleLiveFrameRender(data);
      }
    }
  } catch (_err) {
    // No frames yet is expected for newly connected devices.
  }
}

async function loadDeviceTelemetry(deviceId) {
  if (!deviceId) return;
  try {
    const data = await api(`/devices/${deviceId}/telemetry`);
    if (!data) return;
    state.deviceTelemetry[deviceId] = data;
    if (state.currentPage === "live") {
      renderDevicesList();
    }
  } catch (_err) {
    // Telemetry can be missing right after reconnect.
  }
}

function stopTelemetryPolling() {
  if (state.telemetryPollTimer) {
    clearInterval(state.telemetryPollTimer);
    state.telemetryPollTimer = null;
  }
}

function startTelemetryPolling() {
  stopTelemetryPolling();
  if (state.currentPage !== "live") return;

  state.telemetryPollTimer = setInterval(() => {
    loadDeviceTelemetry(state.selectedDeviceId);
  }, 10000);
}

function updateAlertBadge() {
  const badge = document.getElementById("alert-badge");
  if (badge) {
    badge.textContent = state.unreadAlerts;
    badge.style.display = state.unreadAlerts > 0 ? "block" : "none";
  }
}

// ─── Renderers ──────────────────────────────────────────────────────────────

function render() {
  const app = document.getElementById("app");

  if (state.controllerMode) {
    state.currentPage = "live";
  }

  if (!state.token) {
    if (state.streamTimer) {
      clearTimeout(state.streamTimer);
      state.streamTimer = null;
      state.pendingLiveFrame = null;
    }
    stopTelemetryPolling();
    app.innerHTML = renderLoginPage();
    attachLoginEvents();
    return;
  }

  app.innerHTML = renderDashboard();
  applyStreamProfile("p20");
  attachDashboardEvents();
  initSocket();
  loadDevices().then(() => {
    renderDevicesList();
    navigateTo(state.currentPage);
  });
  loadAlerts();
}

function renderLoginPage() {
  return `
    <div class="login-container">
      <div class="login-card">
        <div class="login-logo">
          <div class="shield-icon">🛡️</div>
          <h1>Guardian Shield</h1>
        </div>
        <p class="login-subtitle">Sign in to access your monitoring dashboard</p>
        <div id="login-error" class="login-error"></div>
        <form id="login-form">
          <div class="form-group">
            <label for="username">Username</label>
            <input id="username" class="form-input" type="text" placeholder="admin" autocomplete="username" required />
          </div>
          <div class="form-group">
            <label for="password">Password</label>
            <input id="password" class="form-input" type="password" placeholder="Enter password" autocomplete="current-password" required />
          </div>
          <button type="submit" class="btn btn-primary" id="login-btn">Sign In</button>
        </form>
      </div>
    </div>
  `;
}

function renderDashboard() {
  return `
    <div class="dashboard ${state.controllerMode ? "controller-mode" : ""}">
      <aside class="sidebar">
        <div class="sidebar-logo">
          <div class="shield-icon">🛡️</div>
          <span>Guardian Shield</span>
        </div>
        <nav class="sidebar-nav">
          <button class="nav-item active" data-page="live">
            <span class="icon">📺</span> Live View
          </button>
          <button class="nav-item" data-page="devices">
            <span class="icon">💻</span> Devices
          </button>
          <button class="nav-item" data-page="blocklist">
            <span class="icon">🚫</span> Blocklist
          </button>
          <button class="nav-item" data-page="rules">
            <span class="icon">⚖️</span> Rules & Limits
          </button>
          <button class="nav-item" data-page="alerts">
            <span class="icon">🔔</span> Alerts
            <span class="badge" id="alert-badge" style="display:none">0</span>
          </button>
        </nav>
        <div class="sidebar-footer">
          <button class="nav-item" id="logout-btn">
            <span class="icon">🚪</span> Logout
          </button>
        </div>
      </aside>

      <header class="topbar">
        <h2 class="topbar-title" id="page-title">Live View</h2>
        <div class="topbar-right">
          <div class="connection-status">
            <span class="status-dot online"></span> Connecting...
          </div>
        </div>
      </header>

      <main class="main-content">
        <!-- Live View Page -->
        <div class="page active" id="page-live">
          <div class="stats-grid" id="stats-grid"></div>
          <div class="card stream-controls-card">
            <div class="card-header">
              <div class="card-title">🎛️ Live Stream Tuning</div>
              <div class="stream-actions">
                <button class="btn btn-secondary btn-sm" id="open-output-controller-btn">Open Output Controller Window</button>
                <button class="btn btn-secondary btn-sm" id="apply-stream-config-btn">Apply to Selected Device</button>
              </div>
            </div>
            <div class="stream-profiles">
              <button class="btn btn-secondary btn-sm" data-stream-profile="p20">Smooth 20 FPS</button>
              <button class="btn btn-secondary btn-sm" data-stream-profile="p24">Fast 24 FPS</button>
              <button class="btn btn-secondary btn-sm" data-stream-profile="p30">Ultra 30 FPS</button>
            </div>
            <div class="stream-grid">
              <div class="stream-control">
                <label for="stream-fps">FPS</label>
                <input id="stream-fps" type="range" min="10" max="30" step="1" value="20" />
                <div class="stream-value" id="stream-fps-value">20 FPS</div>
              </div>
              <div class="stream-control">
                <label for="stream-quality">Quality</label>
                <input id="stream-quality" type="range" min="20" max="80" step="1" value="50" />
                <div class="stream-value" id="stream-quality-value">50%</div>
              </div>
              <div class="stream-control">
                <label for="stream-width">Resolution Width</label>
                <select id="stream-width" class="form-input">
                  <option value="960">960 px</option>
                  <option value="1280" selected>1280 px</option>
                  <option value="1600">1600 px</option>
                  <option value="1920">1920 px</option>
                </select>
                <div class="stream-value">Higher width = sharper but slower</div>
              </div>
            </div>

            <div class="display-controls-title">Display Output Controls</div>
            <div class="stream-grid output-grid">
              <div class="stream-control">
                <label for="display-fit-mode">Fit Mode</label>
                <select id="display-fit-mode" class="form-input">
                  <option value="contain">Contain (full frame)</option>
                  <option value="cover">Cover (fill panel)</option>
                </select>
                <div class="stream-value">How video fills dashboard viewer</div>
              </div>
              <div class="stream-control">
                <label for="display-zoom">Zoom</label>
                <input id="display-zoom" type="range" min="80" max="150" step="1" value="100" />
                <div class="stream-value" id="display-zoom-value">100%</div>
              </div>
              <div class="stream-control">
                <label for="display-brightness">Brightness</label>
                <input id="display-brightness" type="range" min="70" max="140" step="1" value="100" />
                <div class="stream-value" id="display-brightness-value">100%</div>
              </div>
              <div class="stream-control">
                <label for="display-contrast">Contrast</label>
                <input id="display-contrast" type="range" min="70" max="140" step="1" value="100" />
                <div class="stream-value" id="display-contrast-value">100%</div>
              </div>
            </div>
          </div>
          <div class="live-view-container">
            <div class="screen-viewer">
              <img id="live-screen-img" style="display:none" alt="Live screen" />
              <div id="screen-placeholder" class="screen-placeholder">
                <div class="icon">📺</div>
                <p>Select a device to view its screen</p>
              </div>
              <div class="presentation-overlay" id="presentation-overlay" style="display:none">
                <div class="demo-chip" id="presentation-verdict">MONITOR</div>
                <div class="demo-details">
                  <div><span>Tracked User</span><strong id="presentation-user">Demo User</strong></div>
                  <div><span>Current Focus</span><strong id="presentation-focus">active session</strong></div>
                  <div><span>Risk Score</span><strong id="presentation-risk">42/100</strong></div>
                </div>
              </div>
              <div class="live-badge" id="live-badge" style="display:none">
                <div class="pulse"></div> LIVE
              </div>
            </div>
            <div class="device-panel">
              <div class="card-title">📡 Connected Devices</div>
              <div id="device-list"></div>
            </div>
          </div>
        </div>

        <!-- Devices Page -->
        <div class="page" id="page-devices">
          <div class="card">
            <div class="card-header">
              <div class="card-title">💻 All Devices</div>
            </div>
            <div id="devices-table-wrap"></div>
          </div>
        </div>

        <!-- Blocklist Page -->
        <div class="page" id="page-blocklist">
          <div class="card">
            <div class="card-header">
              <div class="card-title">🚫 Blocked Websites</div>
            </div>
            <div class="blocklist-header">
              <input class="form-input" id="new-domain-input" type="text" placeholder="Enter domain to block (e.g. example.com)" />
              <button class="btn btn-primary" id="add-domain-btn" style="width:auto">+ Block Domain</button>
            </div>
            <div id="blocklist-table-wrap"></div>
          </div>
        </div>

        <!-- Rules & Limits Page -->
        <div class="page" id="page-rules">
          <div class="rules-shell">
            <section class="rules-top-grid">
              <div class="rules-card">
                <div class="rules-title-row">
                  <div>
                    <h3>Content Categories</h3>
                    <p>Automatically block access to broad categories of content.</p>
                  </div>
                  <span class="rules-pill">Global Protection Active</span>
                </div>
                <div class="rules-category-grid">
                  <div class="rules-category-item">
                    <div>
                      <strong>Adult Content</strong>
                      <small>Pornography and explicit media</small>
                    </div>
                    <label class="rules-switch"><input type="checkbox" checked /><span></span></label>
                  </div>
                  <div class="rules-category-item">
                    <div>
                      <strong>Online Gaming</strong>
                      <small>Multiplayer and gambling sites</small>
                    </div>
                    <label class="rules-switch"><input type="checkbox" /><span></span></label>
                  </div>
                  <div class="rules-category-item">
                    <div>
                      <strong>Social Media</strong>
                      <small>Networking and chat platforms</small>
                    </div>
                    <label class="rules-switch"><input type="checkbox" checked /><span></span></label>
                  </div>
                  <div class="rules-category-item">
                    <div>
                      <strong>E-commerce</strong>
                      <small>Online stores and marketplaces</small>
                    </div>
                    <label class="rules-switch"><input type="checkbox" /><span></span></label>
                  </div>
                </div>
              </div>
              <div class="rules-card rules-core-card">
                <h3>Secure Core</h3>
                <p>AI-driven filtering engine analyzes URLs in real-time to prevent access to harmful content.</p>
                <ul>
                  <li>Real-time DB updates</li>
                  <li>Encrypted DNS enabled</li>
                </ul>
                <button class="btn rules-core-btn" type="button">Configure Engine</button>
              </div>
            </section>

            <section class="rules-card">
              <div class="rules-title-row">
                <div>
                  <h3>Custom URL Blocklist</h3>
                  <p>Specify individual websites you want to restrict.</p>
                </div>
                <button class="btn rules-add-btn" id="rules-open-add-modal-btn" type="button">+ Add Custom URL</button>
              </div>
              <div class="rules-meta-line" id="rules-total-count">0 domains</div>
              <div id="rules-blocklist-list"></div>
            </section>

            <section class="rules-bottom-grid">
              <div class="rules-card">
                <h4>Smart Suggestions</h4>
                <p>Based on recent attempts, consider blocking frequently visited new domains.</p>
                <div class="rules-inline-actions">
                  <button class="btn btn-secondary btn-sm" type="button">Dismiss</button>
                </div>
              </div>
              <div class="rules-card">
                <h4>Scheduled Filters</h4>
                <p>Strict mode can run during selected hours to enforce stronger policies automatically.</p>
              </div>
            </section>
          </div>

          <div class="modal-overlay" id="rules-add-url-modal">
            <div class="modal rules-modal">
              <div class="modal-title">Add Custom URL Rule</div>
              <div class="form-group">
                <label for="rules-new-domain-input">Website Address</label>
                <input id="rules-new-domain-input" class="form-input" type="text" placeholder="e.g. example.com" />
              </div>
              <div class="modal-actions">
                <button class="btn btn-secondary" id="rules-cancel-add-btn" type="button">Cancel</button>
                <button class="btn btn-primary rules-save-btn" id="rules-save-domain-btn" type="button">Save Rule</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Alerts Page -->
        <div class="page" id="page-alerts">
          <div class="card">
            <div class="card-header">
              <div class="card-title">🔔 Alerts</div>
              <button class="btn btn-secondary btn-sm" id="mark-all-read-btn">Mark all read</button>
            </div>
            <div id="alerts-list"></div>
          </div>
        </div>
      </main>
    </div>
  `;
}

function renderDevicesList() {
  const container = document.getElementById("device-list");
  if (!container) return;

  if (state.devices.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="icon">📡</div>
        <h3>No devices connected</h3>
        <p>Start the Guardian Shield agent on a device to begin monitoring.</p>
      </div>
    `;
    return;
  }

  container.innerHTML = state.devices
    .map(
      (d) => `
    <div class="device-list-item ${d.id === state.selectedDeviceId ? "selected" : ""}"
         data-device-id="${d.id}">
      <span class="status-dot ${d.status}"></span>
      <div class="device-info">
        <div class="device-name">${esc(d.device_name)}</div>
        <div class="device-meta">${esc(d.platform || "Unknown")} · ${d.status}</div>
      </div>
    </div>
  `
    )
    .join("");

  // Stats
  const statsGrid = document.getElementById("stats-grid");
  if (statsGrid) {
    const online = state.devices.filter((d) => d.status === "online").length;
    const telemetry = getSelectedDeviceTelemetry();
    const cpuTemp = Number.parseFloat(telemetry?.cpu_temp_c);
    const cpuTempLabel = formatCpuTemp(cpuTemp);
    const cpuStatus = Number.isFinite(cpuTemp)
      ? cpuTemp >= 80
        ? "High"
        : cpuTemp >= 70
          ? "Warm"
          : "Normal"
      : "Waiting...";

    statsGrid.innerHTML = `
      <div class="stat-card">
        <div class="stat-icon blue">💻</div>
        <div>
          <div class="stat-value">${state.devices.length}</div>
          <div class="stat-label">Total Devices</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon green">🟢</div>
        <div>
          <div class="stat-value">${online}</div>
          <div class="stat-label">Online Now</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon red">🔔</div>
        <div>
          <div class="stat-value">${state.unreadAlerts}</div>
          <div class="stat-label">Unread Alerts</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon orange">🔥</div>
        <div>
          <div class="stat-value">${cpuTempLabel}</div>
          <div class="stat-label">How Hot Is CPU · ${cpuStatus}</div>
        </div>
      </div>
    `;
  }

  // Devices table for devices page
  const tableWrap = document.getElementById("devices-table-wrap");
  if (tableWrap) {
    if (state.devices.length === 0) {
      tableWrap.innerHTML = '<div class="empty-state"><p>No devices registered</p></div>';
    } else {
      tableWrap.innerHTML = `
        <table class="domain-table">
          <thead><tr>
            <th>Status</th><th>Device Name</th><th>Platform</th><th>Last Seen</th><th>Actions</th>
          </tr></thead>
          <tbody>
            ${state.devices
              .map(
                (d) => `
              <tr>
                <td><span class="status-dot ${d.status}"></span> ${d.status}</td>
                <td>${esc(d.device_name)}</td>
                <td>${esc(d.platform || "—")}</td>
                <td>${d.last_seen ? timeAgo(d.last_seen) : "Never"}</td>
                <td><button class="btn btn-danger btn-sm" data-delete-device="${d.id}">Remove</button></td>
              </tr>
            `
              )
              .join("")}
          </tbody>
        </table>
      `;
    }
  }
}

function renderBlocklistPage() {
  const wrap = document.getElementById("blocklist-table-wrap");
  if (!wrap) return;

  if (state.blocklist.length === 0) {
    wrap.innerHTML = `
      <div class="empty-state">
        <div class="icon">🚫</div>
        <h3>No custom blocked domains</h3>
        <p>The pre-built blocklist is active with 200K+ domains. Add custom domains above.</p>
      </div>
    `;
    return;
  }

  wrap.innerHTML = `
    <table class="domain-table">
      <thead><tr><th>Domain</th><th>Category</th><th>Added</th><th>Actions</th></tr></thead>
      <tbody>
        ${state.blocklist
          .map(
            (b) => `
          <tr>
            <td>${esc(b.domain)}</td>
            <td><span class="category-badge ${b.category}">${b.category}</span></td>
            <td>${timeAgo(b.created_at)}</td>
            <td><button class="btn btn-danger btn-sm" data-unblock="${b.id}">Unblock</button></td>
          </tr>
        `
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderRulesPage() {
  const list = document.getElementById("rules-blocklist-list");
  const count = document.getElementById("rules-total-count");
  if (!list) return;

  if (count) {
    count.textContent = `${state.blocklist.length} domain${state.blocklist.length === 1 ? "" : "s"}`;
  }

  if (state.blocklist.length === 0) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="icon">🌐</div>
        <h3>No custom URL rules yet</h3>
        <p>Add a domain to start protecting this device instantly.</p>
      </div>
    `;
    return;
  }

  list.innerHTML = state.blocklist
    .slice(0, 20)
    .map((entry) => {
      const added = entry.created_at
        ? new Date(entry.created_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "2-digit" })
        : "Unknown";

      return `
        <div class="rules-domain-row">
          <div class="rules-domain-main">
            <div class="rules-domain-name">${esc(entry.domain)}</div>
            <div class="rules-domain-date">Added ${esc(added)}</div>
          </div>
          <div class="rules-domain-side">
            <span class="rules-status-badge blocked">Blocked</span>
            <button class="btn btn-danger btn-sm" data-unblock="${entry.id}">Remove</button>
          </div>
        </div>
      `;
    })
    .join("");
}

function renderAlertsPage() {
  const list = document.getElementById("alerts-list");
  if (!list) return;

  if (state.alerts.length === 0) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="icon">✅</div>
        <h3>No alerts</h3>
        <p>Everything looks good! Alerts will appear here when issues are detected.</p>
      </div>
    `;
    return;
  }

  list.innerHTML = state.alerts
    .map(
      (a) => `
    <div class="alert-item ${a.is_read ? "" : "unread"}">
      <div class="alert-icon ${a.alert_type === "malware" ? "malware" : a.alert_type === "dangerous_file" ? "warning" : "info"}">
        ${a.alert_type === "malware" ? "🦠" : a.alert_type === "dangerous_file" ? "⚠️" : "ℹ️"}
      </div>
      <div class="alert-content">
        <div class="alert-title">${esc(a.message)}</div>
        <div class="alert-message">${esc(a.device_name || "Unknown device")}</div>
        <div class="alert-time">${timeAgo(a.created_at)}</div>
      </div>
    </div>
  `
    )
    .join("");
}

// ─── Navigation ─────────────────────────────────────────────────────────────

function navigateTo(page) {
  state.currentPage = page;

  // Update nav items
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === page);
  });

  // Update pages
  document.querySelectorAll(".page").forEach((el) => {
    el.classList.toggle("active", el.id === `page-${page}`);
  });

  // Update title
  const titles = { live: "Live View", devices: "Devices", blocklist: "Blocklist", rules: "Rules & Limits", alerts: "Alerts" };
  const titleEl = document.getElementById("page-title");
  if (titleEl) {
    titleEl.textContent = state.controllerMode && page === "live"
      ? "Output Controller"
      : (titles[page] || page);
  }

  // Load data for the page
  if (page === "blocklist") loadBlocklist();
  if (page === "rules") {
    renderRulesPage();
    loadBlocklist();
  }
  if (page === "alerts") loadAlerts();
  if (page === "devices") renderDevicesList();
  if (page === "live") {
    pushStreamConfig(true);
    loadDeviceTelemetry(state.selectedDeviceId);
    startTelemetryPolling();
    renderDevicesList();
    // Prefer real-time frames; don't show stale screenshots from old sessions.
    const cached = state.latestScreenshots[state.selectedDeviceId];
    if (cached && isFreshFrame(cached)) scheduleLiveFrameRender(cached);
  } else {
    stopTelemetryPolling();
  }
}

// ─── Event Handlers ─────────────────────────────────────────────────────────

function attachLoginEvents() {
  const form = document.getElementById("login-form");
  if (!form) return;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = document.getElementById("username").value;
    const password = document.getElementById("password").value;
    const errorEl = document.getElementById("login-error");
    const btn = document.getElementById("login-btn");

    btn.disabled = true;
    btn.innerHTML = '<div class="spinner"></div>';

    try {
      const data = await api("/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      if (data && data.token) {
        state.token = data.token;
        localStorage.setItem("gs_token", data.token);
        render();
      } else {
        errorEl.textContent = "Invalid credentials";
        errorEl.classList.add("visible");
      }
    } catch (err) {
      errorEl.textContent = err.message || "Login failed";
      errorEl.classList.add("visible");
    } finally {
      btn.disabled = false;
      btn.textContent = "Sign In";
    }
  });
}

function attachDashboardEvents() {
  // Navigation
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    el.addEventListener("click", () => navigateTo(el.dataset.page));
  });

  // Logout
  document.getElementById("logout-btn")?.addEventListener("click", () => {
    state.token = null;
    localStorage.removeItem("gs_token");
    if (state.socket) state.socket.disconnect();
    stopTelemetryPolling();
    if (state.streamTimer) {
      clearTimeout(state.streamTimer);
      state.streamTimer = null;
      state.pendingLiveFrame = null;
    }
    render();
  });

  // Device selection (delegated)
  document.addEventListener("click", (e) => {
    const deviceItem = e.target.closest(".device-list-item");
    if (deviceItem) {
      const id = parseInt(deviceItem.dataset.deviceId);
      ensureSelectedDeviceIsOnline(id);
      renderDevicesList();
      pushStreamConfig(true);
      loadDeviceTelemetry(state.selectedDeviceId);

      // Show live badge
      const liveBadge = document.getElementById("live-badge");
      if (liveBadge) liveBadge.style.display = "flex";

      // Show cached screenshot or placeholder
      const cached = state.latestScreenshots[state.selectedDeviceId];
      if (cached && isFreshFrame(cached)) {
        scheduleLiveFrameRender(cached);
      } else {
        const img = document.getElementById("live-screen-img");
        const ph = document.getElementById("screen-placeholder");
        if (img) img.style.display = "none";
        if (ph) {
          ph.style.display = "block";
          ph.innerHTML = '<div class="icon">⏳</div><p>Waiting for screenshots from this device...</p>';
        }
        updatePresentationOverlay(null);
      }
    }

    // Delete device
    const deleteBtn = e.target.closest("[data-delete-device]");
    if (deleteBtn) {
      const id = deleteBtn.dataset.deleteDevice;
      if (confirm("Remove this device? Its screenshot history will be deleted.")) {
        api(`/devices/${id}`, { method: "DELETE" })
          .then(() => {
            showToast("Device removed", "success");
            loadDevices().then(renderDevicesList);
          })
          .catch((err) => showToast(err.message, "error"));
      }
    }

    // Unblock domain
    const unblockBtn = e.target.closest("[data-unblock]");
    if (unblockBtn) {
      const id = unblockBtn.dataset.unblock;
      api(`/blocklist/${id}`, { method: "DELETE" })
        .then(() => {
          showToast("Domain unblocked", "success");
          loadBlocklist();
        })
        .catch((err) => showToast(err.message, "error"));
    }
  });

  // Add domain to blocklist
  document.getElementById("add-domain-btn")?.addEventListener("click", () => {
    const input = document.getElementById("new-domain-input");
    const domain = normalizeDomainInput(input?.value);
    if (!domain) return;

    api("/blocklist", {
      method: "POST",
      body: JSON.stringify({ domain }),
    })
      .then(() => {
        showToast(`Blocked: ${domain}`, "success");
        input.value = "";
        loadBlocklist();
      })
      .catch((err) => showToast(err.message, "error"));
  });

  // Enter key on domain input
  document.getElementById("new-domain-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("add-domain-btn")?.click();
  });

  // Rules page modal actions
  const rulesModal = document.getElementById("rules-add-url-modal");
  const rulesInput = document.getElementById("rules-new-domain-input");
  const openRulesModal = () => {
    if (!rulesModal) return;
    rulesModal.classList.add("visible");
    rulesInput?.focus();
  };
  const closeRulesModal = () => {
    rulesModal?.classList.remove("visible");
  };

  document.getElementById("rules-open-add-modal-btn")?.addEventListener("click", openRulesModal);
  document.getElementById("rules-cancel-add-btn")?.addEventListener("click", closeRulesModal);
  rulesModal?.addEventListener("click", (e) => {
    if (e.target === rulesModal) closeRulesModal();
  });

  document.getElementById("rules-save-domain-btn")?.addEventListener("click", () => {
    const domain = normalizeDomainInput(rulesInput?.value);
    if (!domain) return;

    api("/blocklist", {
      method: "POST",
      body: JSON.stringify({ domain }),
    })
      .then(() => {
        showToast(`Blocked: ${domain}`, "success");
        if (rulesInput) rulesInput.value = "";
        closeRulesModal();
        loadBlocklist();
      })
      .catch((err) => showToast(err.message, "error"));
  });

  rulesInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      document.getElementById("rules-save-domain-btn")?.click();
    }
  });

  // Mark all alerts read
  document.getElementById("mark-all-read-btn")?.addEventListener("click", () => {
    api("/alerts/read-all", { method: "PUT" })
      .then(() => {
        state.unreadAlerts = 0;
        updateAlertBadge();
        loadAlerts();
        showToast("All alerts marked as read", "success");
      })
      .catch((err) => showToast(err.message, "error"));
  });

  // Stream tuning controls
  syncStreamControlsUI();
  syncDisplayControlsUI();
  applyDisplaySettingsToView();

  document.getElementById("open-output-controller-btn")?.addEventListener("click", () => {
    openOutputControllerWindow();
  });

  document.querySelectorAll("[data-stream-profile]").forEach((el) => {
    el.addEventListener("click", () => {
      applyStreamProfile(el.dataset.streamProfile);
      pushStreamConfig();
    });
  });

  document.getElementById("stream-fps")?.addEventListener("input", (e) => {
    state.streamConfig.target_fps = parseInt(e.target.value, 10);
    state.streamTargetFps = state.streamConfig.target_fps;
    syncStreamControlsUI();
  });

  document.getElementById("stream-quality")?.addEventListener("input", (e) => {
    state.streamConfig.screenshot_quality = parseInt(e.target.value, 10);
    syncStreamControlsUI();
  });

  document.getElementById("stream-width")?.addEventListener("change", (e) => {
    state.streamConfig.max_width = parseInt(e.target.value, 10);
    syncStreamControlsUI();
  });

  document.getElementById("apply-stream-config-btn")?.addEventListener("click", () => {
    pushStreamConfig();
  });

  // Display output controls
  document.getElementById("display-fit-mode")?.addEventListener("change", (e) => {
    state.displaySettings.fit_mode = e.target.value === "cover" ? "cover" : "contain";
    persistDisplaySettings();
  });

  document.getElementById("display-zoom")?.addEventListener("input", (e) => {
    state.displaySettings.zoom = parseInt(e.target.value, 10);
    persistDisplaySettings();
  });

  document.getElementById("display-brightness")?.addEventListener("input", (e) => {
    state.displaySettings.brightness = parseInt(e.target.value, 10);
    persistDisplaySettings();
  });

  document.getElementById("display-contrast")?.addEventListener("input", (e) => {
    state.displaySettings.contrast = parseInt(e.target.value, 10);
    persistDisplaySettings();
  });
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function esc(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

function timeAgo(dateStr) {
  if (!dateStr) return "—";
  const date = new Date(dateStr);
  const now = new Date();
  const seconds = Math.floor((now - date) / 1000);

  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

// ─── Init ───────────────────────────────────────────────────────────────────

window.addEventListener("storage", (event) => {
  if (event.key !== DISPLAY_SETTINGS_KEY || !event.newValue) return;
  try {
    const parsed = JSON.parse(event.newValue);
    state.displaySettings = {
      fit_mode: parsed.fit_mode === "cover" ? "cover" : "contain",
      zoom: Math.max(80, Math.min(150, parseInt(parsed.zoom || 100, 10))),
      brightness: Math.max(70, Math.min(140, parseInt(parsed.brightness || 100, 10))),
      contrast: Math.max(70, Math.min(140, parseInt(parsed.contrast || 100, 10))),
    };
    syncDisplayControlsUI();
    applyDisplaySettingsToView();
  } catch (_err) {
    // Ignore malformed storage payload.
  }
});

document.addEventListener("pointerdown", unlockDashboardAudio, { once: true });
document.addEventListener("keydown", unlockDashboardAudio, { once: true });
document.addEventListener("DOMContentLoaded", render);
