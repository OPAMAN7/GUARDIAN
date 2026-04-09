/**
 * Guardian Shield - Raspberry Pi Presentation Emulator
 *
 * This script simulates a monitored Raspberry Pi device by sending:
 * - register_device events
 * - live screenshot frames (SVG data URI with Pi-style skin)
 * - alert events with verdict metadata
 *
 * Modes:
 * - default: continuous stream for presentation
 * - --buzzer: send one warning alert to trigger dashboard buzzer
 */

const { io } = require("socket.io-client");
require("dotenv").config();

const args = new Set(process.argv.slice(2));
const mode = args.has("--buzzer") ? "buzzer" : "stream";

const SERVER_URL = process.env.EMULATOR_SERVER_URL || process.env.SERVER_URL || "http://localhost:3000";
const DEVICE_NAME = process.env.EMULATOR_DEVICE_NAME || "Raspberry Pi 4 - Demo Unit";
const HOSTNAME = process.env.EMULATOR_HOSTNAME || "guardian-demo-pi";
const AUTH_TOKEN = process.env.EMULATOR_AUTH_TOKEN || process.env.AGENT_SHARED_SECRET || "";
const FRAME_INTERVAL_MS = Number.parseInt(process.env.EMULATOR_INTERVAL_MS || "2500", 10);

const userScenarios = [
  {
    user: "Aarav",
    focus: "YouTube Shorts",
    verdict: "MONITOR",
    riskScore: 36,
    mood: "curious",
  },
  {
    user: "Aarav",
    focus: "math-homework.pdf",
    verdict: "SAFE",
    riskScore: 8,
    mood: "focused",
  },
  {
    user: "Aarav",
    focus: "unknown-installer.exe",
    verdict: "BLOCKED",
    riskScore: 91,
    mood: "risky",
  },
  {
    user: "Aarav",
    focus: "online game lobby",
    verdict: "MONITOR",
    riskScore: 41,
    mood: "active",
  },
  {
    user: "Aarav",
    focus: "education portal",
    verdict: "SAFE",
    riskScore: 12,
    mood: "calm",
  },
];

const processStart = Date.now();
let frameCount = 0;
let alertTick = 0;

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function verdictColor(verdict) {
  if (verdict === "SAFE") return "#22c55e";
  if (verdict === "BLOCKED") return "#ef4444";
  return "#f59e0b";
}

function riskBand(score) {
  if (score >= 75) return "high";
  if (score >= 35) return "medium";
  return "low";
}

function sessionSeconds() {
  return Math.floor((Date.now() - processStart) / 1000);
}

function pickScenario() {
  return userScenarios[frameCount % userScenarios.length];
}

function buildFrameSvg(scenario) {
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  const ss = String(now.getSeconds()).padStart(2, "0");
  const clock = `${hh}:${mm}:${ss}`;

  const verdict = escHtml(scenario.verdict);
  const focus = escHtml(scenario.focus);
  const user = escHtml(scenario.user);
  const riskScore = Math.max(0, Math.min(100, Number(scenario.riskScore) || 0));
  const riskWidth = Math.round((riskScore / 100) * 360);
  const accent = verdictColor(scenario.verdict);

  const svg = `
<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <linearGradient id="bgA" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="45%" stop-color="#1f2937"/>
      <stop offset="100%" stop-color="#111827"/>
    </linearGradient>
    <linearGradient id="board" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#14532d"/>
      <stop offset="100%" stop-color="#166534"/>
    </linearGradient>
    <linearGradient id="screenGlow" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#0b1220"/>
      <stop offset="100%" stop-color="#05080f"/>
    </linearGradient>
    <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="12" stdDeviation="10" flood-color="#000" flood-opacity="0.35"/>
    </filter>
  </defs>

  <rect width="1280" height="720" fill="url(#bgA)"/>

  <rect x="75" y="70" width="1130" height="580" rx="30" fill="url(#board)" filter="url(#softShadow)"/>
  <circle cx="190" cy="145" r="8" fill="#ef4444"/>
  <circle cx="220" cy="145" r="8" fill="#f59e0b"/>
  <circle cx="250" cy="145" r="8" fill="#22c55e"/>

  <rect x="220" y="130" width="840" height="470" rx="20" fill="#05070d" stroke="#334155" stroke-width="6"/>
  <rect x="242" y="152" width="796" height="426" rx="14" fill="url(#screenGlow)"/>

  <rect x="270" y="185" width="250" height="120" rx="12" fill="#111827" stroke="#1f2937"/>
  <text x="290" y="218" fill="#93c5fd" font-family="Segoe UI, Arial, sans-serif" font-size="18" font-weight="700">TRACKED USER</text>
  <text x="290" y="248" fill="#f8fafc" font-family="Segoe UI, Arial, sans-serif" font-size="28" font-weight="700">${user}</text>
  <text x="290" y="280" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="16">Session ${sessionSeconds()}s</text>

  <rect x="550" y="185" width="460" height="120" rx="12" fill="#111827" stroke="#1f2937"/>
  <text x="575" y="220" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="16">Current Focus</text>
  <text x="575" y="255" fill="#f8fafc" font-family="Segoe UI, Arial, sans-serif" font-size="30" font-weight="700">${focus}</text>
  <text x="575" y="285" fill="#38bdf8" font-family="Segoe UI, Arial, sans-serif" font-size="15">Live capture ${clock}</text>

  <rect x="270" y="335" width="740" height="105" rx="12" fill="#0b1220" stroke="#1f2937"/>
  <text x="290" y="368" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="15">AI Verdict</text>
  <text x="290" y="410" fill="${accent}" font-family="Segoe UI, Arial, sans-serif" font-size="40" font-weight="800">${verdict}</text>

  <rect x="270" y="462" width="740" height="82" rx="10" fill="#0b1220" stroke="#1f2937"/>
  <text x="290" y="495" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="15">Risk Score</text>
  <rect x="290" y="507" width="360" height="16" rx="8" fill="#1f2937"/>
  <rect x="290" y="507" width="${riskWidth}" height="16" rx="8" fill="${accent}"/>
  <text x="670" y="521" fill="#e2e8f0" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="700">${riskScore}/100 (${riskBand(riskScore)})</text>

  <rect x="1050" y="190" width="90" height="56" rx="8" fill="#0f172a" stroke="#334155"/>
  <text x="1060" y="222" fill="#cbd5e1" font-family="Segoe UI, Arial, sans-serif" font-size="13">GPU</text>
  <text x="1060" y="240" fill="#22c55e" font-family="Segoe UI, Arial, sans-serif" font-size="12">DEMO</text>

  <rect x="1050" y="260" width="90" height="56" rx="8" fill="#0f172a" stroke="#334155"/>
  <text x="1060" y="292" fill="#cbd5e1" font-family="Segoe UI, Arial, sans-serif" font-size="13">NET</text>
  <text x="1060" y="310" fill="#38bdf8" font-family="Segoe UI, Arial, sans-serif" font-size="12">LIVE</text>

  <rect x="108" y="624" width="220" height="14" rx="7" fill="#0f172a" stroke="#475569"/>
  <rect x="108" y="624" width="180" height="14" rx="7" fill="#22c55e"/>
  <text x="340" y="636" fill="#86efac" font-family="Segoe UI, Arial, sans-serif" font-size="12">POWER</text>

  <text x="960" y="636" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="14">Guardian Shield Presentation Emulator</text>
</svg>`;

  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

function verdictToAlertType(verdict) {
  if (verdict === "BLOCKED") return "dangerous_file";
  if (verdict === "MONITOR") return "info";
  return "info";
}

function sendVerdictAlert(socket, scenario, prefix = "Presentation verdict") {
  const message = `${prefix}: ${scenario.verdict} (${scenario.focus})`;
  socket.emit("alert", {
    device_name: DEVICE_NAME,
    type: verdictToAlertType(scenario.verdict),
    message,
    details: {
      presentation: true,
      user: scenario.user,
      verdict: scenario.verdict,
      focus: scenario.focus,
      riskScore: scenario.riskScore,
    },
    timestamp: Date.now() / 1000,
  });
}

function runBuzzerMode() {
  const socket = io(SERVER_URL, {
    transports: ["websocket", "polling"],
    reconnection: false,
  });

  socket.on("connect", () => {
    socket.emit("register_device", {
      device_name: DEVICE_NAME,
      platform: "linux-demo",
      hostname: HOSTNAME,
      auth_token: AUTH_TOKEN,
    });

    const scenario = {
      user: "Aarav",
      focus: "manual buzzer test",
      verdict: "BLOCKED",
      riskScore: 95,
    };

    sendVerdictAlert(socket, scenario, "Presentation buzzer test");

    setTimeout(() => {
      socket.disconnect();
      process.exit(0);
    }, 600);
  });

  socket.on("connect_error", (err) => {
    console.error(`[emulator] connect error: ${err.message}`);
    process.exit(1);
  });
}

function runStreamMode() {
  const socket = io(SERVER_URL, {
    transports: ["websocket", "polling"],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
  });

  let timer = null;

  function emitFrame() {
    const scenario = pickScenario();
    const image = buildFrameSvg(scenario);
    const payload = {
      device_name: DEVICE_NAME,
      image,
      timestamp: Date.now() / 1000,
      size: image.length,
      meta: {
        presentation: true,
        user: scenario.user,
        verdict: scenario.verdict,
        focus: scenario.focus,
        riskScore: scenario.riskScore,
        sessionSeconds: sessionSeconds(),
      },
    };

    socket.emit("screenshot", payload);

    alertTick += 1;
    if (alertTick % 6 === 0) {
      sendVerdictAlert(socket, scenario);
    }

    frameCount += 1;
  }

  socket.on("connect", () => {
    console.log(`[emulator] connected to ${SERVER_URL}`);
    socket.emit("register_device", {
      device_name: DEVICE_NAME,
      platform: "linux-demo",
      hostname: HOSTNAME,
      auth_token: AUTH_TOKEN,
    });

    emitFrame();
    timer = setInterval(emitFrame, FRAME_INTERVAL_MS);
  });

  socket.on("disconnect", (reason) => {
    console.log(`[emulator] disconnected: ${reason}`);
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  });

  socket.on("connect_error", (err) => {
    console.error(`[emulator] connect error: ${err.message}`);
  });

  process.on("SIGINT", () => {
    if (timer) clearInterval(timer);
    socket.disconnect();
    process.exit(0);
  });
}

console.log(`[emulator] mode=${mode} server=${SERVER_URL} device=${DEVICE_NAME}`);
if (mode === "buzzer") {
  runBuzzerMode();
} else {
  runStreamMode();
}
