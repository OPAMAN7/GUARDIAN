/**
 * Guardian Shield — Blocklist Routes
 * Manage blocked websites from the dashboard.
 */

const express = require("express");
const db = require("../models/db");
const { authMiddleware } = require("./auth");

const router = express.Router();
router.use(authMiddleware);

function normalizeDomainInput(rawValue) {
  if (!rawValue || typeof rawValue !== "string") return null;

  let value = rawValue.trim().toLowerCase();
  if (!value) return null;

  // Accept pasted URLs by extracting hostname.
  try {
    const withProto = /^[a-z][a-z0-9+.-]*:\/\//i.test(value) ? value : `http://${value}`;
    const parsed = new URL(withProto);
    value = (parsed.hostname || "").trim().toLowerCase();
  } catch (_err) {
    value = value.replace(/^https?:\/\//, "").split(/[\/?#:]/)[0].trim().toLowerCase();
  }

  // Remove brackets around IPv6 style host literals and trailing dots.
  value = value.replace(/^\[|\]$/g, "").replace(/\.+$/, "");

  if (!value || value === "localhost") return null;
  if (!/^[a-z0-9.-]+$/i.test(value)) return null;
  if (value.length > 253) return null;
  return value;
}

function normalizeStoredBlocklistDomains() {
  const rows = db.prepare("SELECT id, domain FROM blocklist ORDER BY id ASC").all();
  if (!rows.length) return;

  const tx = db.transaction((items) => {
    const seen = new Set();
    for (const row of items) {
      const normalized = normalizeDomainInput(row.domain);

      if (!normalized || seen.has(normalized)) {
        db.prepare("DELETE FROM blocklist WHERE id = ?").run(row.id);
        continue;
      }

      seen.add(normalized);
      if (normalized !== row.domain) {
        db.prepare("UPDATE blocklist SET domain = ? WHERE id = ?").run(normalized, row.id);
      }
    }
  });

  tx(rows);
}

normalizeStoredBlocklistDomains();

/**
 * GET /api/blocklist
 * Returns all blocked domains
 */
router.get("/", (req, res) => {
  const domains = db
    .prepare("SELECT * FROM blocklist ORDER BY domain ASC")
    .all();
  res.json(domains);
});

/**
 * POST /api/blocklist
 * Body: { domain, category? }
 * Add a domain to the blocklist
 */
router.post("/", (req, res) => {
  const { domain, category } = req.body;
  if (!domain) return res.status(400).json({ error: "Domain required" });

  const cleanDomain = normalizeDomainInput(domain);
  if (!cleanDomain) {
    return res.status(400).json({ error: "Invalid domain or URL" });
  }

  try {
    db.prepare(
      "INSERT INTO blocklist (domain, category, added_by) VALUES (?, ?, ?)"
    ).run(cleanDomain, category || "custom", req.user.username);

    // Notify connected agents to update their blocklist
    const io = req.app.get("io");
    if (io) {
      const allDomains = db
        .prepare("SELECT domain FROM blocklist")
        .all()
        .map((r) => r.domain);
      io.emit("update_blocklist", { domains: allDomains });
    }

    res.status(201).json({ message: "Domain blocked", domain: cleanDomain });
  } catch (err) {
    if (err.message.includes("UNIQUE")) {
      return res.status(409).json({ error: "Domain already blocked" });
    }
    res.status(500).json({ error: err.message });
  }
});

/**
 * POST /api/blocklist/bulk
 * Body: { domains: string[] }
 * Add multiple domains at once
 */
router.post("/bulk", (req, res) => {
  const { domains } = req.body;
  if (!Array.isArray(domains)) {
    return res.status(400).json({ error: "domains array required" });
  }

  const normalized = domains
    .map((d) => normalizeDomainInput(String(d || "")))
    .filter(Boolean);

  if (normalized.length === 0) {
    return res.status(400).json({ error: "No valid domains supplied" });
  }

  const insert = db.prepare(
    "INSERT OR IGNORE INTO blocklist (domain, category, added_by) VALUES (?, ?, ?)"
  );

  const insertMany = db.transaction((domainList) => {
    let added = 0;
    for (const domain of domainList) {
      const result = insert.run(
        domain.toLowerCase().trim(),
        "custom",
        req.user.username
      );
      if (result.changes > 0) added++;
    }
    return added;
  });

  const added = insertMany(normalized);

  // Notify agents
  const io = req.app.get("io");
  if (io) {
    const allDomains = db
      .prepare("SELECT domain FROM blocklist")
      .all()
      .map((r) => r.domain);
    io.emit("update_blocklist", { domains: allDomains });
  }

  res.json({ message: `${added} domains added`, total: normalized.length });
});

/**
 * DELETE /api/blocklist/:id
 * Remove a domain from blocklist
 */
router.delete("/:id", (req, res) => {
  const result = db
    .prepare("DELETE FROM blocklist WHERE id = ?")
    .run(req.params.id);

  if (result.changes === 0) {
    return res.status(404).json({ error: "Domain not found" });
  }

  // Notify agents
  const io = req.app.get("io");
  if (io) {
    const allDomains = db
      .prepare("SELECT domain FROM blocklist")
      .all()
      .map((r) => r.domain);
    io.emit("update_blocklist", { domains: allDomains });
  }

  res.json({ message: "Domain unblocked" });
});

/**
 * GET /api/blocklist/search?q=query
 * Search blocklist
 */
router.get("/search", (req, res) => {
  const q = req.query.q;
  if (!q) return res.status(400).json({ error: "Query required" });

  const results = db
    .prepare("SELECT * FROM blocklist WHERE domain LIKE ? LIMIT 50")
    .all(`%${q}%`);
  res.json(results);
});

module.exports = router;
