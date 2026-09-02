/**
 * Guardian Shield — Auth Routes
 * JWT-based authentication for the dashboard.
 */

const express = require("express");
const jwt = require("jsonwebtoken");
const bcrypt = require("bcryptjs");
const db = require("../models/db");

const router = express.Router();

const JWT_SECRET = process.env.JWT_SECRET;
const TOKEN_EXPIRY = "24h";
const DEV_JWT_SECRET = "guardian-shield-dev-only-secret";

if (!JWT_SECRET && process.env.NODE_ENV === "production") {
  throw new Error("JWT_SECRET is required in production");
}

const EFFECTIVE_JWT_SECRET = JWT_SECRET || DEV_JWT_SECRET;

// Simple in-memory login throttling to reduce brute-force attempts.
const LOGIN_WINDOW_MS = 10 * 60 * 1000;
const MAX_LOGIN_ATTEMPTS = 10;
const loginAttempts = new Map();

function getClientKey(req) {
  const forwarded = req.headers["x-forwarded-for"];
  const firstForwarded = typeof forwarded === "string" ? forwarded.split(",")[0] : "";
  const ip = (firstForwarded || req.socket.remoteAddress || "unknown").trim();
  const username = (req.body?.username || "").toLowerCase().trim();
  return `${ip}:${username}`;
}

function pruneAndGetAttempt(key) {
  const now = Date.now();
  const current = loginAttempts.get(key);
  if (!current) return { count: 0, resetAt: now + LOGIN_WINDOW_MS };
  if (now > current.resetAt) {
    loginAttempts.delete(key);
    return { count: 0, resetAt: now + LOGIN_WINDOW_MS };
  }
  return current;
}

function registerFailedAttempt(key) {
  const now = Date.now();
  const current = pruneAndGetAttempt(key);
  loginAttempts.set(key, {
    count: current.count + 1,
    resetAt: current.resetAt || now + LOGIN_WINDOW_MS,
  });
}

/**
 * POST /api/auth/login
 * Body: { username, password }
 * Returns: { token, user: { id, username, role } }
 */
router.post("/login", (req, res) => {
  const clientKey = getClientKey(req);
  const currentAttempt = pruneAndGetAttempt(clientKey);
  if (currentAttempt.count >= MAX_LOGIN_ATTEMPTS) {
    return res.status(429).json({
      error: "Too many login attempts. Try again in a few minutes.",
    });
  }

  const { username, password } = req.body;

  if (!username || !password) {
    registerFailedAttempt(clientKey);
    return res.status(400).json({ error: "Username and password required" });
  }

  const user = db
    .prepare("SELECT * FROM users WHERE username = ?")
    .get(username);

  if (!user) {
    registerFailedAttempt(clientKey);
    return res.status(401).json({ error: "Invalid credentials" });
  }

  const valid = bcrypt.compareSync(password, user.password_hash);
  if (!valid) {
    registerFailedAttempt(clientKey);
    return res.status(401).json({ error: "Invalid credentials" });
  }

  loginAttempts.delete(clientKey);

  const token = jwt.sign(
    { id: user.id, username: user.username, role: user.role },
    EFFECTIVE_JWT_SECRET,
    { expiresIn: TOKEN_EXPIRY }
  );

  res.json({
    token,
    user: {
      id: user.id,
      username: user.username,
      role: user.role,
    },
  });
});

/**
 * GET /api/auth/me
 * Headers: Authorization: Bearer <token>
 * Returns current user info
 */
router.get("/me", (req, res) => {
  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    return res.status(401).json({ error: "No token provided" });
  }

  try {
    const token = authHeader.split(" ")[1];
    const decoded = jwt.verify(token, EFFECTIVE_JWT_SECRET);
    res.json({
      id: decoded.id,
      username: decoded.username,
      role: decoded.role,
    });
  } catch (err) {
    res.status(401).json({ error: "Invalid token" });
  }
});

/**
 * POST /api/auth/change-password
 * Body: { currentPassword, newPassword }
 */
router.post("/change-password", (req, res) => {
  const authHeader = req.headers.authorization;
  if (!authHeader) return res.status(401).json({ error: "Unauthorized" });

  try {
    const token = authHeader.split(" ")[1];
    const decoded = jwt.verify(token, EFFECTIVE_JWT_SECRET);

    const { currentPassword, newPassword } = req.body;
    if (!currentPassword || !newPassword) {
      return res.status(400).json({ error: "Both passwords required" });
    }
    if (newPassword.length < 8) {
      return res.status(400).json({ error: "New password must be at least 8 characters" });
    }
    if (newPassword === currentPassword) {
      return res.status(400).json({ error: "New password must be different" });
    }

    const user = db.prepare("SELECT * FROM users WHERE id = ?").get(decoded.id);
    if (!bcrypt.compareSync(currentPassword, user.password_hash)) {
      return res.status(401).json({ error: "Current password is incorrect" });
    }

    const hash = bcrypt.hashSync(newPassword, 10);
    db.prepare("UPDATE users SET password_hash = ? WHERE id = ?").run(hash, decoded.id);

    res.json({ message: "Password changed successfully" });
  } catch (err) {
    res.status(401).json({ error: "Invalid token" });
  }
});

// Auth middleware for other routes
function authMiddleware(req, res, next) {
  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    return res.status(401).json({ error: "Authentication required" });
  }

  try {
    const token = authHeader.split(" ")[1];
    req.user = jwt.verify(token, EFFECTIVE_JWT_SECRET);
    next();
  } catch (err) {
    res.status(401).json({ error: "Invalid or expired token" });
  }
}

/**
 * BUG-12: verify a JWT presented over a Socket.IO handshake (or a later
 * `dashboard_auth` event). Returns the decoded payload, or null when the token is
 * missing/invalid/expired. Lives here so the JWT secret — including the dev
 * fallback — is resolved in exactly one place.
 */
function verifyToken(rawToken) {
  const token =
    typeof rawToken === "string" ? rawToken.replace(/^Bearer\s+/i, "").trim() : "";
  if (!token) return null;
  try {
    return jwt.verify(token, EFFECTIVE_JWT_SECRET);
  } catch (err) {
    return null;
  }
}

module.exports = { router, authMiddleware, verifyToken };
