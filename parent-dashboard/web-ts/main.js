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
  // Highest frame token actually painted. Used to decide whether a decode that
  // just finished is still newer than what is on screen.
  lastAppliedFrameToken: 0,
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
  appsData: {
    activeApp: null,
    runningApps: [],
    installedApps: [],
    blockedApps: [],
    activeTimers: [],
    messages: [],
    selectedTab: "running",
    runningSearch: "",
    installedSearch: "",
    installedCategory: "all",
  },
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

// A Raspberry Pi captures through grim/scrot, which writes a PNG to disk and
// re-encodes it for every single frame. It cannot sustain the Windows rate, and
// pushing 20 fps at it just queues frames until the socket stalls. Automatic
// pushes are therefore capped; an explicit Apply from the stream settings panel
// still wins, so this stays overridable.
const LOW_POWER_AUTO_FPS = 5;

function isLowPowerDevice(device) {
  const p = String(device?.platform || "").toLowerCase();
  return p.includes("linux") || p.includes("arm") || p.includes("raspberry");
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

  const config = { ...state.streamConfig };
  if (silent && isLowPowerDevice(selected)) {
    config.target_fps = Math.min(config.target_fps, LOW_POWER_AUTO_FPS);
  }

  state.streamTargetFps = config.target_fps;
  state.socket.emit("stream_config", {
    device_id: state.selectedDeviceId,
    config,
  });
}

// ─── Socket.IO Setup ────────────────────────────────────────────────────────

function initSocket() {
  if (state.socket) state.socket.disconnect();

  // BUG-12: the server only forwards device data (screenshots, telemetry, app
  // reports) to sockets that present a valid parent token, so send it with the
  // handshake. initSocket() only runs once state.token exists.
  state.socket = io({
    transports: ["websocket", "polling"],
    auth: { token: state.token },
  });

  state.socket.on("connect", () => {
    state.connected = true;
    // Re-assert credentials on every (re)connect: after a token refresh the
    // handshake payload can be stale.
    state.socket.emit("dashboard_auth", { token: state.token });
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
    } else if (data.error === "Unauthorized") {
      // Not a device problem: this socket is not a recognised viewer, so the server
      // also will not send it live_screenshot frames. Almost always a stale
      // web-ts/dist bundle that predates the socket auth handshake (BUG-29) — or a
      // session whose token expired.
      showToast("This dashboard is not authorized for live streaming — rebuild the UI " +
                "(GUARD.bat build) or log in again", "error");
    } else {
      showToast("No target device received stream settings", "warning");
    }
  });

  // ─── Application Tracking Real-Time Events ────────────────────────────────
  state.socket.on("running_apps_updated", (data) => {
    if (data.device_id === state.selectedDeviceId) {
      state.appsData.activeApp = data.active_app || null;
      state.appsData.runningApps = data.running_apps || [];
      state.appsData.activeTimers = data.active_timers || [];
      updateLiveViewActiveAppPill(data.active_app);
      if (state.currentPage === "apps") {
        renderAppsPage();
      }
    }
  });

  state.socket.on("installed_apps_updated", (data) => {
    if (data.device_id === state.selectedDeviceId) {
      if (state.currentPage === "apps" && state.appsData.selectedTab === "installed") {
        loadInstalledApps();
      }
    }
  });

  state.socket.on("app_timer_changed", (data) => {
    if (data.device_id === state.selectedDeviceId) {
      loadAppsOverview();
    }
  });

  state.socket.on("child_message_status", (data) => {
    if (data.device_id === state.selectedDeviceId) {
      showToast(`Child received message: "${data.title}"`, "success");
      loadChildMessages();
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
      // Paint any frame newer than what is already on screen.
      //
      // The old test was `token !== state.frameRenderToken`, i.e. "discard this
      // decode if a newer frame has even STARTED decoding". Once decode time
      // exceeded the frame interval every frame lost that race, so nothing after
      // the first frame was ever painted and the live view looked like a static
      // image. Comparing against the last painted token instead means a frame is
      // only dropped when a strictly newer one has already been shown.
      if (token <= state.lastAppliedFrameToken) return;
      state.lastAppliedFrameToken = token;
      img.src = nextSrc;
      img.style.display = "block";
      applyDisplaySettingsToView();
      if (placeholder) placeholder.style.display = "none";
      updatePresentationOverlay(meta);
    };
    preload.onerror = () => {
      if (token <= state.lastAppliedFrameToken) return;
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

// ─── Application Tracking Data Loaders & Renderers ──────────────────────────

async function loadAppsOverview(deviceId = state.selectedDeviceId) {
  if (!deviceId) return;
  try {
    const data = await api(`/apps/overview/${deviceId}`);
    if (!data) return;
    state.appsData.activeApp = data.active_app || null;
    state.appsData.runningApps = data.running_apps || [];
    state.appsData.activeTimers = data.active_timers || [];
    state.appsData.blockedApps = data.blocked_apps || [];
    updateLiveViewActiveAppPill(data.active_app);
    if (state.currentPage === "apps") {
      renderAppsPage();
    }
  } catch (err) {
    console.debug("Failed to load apps overview:", err);
  }
}

async function loadInstalledApps(deviceId = state.selectedDeviceId) {
  if (!deviceId) return;
  try {
    const data = await api(`/apps/installed/${deviceId}`);
    state.appsData.installedApps = data || [];
    if (state.currentPage === "apps" && state.appsData.selectedTab === "installed") {
      renderInstalledAppsGrid();
    }
  } catch (err) {
    console.debug("Failed to load installed apps:", err);
  }
}

async function loadChildMessages(deviceId = state.selectedDeviceId) {
  if (!deviceId) return;
  try {
    const data = await api(`/apps/messages/${deviceId}`);
    state.appsData.messages = data || [];
    if (state.currentPage === "apps" && state.appsData.selectedTab === "messages") {
      renderChildMessagesList();
    }
  } catch (err) {
    console.debug("Failed to load messages:", err);
  }
}

function updateLiveViewActiveAppPill(activeApp) {
  const pill = document.getElementById("live-active-app-pill");
  const nameEl = document.getElementById("live-active-app-name");
  if (!pill || !nameEl) return;

  if (!activeApp || !activeApp.app_name || activeApp.app_name.includes("Desktop / System")) {
    pill.style.display = "none";
    return;
  }

  pill.style.display = "inline-flex";
  nameEl.textContent = `🎯 ${activeApp.app_name}${activeApp.window_title ? ` — ${activeApp.window_title.slice(0, 30)}` : ""}`;
}

function renderAppsPage() {
  renderActiveAppHero();
  renderActiveTimersStrip();

  // Update tab counts
  const runCount = document.getElementById("running-apps-count");
  if (runCount) runCount.textContent = state.appsData.runningApps.length;

  const instCount = document.getElementById("installed-apps-count");
  if (instCount) instCount.textContent = state.appsData.installedApps.length;

  const blkCount = document.getElementById("blocked-apps-count");
  if (blkCount) blkCount.textContent = state.appsData.blockedApps.length;

  const msgCount = document.getElementById("messages-count");
  if (msgCount) msgCount.textContent = state.appsData.messages.length;

  if (state.appsData.selectedTab === "running") renderRunningAppsTable();
  else if (state.appsData.selectedTab === "installed") renderInstalledAppsGrid();
  else if (state.appsData.selectedTab === "blocked") renderBlockedAppsList();
  else if (state.appsData.selectedTab === "messages") renderChildMessagesList();
}

function renderActiveAppHero() {
  const hero = document.getElementById("apps-hero-card");
  if (!hero) return;

  const active = state.appsData.activeApp;
  const name = active?.app_name || "Desktop / System";
  const proc = active?.process_name || "explorer.exe";
  const title = active?.window_title || "Monitoring desktop session...";
  const pid = active?.pid || 0;
  const ram = active?.memory_mb ? `${active.memory_mb} MB` : "--";
  const durSec = active?.duration_sec || 0;
  const durStr = durSec > 60 ? `${Math.floor(durSec / 60)}m ${durSec % 60}s` : `${durSec}s`;

  let icon = "💻";
  if (name.toLowerCase().includes("discord") || name.toLowerCase().includes("chat")) icon = "💬";
  else if (name.toLowerCase().includes("chrome") || name.toLowerCase().includes("edge") || name.toLowerCase().includes("firefox")) icon = "🌐";
  else if (name.toLowerCase().includes("game") || name.toLowerCase().includes("roblox") || name.toLowerCase().includes("steam") || name.toLowerCase().includes("minecraft")) icon = "🎮";
  else if (name.toLowerCase().includes("code") || name.toLowerCase().includes("studio")) icon = "💻";
  else if (name.toLowerCase().includes("spotify") || name.toLowerCase().includes("vlc") || name.toLowerCase().includes("media")) icon = "🎬";

  const iconEl = document.getElementById("apps-hero-icon");
  const nameEl = document.getElementById("apps-hero-name");
  const titleEl = document.getElementById("apps-hero-title");
  const procEl = document.getElementById("apps-hero-proc");
  const durEl = document.getElementById("apps-hero-dur");
  const memEl = document.getElementById("apps-hero-mem");

  if (iconEl) iconEl.textContent = icon;
  if (nameEl) nameEl.innerHTML = `${esc(name)} <span class="app-badge general">Active Focus</span>`;
  if (titleEl) titleEl.textContent = title;
  if (procEl) procEl.textContent = `${proc}${pid ? ` (PID: ${pid})` : ""}`;
  if (durEl) durEl.textContent = durStr;
  if (memEl) memEl.textContent = ram;

  // Set data attributes on hero buttons for current active app
  const closeBtn = document.getElementById("hero-close-app-btn");
  const timerBtn = document.getElementById("hero-set-timer-btn");
  const blockBtn = document.getElementById("hero-block-app-btn");

  if (closeBtn) {
    closeBtn.dataset.pid = String(pid);
    closeBtn.dataset.proc = proc;
    closeBtn.dataset.appName = name;
  }
  if (timerBtn) {
    timerBtn.dataset.proc = proc;
    timerBtn.dataset.appName = name;
  }
  if (blockBtn) {
    blockBtn.dataset.proc = proc;
    blockBtn.dataset.appName = name;
  }
}

function renderActiveTimersStrip() {
  const strip = document.getElementById("apps-timers-strip");
  const grid = document.getElementById("apps-timers-grid");
  if (!strip || !grid) return;

  const timers = state.appsData.activeTimers || [];
  if (timers.length === 0) {
    strip.style.display = "none";
    return;
  }

  strip.style.display = "block";
  grid.innerHTML = timers
    .map((t) => {
      const rem = t.remaining_seconds != null ? t.remaining_seconds : Math.max(0, Math.round((t.expires_at - Date.now() / 1000)));
      const mins = Math.floor(rem / 60);
      const secs = rem % 60;
      const timeLabel = `${mins}m ${secs < 10 ? "0" : ""}${secs}s`;

      return `
        <div class="apps-timer-card">
          <div class="apps-timer-info">
            <div class="apps-timer-app">⏱️ ${esc(t.app_name || t.process_name)}</div>
            <div class="apps-timer-countdown">Closes in: <strong>${timeLabel}</strong></div>
          </div>
          <button class="btn btn-secondary btn-sm" data-cancel-timer="${esc(t.id || t.process_name)}">Cancel</button>
        </div>
      `;
    })
    .join("");
}

function renderRunningAppsTable() {
  const wrap = document.getElementById("running-apps-table-wrap");
  if (!wrap) return;

  let list = state.appsData.runningApps || [];
  const query = (state.appsData.runningSearch || "").toLowerCase().trim();

  if (query) {
    list = list.filter(
      (a) =>
        a.app_name.toLowerCase().includes(query) ||
        a.process_name.toLowerCase().includes(query) ||
        (a.username && a.username.toLowerCase().includes(query))
    );
  }

  if (list.length === 0) {
    wrap.innerHTML = `
      <div class="empty-state">
        <div class="icon">⚡</div>
        <h3>No running user processes matching search</h3>
        <p>Ensure the target device agent is online to view real-time applications.</p>
      </div>
    `;
    return;
  }

  wrap.innerHTML = `
    <table class="domain-table">
      <thead>
        <tr>
          <th>Application</th>
          <th>Process</th>
          <th>Memory</th>
          <th>PID</th>
          <th>Uptime</th>
          <th style="text-align: right;">Quick Actions</th>
        </tr>
      </thead>
      <tbody>
        ${list
          .map((a) => {
            const uptime = a.uptime_sec > 60 ? `${Math.floor(a.uptime_sec / 60)}m` : `${a.uptime_sec}s`;
            const isBlocked = a.is_blocked;

            return `
              <tr>
                <td>
                  <strong>${esc(a.app_name)}</strong>
                  ${isBlocked ? '<span class="app-badge games" style="margin-left:6px">Blocked</span>' : ""}
                </td>
                <td><code>${esc(a.process_name)}</code></td>
                <td><span style="color: var(--accent-cyan); font-weight:600;">${a.memory_mb} MB</span></td>
                <td><code>${a.pid}</code></td>
                <td>${uptime}</td>
                <td style="text-align: right;">
                  <div style="display: inline-flex; gap: 0.35rem; justify-content: flex-end;">
                    <button class="btn btn-secondary btn-sm" data-app-msg="${esc(a.app_name)}" title="Send message about this app">💬 Message</button>
                    <button class="btn btn-warning btn-sm" data-app-timer="${esc(a.process_name)}" data-app-name="${esc(a.app_name)}" title="Set time limit">⏱️ Timer</button>
                    <button class="btn btn-danger btn-sm" data-close-pid="${a.pid}" data-close-proc="${esc(a.process_name)}" data-app-name="${esc(a.app_name)}" title="Close app now anonymously">❌ Close</button>
                    ${
                      isBlocked
                        ? `<button class="btn btn-secondary btn-sm" data-unblock-proc="${esc(a.process_name)}">Unblock</button>`
                        : `<button class="btn btn-secondary btn-sm" data-block-proc="${esc(a.process_name)}" data-app-name="${esc(a.app_name)}" title="Add to blocklist">🚫 Block</button>`
                    }
                  </div>
                </td>
              </tr>
            `;
          })
          .join("")}
      </tbody>
    </table>
  `;
}

function renderInstalledAppsGrid() {
  const wrap = document.getElementById("installed-apps-grid-wrap");
  if (!wrap) return;

  let list = state.appsData.installedApps || [];
  const query = (state.appsData.installedSearch || "").toLowerCase().trim();
  const cat = state.appsData.installedCategory || "all";

  if (cat !== "all") {
    list = list.filter((a) => a.category === cat);
  }

  if (query) {
    list = list.filter(
      (a) =>
        a.app_name.toLowerCase().includes(query) ||
        (a.process_name && a.process_name.toLowerCase().includes(query))
    );
  }

  if (list.length === 0) {
    wrap.innerHTML = `
      <div class="empty-state">
        <div class="icon">📦</div>
        <h3>No installed applications found</h3>
        <p>Click "Re-scan Device" to discover all programs on the target device.</p>
      </div>
    `;
    return;
  }

  wrap.innerHTML = `
    <div class="installed-apps-grid">
      ${list
        .map((app) => {
          let icon = "📁";
          if (app.category === "Games") icon = "🎮";
          else if (app.category === "Social & Chat") icon = "💬";
          else if (app.category === "Web Browser") icon = "🌐";
          else if (app.category === "Media & Streaming") icon = "🎬";
          else if (app.category === "Development") icon = "💻";

          const isBlocked = app.is_blocked;

          return `
            <div class="installed-app-card">
              <div class="installed-app-info">
                <div class="installed-app-icon">${icon}</div>
                <div class="installed-app-text">
                  <div class="installed-app-name" title="${esc(app.app_name)}">${esc(app.app_name)}</div>
                  <div class="installed-app-cat">${esc(app.category)} · <code>${esc(app.process_name || "app")}</code></div>
                </div>
              </div>
              <div style="display: flex; gap: 0.35rem;">
                <button class="btn btn-warning btn-sm" data-app-timer="${esc(app.process_name)}" data-app-name="${esc(app.app_name)}" title="Set time limit">⏱️</button>
                ${
                  isBlocked
                    ? `<button class="btn btn-danger btn-sm" data-unblock-proc="${esc(app.process_name)}" title="Click to unblock">🚫 Blocked</button>`
                    : `<button class="btn btn-secondary btn-sm" data-block-proc="${esc(app.process_name)}" data-app-name="${esc(app.app_name)}" data-cat="${esc(app.category)}" title="Block this application">Block</button>`
                }
              </div>
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderBlockedAppsList() {
  const wrap = document.getElementById("blocked-apps-table-wrap");
  if (!wrap) return;

  const list = state.appsData.blockedApps || [];
  if (list.length === 0) {
    wrap.innerHTML = `
      <div class="empty-state">
        <div class="icon">🚫</div>
        <h3>No applications currently blocked</h3>
        <p>Add a process name or app above (e.g. discord.exe, roblox, steam) to prevent it from launching.</p>
      </div>
    `;
    return;
  }

  wrap.innerHTML = `
    <table class="domain-table">
      <thead>
        <tr>
          <th>Application Name</th>
          <th>Process Match Rule</th>
          <th>Category</th>
          <th>Added By</th>
          <th style="text-align: right;">Action</th>
        </tr>
      </thead>
      <tbody>
        ${list
          .map(
            (b) => `
            <tr>
              <td><strong>${esc(b.app_name)}</strong></td>
              <td><code>${esc(b.process_name)}</code></td>
              <td><span class="category-badge ${b.category.toLowerCase()}">${esc(b.category)}</span></td>
              <td>${esc(b.added_by || "admin")}</td>
              <td style="text-align: right;">
                <button class="btn btn-danger btn-sm" data-unblock-app-id="${b.id}">Remove Block</button>
              </td>
            </tr>
          `
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderChildMessagesList() {
  const wrap = document.getElementById("child-messages-table-wrap");
  if (!wrap) return;

  const list = state.appsData.messages || [];
  if (list.length === 0) {
    wrap.innerHTML = `
      <div class="empty-state">
        <div class="icon">💬</div>
        <h3>No message history yet</h3>
        <p>Send a quick reminder or alert directly to your child's screen using the button above.</p>
      </div>
    `;
    return;
  }

  wrap.innerHTML = `
    <table class="domain-table">
      <thead>
        <tr>
          <th>Status</th>
          <th>Title</th>
          <th>Message</th>
          <th>Urgency</th>
          <th>Sent At</th>
        </tr>
      </thead>
      <tbody>
        ${list
          .map(
            (m) => `
            <tr>
              <td>${m.delivered ? '<span style="color:var(--accent-green)">✅ Delivered</span>' : "⏳ Sent"}</td>
              <td><strong>${esc(m.title)}</strong></td>
              <td>${esc(m.message)}</td>
              <td><span class="app-badge ${m.urgency === "urgent" ? "games" : m.urgency === "warning" ? "media" : "browser"}">${esc(m.urgency.toUpperCase())}</span></td>
              <td>${timeAgo(m.created_at)}</td>
            </tr>
          `
          )
          .join("")}
      </tbody>
    </table>
  `;
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
          <button class="nav-item" data-page="apps">
            <span class="icon">🎮</span> Apps
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
              <div class="live-active-app-pill" id="live-active-app-pill" style="display:none">
                <span class="pulse-dot"></span>
                <span id="live-active-app-name">Active App</span>
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

        <!-- Apps Page -->
        <div class="page" id="page-apps">
          <div class="apps-container">
            <!-- Active Foreground App Hero Banner -->
            <div class="apps-hero-card" id="apps-hero-card">
              <div class="apps-hero-header">
                <div class="apps-hero-info">
                  <div class="apps-hero-icon" id="apps-hero-icon">💻</div>
                  <div class="apps-hero-title-group">
                    <h3 id="apps-hero-name">Desktop / System <span class="app-badge general">Active Focus</span></h3>
                    <div class="apps-hero-window-title" id="apps-hero-title">Monitoring active desktop session...</div>
                    <div class="apps-hero-meta" id="apps-hero-meta">
                      <span>⚙️ Process: <strong id="apps-hero-proc">explorer.exe</strong></span>
                      <span>⏱️ Active: <strong id="apps-hero-dur">0m</strong></span>
                      <span>💾 RAM: <strong id="apps-hero-mem">--</strong></span>
                    </div>
                  </div>
                </div>
                <div class="apps-hero-actions" id="apps-hero-actions">
                  <button class="btn btn-danger btn-sm" id="hero-close-app-btn">❌ Close App Now</button>
                  <button class="btn btn-warning btn-sm" id="hero-set-timer-btn">⏱️ Set App Limit</button>
                  <button class="btn btn-primary btn-sm" id="hero-send-msg-btn">💬 Send Message</button>
                  <button class="btn btn-secondary btn-sm" id="hero-block-app-btn">🚫 Block App</button>
                </div>
              </div>
            </div>

            <!-- Active Timers Countdown Strip -->
            <div class="apps-timers-section" id="apps-timers-strip" style="display:none">
              <div class="apps-timers-title">⏳ Active App Time Limits</div>
              <div class="apps-timers-grid" id="apps-timers-grid"></div>
            </div>

            <!-- Sub Navigation Tabs & Panels -->
            <div class="card" style="padding: 1.25rem;">
              <div class="apps-tabs">
                <button class="apps-tab-btn active" data-apps-tab="running">
                  <span>⚡</span> Live Running Apps <span class="tab-badge" id="running-apps-count">0</span>
                </button>
                <button class="apps-tab-btn" data-apps-tab="installed">
                  <span>📦</span> Installed Applications <span class="tab-badge" id="installed-apps-count">0</span>
                </button>
                <button class="apps-tab-btn" data-apps-tab="blocked">
                  <span>🚫</span> Blocked App Rules <span class="tab-badge" id="blocked-apps-count">0</span>
                </button>
                <button class="apps-tab-btn" data-apps-tab="messages">
                  <span>💬</span> Message History <span class="tab-badge" id="messages-count">0</span>
                </button>
              </div>

              <!-- Tab 1: Live Running Apps -->
              <div class="apps-tab-panel active" id="tab-panel-running" style="margin-top: 1.25rem;">
                <div class="apps-toolbar">
                  <div class="apps-search-wrap">
                    <span class="apps-search-icon">🔍</span>
                    <input class="form-input" id="running-apps-search" type="text" placeholder="Search running processes or apps..." />
                  </div>
                  <div class="apps-category-filters">
                    <button class="btn btn-secondary btn-sm" id="refresh-running-apps-btn">🔄 Refresh Processes</button>
                    <button class="btn btn-primary btn-sm" id="open-new-msg-modal-btn">💬 Send Message to Child</button>
                  </div>
                </div>
                <div id="running-apps-table-wrap"></div>
              </div>

              <!-- Tab 2: Installed Apps Catalog -->
              <div class="apps-tab-panel" id="tab-panel-installed" style="margin-top: 1.25rem;">
                <div class="apps-toolbar">
                  <div class="apps-search-wrap">
                    <span class="apps-search-icon">🔍</span>
                    <input class="form-input" id="installed-apps-search" type="text" placeholder="Search 200+ discovered applications..." />
                  </div>
                  <div class="apps-category-filters" id="installed-cat-filters">
                    <button class="apps-cat-chip active" data-cat="all">All Apps</button>
                    <button class="apps-cat-chip" data-cat="Games">🎮 Games</button>
                    <button class="apps-cat-chip" data-cat="Social & Chat">💬 Social & Chat</button>
                    <button class="apps-cat-chip" data-cat="Web Browser">🌐 Browsers</button>
                    <button class="apps-cat-chip" data-cat="Media & Streaming">🎬 Media</button>
                    <button class="apps-cat-chip" data-cat="Development">💻 Dev</button>
                    <button class="apps-cat-chip" data-cat="General">📁 General</button>
                    <button class="btn btn-secondary btn-sm" id="rescan-installed-btn" style="margin-left:auto">🔄 Re-scan Device</button>
                  </div>
                </div>
                <div id="installed-apps-grid-wrap"></div>
              </div>

              <!-- Tab 3: Blocked Apps Rules -->
              <div class="apps-tab-panel" id="tab-panel-blocked" style="margin-top: 1.25rem;">
                <div class="blocklist-header" style="margin-bottom: 1.25rem;">
                  <input class="form-input" id="new-blocked-app-input" type="text" placeholder="Enter process or application name to block (e.g. discord.exe, roblox, steam)" />
                  <select class="form-input" id="new-blocked-app-category" style="max-width: 180px;">
                    <option value="Games">🎮 Games</option>
                    <option value="Social & Chat">💬 Social & Chat</option>
                    <option value="Media & Streaming">🎬 Media</option>
                    <option value="Web Browser">🌐 Browser</option>
                    <option value="General" selected>📁 General</option>
                  </select>
                  <button class="btn btn-primary" id="add-blocked-app-btn" style="width:auto">+ Block Application</button>
                </div>
                <div id="blocked-apps-table-wrap"></div>
              </div>

              <!-- Tab 4: Messages History -->
              <div class="apps-tab-panel" id="tab-panel-messages" style="margin-top: 1.25rem;">
                <div class="apps-toolbar">
                  <h4 style="font-size: 0.95rem; font-weight: 600; color: var(--text-secondary);">Recent Direct Messages to Child's Screen</h4>
                  <button class="btn btn-primary btn-sm" id="open-new-msg-modal-btn2">+ Send New Message</button>
                </div>
                <div id="child-messages-table-wrap"></div>
              </div>
            </div>
          </div>

          <!-- Modal 1: Send Child Message Modal -->
          <div class="modal-overlay" id="app-msg-modal">
            <div class="modal" style="max-width: 520px;">
              <div class="modal-title">💬 Send Message to Child's Screen</div>
              <p style="color: var(--text-secondary); font-size: 0.85rem; margin-bottom: 1.25rem;">
                Displays a prominent floating banner directly on your child's screen.
              </p>
              <div class="form-group">
                <label>Message Title</label>
                <input id="msg-title-input" class="form-input" type="text" placeholder="e.g. Time to Study!" value="Parent Notice" />
                <div class="msg-templates-group">
                  <button class="msg-template-btn" type="button" data-tmpl-title="Time for Homework" data-tmpl-msg="Please pause games and finish your homework assignments now.">📚 Homework</button>
                  <button class="msg-template-btn" type="button" data-tmpl-title="Dinner Time!" data-tmpl-msg="Dinner is ready! Please save your game and come to the kitchen.">🍽️ Dinner</button>
                  <button class="msg-template-btn" type="button" data-tmpl-title="Computer Off in 5 Mins" data-tmpl-msg="Screen time is almost up for today. Wrap up in the next 5 minutes.">⏰ 5 Min Warning</button>
                  <button class="msg-template-btn" type="button" data-tmpl-title="Bedtime" data-tmpl-msg="Time for bed. Shut down your PC and get ready for sleep.">🌙 Bedtime</button>
                </div>
              </div>
              <div class="form-group">
                <label>Message Body</label>
                <textarea id="msg-body-input" class="form-input" rows="3" placeholder="Type what you want to say to your child..."></textarea>
              </div>
              <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                <div class="form-group">
                  <label>Urgency Level</label>
                  <select id="msg-urgency-select" class="form-input">
                    <option value="info">ℹ️ Normal Notice (Blue)</option>
                    <option value="warning" selected>⚠️ Warning (Yellow)</option>
                    <option value="urgent">🚨 Urgent Alarm (Red)</option>
                  </select>
                </div>
                <div class="form-group">
                  <label>Display Duration</label>
                  <select id="msg-duration-select" class="form-input">
                    <option value="6">6 seconds</option>
                    <option value="10" selected>10 seconds</option>
                    <option value="20">20 seconds</option>
                    <option value="30">30 seconds</option>
                  </select>
                </div>
              </div>
              <div class="modal-actions">
                <button class="btn btn-secondary" id="cancel-msg-modal-btn" type="button">Cancel</button>
                <button class="btn btn-primary" id="submit-send-msg-btn" type="button">🚀 Send to Child's Screen</button>
              </div>
            </div>
          </div>

          <!-- Modal 2: Set App Timer Modal -->
          <div class="modal-overlay" id="app-timer-modal">
            <div class="modal" style="max-width: 480px;">
              <div class="modal-title">⏱️ Set App Usage Limit</div>
              <p style="color: var(--text-secondary); font-size: 0.85rem; margin-bottom: 1.25rem;">
                Automatically closes the application when the timer expires.
              </p>
              <div class="form-group">
                <label>Target Application</label>
                <input id="timer-app-name-input" class="form-input" type="text" readonly />
                <input id="timer-process-name-hidden" type="hidden" />
              </div>
              <div class="form-group">
                <label>Select Duration</label>
                <div class="timer-presets">
                  <button class="timer-preset-btn" type="button" data-timer-sec="300">5 min</button>
                  <button class="timer-preset-btn" type="button" data-timer-sec="600">10 min</button>
                  <button class="timer-preset-btn active" type="button" data-timer-sec="900">15 min</button>
                  <button class="timer-preset-btn" type="button" data-timer-sec="1800">30 min</button>
                  <button class="timer-preset-btn" type="button" data-timer-sec="2700">45 min</button>
                  <button class="timer-preset-btn" type="button" data-timer-sec="3600">60 min</button>
                  <button class="timer-preset-btn" type="button" data-timer-sec="5400">90 min</button>
                  <button class="timer-preset-btn" type="button" data-timer-sec="7200">2 hours</button>
                </div>
              </div>
              <div class="form-group">
                <label>Or Custom Minutes</label>
                <input id="timer-custom-min-input" class="form-input" type="number" min="1" max="720" placeholder="e.g. 25" />
              </div>
              <div class="modal-actions">
                <button class="btn btn-secondary" id="cancel-timer-modal-btn" type="button">Cancel</button>
                <button class="btn btn-primary" id="submit-timer-btn" type="button" style="background: var(--accent-orange); border-color: var(--accent-orange);">⏱️ Start Countdown Timer</button>
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
  const titles = {
    live: "Live View",
    devices: "Devices",
    apps: "Application Tracking & Control",
    blocklist: "Blocklist",
    rules: "Rules & Limits",
    alerts: "Alerts",
  };
  const titleEl = document.getElementById("page-title");
  if (titleEl) {
    titleEl.textContent = state.controllerMode && page === "live"
      ? "Output Controller"
      : (titles[page] || page);
  }

  // Load data for the page
  if (page === "blocklist") loadBlocklist();
  if (page === "apps") {
    loadAppsOverview();
    loadInstalledApps();
    loadChildMessages();
  }
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

  // ─── Application Tracking Interactive Event Listeners ────────────────────

  // Apps Subtab switching
  document.addEventListener("click", (e) => {
    const tabBtn = e.target.closest("[data-apps-tab]");
    if (tabBtn) {
      const tab = tabBtn.dataset.appsTab;
      state.appsData.selectedTab = tab;

      document.querySelectorAll("[data-apps-tab]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.appsTab === tab);
      });

      document.querySelectorAll(".apps-tab-panel").forEach((p) => {
        p.classList.toggle("active", p.id === `tab-panel-${tab}`);
      });

      if (tab === "running") renderRunningAppsTable();
      else if (tab === "installed") {
        if (!state.appsData.installedApps.length) loadInstalledApps();
        else renderInstalledAppsGrid();
      } else if (tab === "blocked") renderBlockedAppsList();
      else if (tab === "messages") {
        if (!state.appsData.messages.length) loadChildMessages();
        else renderChildMessagesList();
      }
    }

    // Category filter chips in Installed Apps
    const catChip = e.target.closest(".apps-cat-chip");
    if (catChip) {
      document.querySelectorAll(".apps-cat-chip").forEach((c) => c.classList.remove("active"));
      catChip.classList.add("active");
      state.appsData.installedCategory = catChip.dataset.cat || "all";
      renderInstalledAppsGrid();
    }

    // Close App Now (anonymous / instant kill)
    const closeBtn = e.target.closest("[data-close-pid], [data-close-proc], #hero-close-app-btn");
    if (closeBtn && (closeBtn.id === "hero-close-app-btn" || closeBtn.dataset.closePid || closeBtn.dataset.closeProc)) {
      const pid = closeBtn.dataset.closePid || closeBtn.dataset.pid;
      const proc = closeBtn.dataset.closeProc || closeBtn.dataset.proc;
      const appName = closeBtn.dataset.appName || proc || `PID ${pid}`;

      if (!state.selectedDeviceId) {
        showToast("Please select a device first", "warning");
        return;
      }

      if (confirm(`Close application '${appName}' immediately on child's screen?`)) {
        api("/apps/close", {
          method: "POST",
          body: JSON.stringify({
            deviceId: state.selectedDeviceId,
            pid: pid ? parseInt(pid, 10) : null,
            process_name: proc,
            app_name: appName,
          }),
        })
          .then(() => {
            showToast(`Closed ${appName}`, "success");
            setTimeout(loadAppsOverview, 1000);
          })
          .catch((err) => showToast(err.message, "error"));
      }
    }

    // Open Set App Timer Modal
    const timerOpenBtn = e.target.closest("[data-app-timer], #hero-set-timer-btn");
    if (timerOpenBtn) {
      const proc = timerOpenBtn.dataset.appTimer || timerOpenBtn.dataset.proc || "";
      const appName = timerOpenBtn.dataset.appName || proc || "Selected App";

      const modal = document.getElementById("app-timer-modal");
      const nameInput = document.getElementById("timer-app-name-input");
      const procHidden = document.getElementById("timer-process-name-hidden");

      if (modal && nameInput && procHidden) {
        nameInput.value = appName;
        procHidden.value = proc;
        modal.classList.add("visible");
      }
    }

    // Preset button selection inside Timer Modal
    const presetBtn = e.target.closest(".timer-preset-btn");
    if (presetBtn) {
      document.querySelectorAll(".timer-preset-btn").forEach((b) => b.classList.remove("active"));
      presetBtn.classList.add("active");
    }

    // Cancel active timer
    const cancelTimerBtn = e.target.closest("[data-cancel-timer]");
    if (cancelTimerBtn) {
      const target = cancelTimerBtn.dataset.cancelTimer;
      api(`/apps/timer/${encodeURIComponent(target)}`, { method: "DELETE" })
        .then(() => {
          showToast("App timer cancelled", "info");
          loadAppsOverview();
        })
        .catch((err) => showToast(err.message, "error"));
    }

    // Open Send Child Message Modal
    const msgOpenBtn = e.target.closest("[data-app-msg], #hero-send-msg-btn, #open-new-msg-modal-btn, #open-new-msg-modal-btn2");
    if (msgOpenBtn) {
      const appHint = msgOpenBtn.dataset.appMsg || "";
      const modal = document.getElementById("app-msg-modal");
      const bodyInput = document.getElementById("msg-body-input");

      if (modal) {
        if (appHint && bodyInput && !bodyInput.value) {
          bodyInput.value = `Please wrap up what you are doing in ${appHint}.`;
        }
        modal.classList.add("visible");
      }
    }

    // Template chip clicked in Message Modal
    const tmplBtn = e.target.closest(".msg-template-btn");
    if (tmplBtn) {
      const titleInput = document.getElementById("msg-title-input");
      const bodyInput = document.getElementById("msg-body-input");
      if (titleInput && tmplBtn.dataset.tmplTitle) titleInput.value = tmplBtn.dataset.tmplTitle;
      if (bodyInput && tmplBtn.dataset.tmplMsg) bodyInput.value = tmplBtn.dataset.tmplMsg;
    }

    // Block App quick action button
    const blockBtn = e.target.closest("[data-block-proc], #hero-block-app-btn");
    if (blockBtn) {
      const proc = blockBtn.dataset.blockProc || blockBtn.dataset.proc;
      const appName = blockBtn.dataset.appName || proc;
      const cat = blockBtn.dataset.cat || "General";

      if (!proc) {
        showToast("Process name missing", "warning");
        return;
      }

      if (confirm(`Block '${appName}' (${proc})? The agent will immediately kill it whenever launched.`)) {
        api("/apps/blocked", {
          method: "POST",
          body: JSON.stringify({
            deviceId: state.selectedDeviceId,
            app_name: appName,
            process_name: proc,
            category: cat,
          }),
        })
          .then(() => {
            showToast(`Blocked application: ${appName}`, "success");
            loadAppsOverview();
            loadInstalledApps();
          })
          .catch((err) => showToast(err.message, "error"));
      }
    }

    // Unblock App button by ID or process
    const unblockAppBtn = e.target.closest("[data-unblock-app-id]");
    if (unblockAppBtn) {
      const id = unblockAppBtn.dataset.unblockAppId;
      api(`/apps/blocked/${id}`, { method: "DELETE" })
        .then(() => {
          showToast("Application unblocked", "success");
          loadAppsOverview();
          loadInstalledApps();
        })
        .catch((err) => showToast(err.message, "error"));
    }

    const unblockProcBtn = e.target.closest("[data-unblock-proc]");
    if (unblockProcBtn) {
      const proc = unblockProcBtn.dataset.unblockProc.toLowerCase();
      const existing = (state.appsData.blockedApps || []).find(
        (b) => b.process_name.toLowerCase() === proc
      );
      if (existing) {
        api(`/apps/blocked/${existing.id}`, { method: "DELETE" })
          .then(() => {
            showToast(`Unblocked ${existing.app_name}`, "success");
            loadAppsOverview();
            loadInstalledApps();
          })
          .catch((err) => showToast(err.message, "error"));
      }
    }

    // Re-scan installed apps
    const rescanBtn = e.target.closest("#rescan-installed-btn");
    if (rescanBtn) {
      if (!state.selectedDeviceId) {
        showToast("Select a device first", "warning");
        return;
      }
      rescanBtn.disabled = true;
      rescanBtn.textContent = "Scanning...";
      api(`/apps/installed/refresh/${state.selectedDeviceId}`, { method: "POST" })
        .then(() => {
          showToast("Scan command sent to device", "info");
          setTimeout(() => {
            rescanBtn.disabled = false;
            rescanBtn.textContent = "🔄 Re-scan Device";
            loadInstalledApps();
          }, 3000);
        })
        .catch((err) => {
          rescanBtn.disabled = false;
          rescanBtn.textContent = "🔄 Re-scan Device";
          showToast(err.message, "error");
        });
    }

    // Refresh running apps button
    const refreshRunningBtn = e.target.closest("#refresh-running-apps-btn");
    if (refreshRunningBtn) {
      loadAppsOverview();
      showToast("Refreshed running processes", "info");
    }
  });

  // Modal Cancel Buttons
  document.getElementById("cancel-msg-modal-btn")?.addEventListener("click", () => {
    document.getElementById("app-msg-modal")?.classList.remove("visible");
  });

  document.getElementById("cancel-timer-modal-btn")?.addEventListener("click", () => {
    document.getElementById("app-timer-modal")?.classList.remove("visible");
  });

  // Submit Send Child Message
  document.getElementById("submit-send-msg-btn")?.addEventListener("click", () => {
    if (!state.selectedDeviceId) {
      showToast("Please select a device first", "warning");
      return;
    }

    const title = document.getElementById("msg-title-input")?.value.trim() || "Parent Notice";
    const body = document.getElementById("msg-body-input")?.value.trim();
    const urgency = document.getElementById("msg-urgency-select")?.value || "info";
    const duration = parseInt(document.getElementById("msg-duration-select")?.value || "10", 10);

    if (!body) {
      showToast("Please enter a message", "warning");
      return;
    }

    api("/apps/message", {
      method: "POST",
      body: JSON.stringify({
        deviceId: state.selectedDeviceId,
        title,
        message: body,
        urgency,
        duration,
      }),
    })
      .then(() => {
        showToast("Message sent to child's screen!", "success");
        document.getElementById("app-msg-modal")?.classList.remove("visible");
        const bodyEl = document.getElementById("msg-body-input");
        if (bodyEl) bodyEl.value = "";
        loadChildMessages();
      })
      .catch((err) => showToast(err.message, "error"));
  });

  // Submit App Timer Limit
  document.getElementById("submit-timer-btn")?.addEventListener("click", () => {
    if (!state.selectedDeviceId) {
      showToast("Please select a device first", "warning");
      return;
    }

    const appName = document.getElementById("timer-app-name-input")?.value || "App";
    const procName = document.getElementById("timer-process-name-hidden")?.value || appName;
    const customMin = parseInt(document.getElementById("timer-custom-min-input")?.value || "0", 10);

    let sec = 900; // default 15 min
    if (customMin > 0) {
      sec = customMin * 60;
    } else {
      const activePreset = document.querySelector(".timer-preset-btn.active");
      if (activePreset && activePreset.dataset.timerSec) {
        sec = parseInt(activePreset.dataset.timerSec, 10);
      }
    }

    api("/apps/timer", {
      method: "POST",
      body: JSON.stringify({
        deviceId: state.selectedDeviceId,
        app_name: appName,
        process_name: procName,
        duration_seconds: sec,
      }),
    })
      .then(() => {
        showToast(`Timer set: ${appName} closes in ${Math.round(sec / 60)} min`, "success");
        document.getElementById("app-timer-modal")?.classList.remove("visible");
        loadAppsOverview();
      })
      .catch((err) => showToast(err.message, "error"));
  });

  // Add Manual Blocked App Form Submit
  document.getElementById("add-blocked-app-btn")?.addEventListener("click", () => {
    const input = document.getElementById("new-blocked-app-input");
    const catSelect = document.getElementById("new-blocked-app-category");
    const raw = (input?.value || "").trim();

    if (!raw) {
      showToast("Please enter an application or process name", "warning");
      return;
    }

    api("/apps/blocked", {
      method: "POST",
      body: JSON.stringify({
        deviceId: state.selectedDeviceId,
        app_name: raw.replace(".exe", "").title || raw,
        process_name: raw,
        category: catSelect?.value || "General",
      }),
    })
      .then(() => {
        showToast(`Blocked application: ${raw}`, "success");
        if (input) input.value = "";
        loadAppsOverview();
      })
      .catch((err) => showToast(err.message, "error"));
  });

  // Search input listeners
  document.getElementById("running-apps-search")?.addEventListener("input", (e) => {
    state.appsData.runningSearch = e.target.value;
    renderRunningAppsTable();
  });

  document.getElementById("installed-apps-search")?.addEventListener("input", (e) => {
    state.appsData.installedSearch = e.target.value;
    renderInstalledAppsGrid();
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
  // BUG-11: this used to escape via textContent → innerHTML, which handles
  // & < > but leaves " and ' untouched. Most call sites interpolate into HTML
  // attributes (title="...", data-app="..."), so an app name or window title
  // containing a quote could close the attribute and inject markup — and those
  // strings come straight off the monitored device. Escape quotes too.
  return String(str == null ? "" : str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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
