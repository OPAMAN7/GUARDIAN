/**
 * Guardian Shield — Activity Classifier
 * =====================================
 *
 * Turns raw facts into one of three *separate* taxonomies and an academic
 * weight. Classification lives on the server, never on the Pi, for two reasons:
 * a rule change must not require re-deploying the agent, and a parent override
 * has to take effect on the next page load.
 *
 * The one rule that shapes this file
 * ---------------------------------
 * The three sources have three disjoint vocabularies:
 *
 *   source 'app'   -> app_category    (study_tools, coding, gaming, ...)
 *   source 'web'   -> web_category    (academic, reference, social, ...)
 *   source 'watch' -> watch_category  (lecture, tutorial, shorts, ...)
 *
 * No category string is shared between them, so a mislabelled row can never
 * silently move time from one ledger to another. `classifyApp` cannot return a
 * web category even if a rule row is corrupt, because it only ever consults
 * rules whose `source` column is 'app'.
 *
 * This is also NOT the taxonomy the shipped Apps page uses. `app_tracker`'s
 * "Games / Social & Chat / Web Browser / ..." labels stay exactly as they are;
 * these live in their own columns.
 */

const db = require("../models/db");

// ─── Weights ────────────────────────────────────────────────────────────────
// +2 academic, +1 constructive, 0 neutral, -1 passive, -2 distraction.
// The band a weight falls into is what the grade is computed from, so the
// weight is stored on every row: re-labelling a category later cannot silently
// rewrite last month's grade.
const W = { ACADEMIC: 2, CONSTRUCTIVE: 1, NEUTRAL: 0, PASSIVE: -1, DISTRACTION: -2 };

const BAND_BY_WEIGHT = {
  "2": "academic",
  "1": "constructive",
  "0": "neutral",
  "-1": "passive",
  "-2": "distraction",
};

/** Which bucket of the score a weight contributes to. */
function band(weight) {
  return BAND_BY_WEIGHT[String(weight)] || "neutral";
}

// ─── App taxonomy (source: 'app') ───────────────────────────────────────────
const APP_CATEGORIES = {
  study_tools: W.ACADEMIC,
  coding: W.ACADEMIC,
  writing_docs: W.CONSTRUCTIVE,
  creative: W.CONSTRUCTIVE,
  communication: W.NEUTRAL,
  utility: W.NEUTRAL,
  browser: W.NEUTRAL, // envelope only — never counted, see counts_toward_total
  entertainment: W.PASSIVE,
  gaming: W.DISTRACTION,
  other: W.NEUTRAL,
};

// Matched against process_name first, then app_name. Substring match, longest
// pattern wins, so "libreoffice-calc" beats "libreoffice".
const APP_RULES = [
  ["study_tools", ["anki", "geogebra", "kalzium", "stellarium", "marble", "kturtle",
    "scratch", "sonic-pi", "mathematica", "octave", "rstudio", "zotero",
    "mendeley", "kiwix", "khan", "duolingo", "photomath", "wolfram"]],
  ["coding", ["code", "codium", "thonny", "idle", "pycharm", "intellij", "eclipse",
    "netbeans", "arduino", "gitkraken", "sublime_text", "vim", "nvim", "emacs",
    "geany", "bluej", "android-studio", "jupyter", "gnome-terminal",
    "konsole", "xterm", "lxterminal", "terminator", "bash", "python3"]],
  ["writing_docs", ["libreoffice", "soffice", "winword", "excel", "powerpnt",
    "onenote", "abiword", "gnumeric", "okular", "evince", "atril", "xreader",
    "acrobat", "acrord32", "notepad", "gedit", "kate", "mousepad", "obsidian",
    "notion", "xournal"]],
  ["creative", ["gimp", "inkscape", "krita", "blender", "kdenlive", "openshot",
    "audacity", "musescore", "shotcut", "darktable", "freecad", "tinkercad",
    "pinta", "photoshop", "illustrator", "figma"]],
  ["communication", ["thunderbird", "evolution", "zoom", "teams", "skype", "webex",
    "meet", "slack", "outlook", "mail"]],
  ["utility", ["nautilus", "dolphin", "thunar", "pcmanfm", "file-roller",
    "gnome-control-center", "systemsettings", "gparted", "synaptic",
    "software-center", "gnome-software", "task", "calculator", "galculator",
    "gnome-calculator", "settings", "explorer"]],
  ["entertainment", ["vlc", "mpv", "totem", "spotify", "rhythmbox", "netflix",
    "primevideo", "disney", "hotstar", "kodi", "plex", "smplayer",
    "mediaplayer", "groove", "wmplayer", "youtube-music"]],
  ["gaming", ["steam", "lutris", "minecraft", "roblox", "epicgames", "battle.net",
    "origin", "supertux", "supertuxkart", "0ad", "wesnoth", "openttd",
    "minetest", "freeciv", "pcsx", "dolphin-emu", "retroarch", "ppsspp",
    "gamehub", "heroic", "valheim", "fortnite", "valorant", "riotclient",
    "genshin", "pubg", "csgo", "dota", "gta"]],
];

// ─── Web taxonomy (source: 'web') ───────────────────────────────────────────
const WEB_CATEGORIES = {
  academic: W.ACADEMIC,
  reference: W.ACADEMIC,
  coding_docs: W.ACADEMIC,
  school_admin: W.CONSTRUCTIVE,
  creative_web: W.CONSTRUCTIVE,
  news: W.NEUTRAL,
  search: W.NEUTRAL,
  shopping: W.PASSIVE,
  social: W.PASSIVE,
  video_other: W.PASSIVE,
  gaming_web: W.DISTRACTION,
  other: W.NEUTRAL,
};

// Domain suffix match: a rule matches example.com and any subdomain of it.
const WEB_RULES = [
  ["academic", ["khanacademy.org", "ncert.nic.in", "nios.ac.in", "byjus.com",
    "vedantu.com", "unacademy.com", "toppr.com", "doubtnut.com",
    "physicswallah.live", "pw.live", "coursera.org", "edx.org", "udemy.com",
    "swayam.gov.in", "nptel.ac.in", "brilliant.org", "openstax.org",
    "ck12.org", "quizlet.com", "gradeup.co", "embibe.com", "meritnation.com",
    "learncbse.in", "tiwariacademy.com", "cuemath.com", "mathsisfun.com",
    "desmos.com", "geogebra.org", "wolframalpha.com", "symbolab.com",
    "socratic.org", "sciencedirect.com", "jstor.org", "arxiv.org",
    "scholar.google.com", "researchgate.net", "pubmed.ncbi.nlm.nih.gov"]],
  ["reference", ["wikipedia.org", "wiktionary.org", "britannica.com",
    "dictionary.com", "merriam-webster.com", "thesaurus.com",
    "nationalgeographic.com", "nasa.gov", "esa.int", "sciencedaily.com",
    "howstuffworks.com", "sparknotes.com", "gutenberg.org", "archive.org",
    "wikibooks.org", "wikisource.org", "省"]],
  ["coding_docs", ["stackoverflow.com", "stackexchange.com", "github.com",
    "gitlab.com", "developer.mozilla.org", "w3schools.com", "geeksforgeeks.org",
    "python.org", "docs.python.org", "nodejs.org", "npmjs.com",
    "hackerrank.com", "leetcode.com", "codeforces.com", "codechef.com",
    "replit.com", "codepen.io", "freecodecamp.org", "programiz.com",
    "tutorialspoint.com", "javatpoint.com", "realpython.com", "css-tricks.com",
    "devdocs.io", "readthedocs.io", "kaggle.com"]],
  ["school_admin", ["classroom.google.com", "docs.google.com", "drive.google.com",
    "sheets.google.com", "slides.google.com", "moodle.org", "canvas.net",
    "instructure.com", "blackboard.com", "schoology.com", "teams.microsoft.com",
    "office.com", "onedrive.live.com", "notion.so", "overleaf.com",
    "cbse.gov.in", "cisce.org", "digilocker.gov.in"]],
  ["creative_web", ["figma.com", "canva.com", "tinkercad.com", "scratch.mit.edu",
    "code.org", "makecode.microbit.org", "thingiverse.com", "behance.net",
    "soundtrap.com", "chrome-music-lab.appspot.com"]],
  ["news", ["bbc.com", "bbc.co.uk", "thehindu.com", "indianexpress.com",
    "timesofindia.indiatimes.com", "ndtv.com", "reuters.com", "apnews.com",
    "hindustantimes.com", "livemint.com", "economictimes.indiatimes.com",
    "weather.com", "accuweather.com"]],
  ["search", ["google.com", "bing.com", "duckduckgo.com", "yahoo.com",
    "ecosia.org", "startpage.com", "brave.com", "qwant.com"]],
  ["shopping", ["amazon.in", "amazon.com", "flipkart.com", "myntra.com",
    "ajio.com", "meesho.com", "ebay.com", "aliexpress.com", "shein.com",
    "nykaa.com", "swiggy.com", "zomato.com", "blinkit.com", "zepto.com"]],
  ["social", ["instagram.com", "facebook.com", "fb.com", "snapchat.com",
    "tiktok.com", "x.com", "twitter.com", "reddit.com", "discord.com",
    "pinterest.com", "tumblr.com", "threads.net", "whatsapp.com",
    "telegram.org", "web.telegram.org", "quora.com", "9gag.com",
    "imgur.com", "omegle.com", "chat.whatsapp.com"]],
  // youtube.com is listed here even though YouTube rows are scored on the watch
  // taxonomy: this is the web_category a YouTube row carries for context, and it
  // is also the fallback if the YouTube collector ever misses a row and it
  // arrives tagged platform='web'. Falling back to a video label rather than
  // 'other' keeps that failure honest instead of quietly neutral.
  ["video_other", ["netflix.com", "primevideo.com", "hotstar.com",
    "disneyplus.com", "sonyliv.com", "zee5.com", "jiocinema.com",
    "twitch.tv", "dailymotion.com", "vimeo.com", "hulu.com", "mxplayer.in",
    "crunchyroll.com", "aniwatch.to", "spotify.com", "soundcloud.com",
    "gaana.com", "wynk.in", "youtube.com", "youtu.be", "music.youtube.com"]],
  ["gaming_web", ["roblox.com", "poki.com", "crazygames.com", "miniclip.com",
    "y8.com", "friv.com", "coolmathgames.com", "addictinggames.com",
    "epicgames.com", "steampowered.com", "itch.io", "kongregate.com",
    "agar.io", "slither.io", "krunker.io", "shellshock.io", "1v1.lol",
    "now.gg", "silvergames.com", "unblockedgames.com"]],
];

// ─── Watch taxonomy (source: 'watch', YouTube only) ─────────────────────────
const WATCH_CATEGORIES = {
  lecture: W.ACADEMIC,
  tutorial: W.ACADEMIC,
  exam_prep: W.ACADEMIC,
  edutainment: W.CONSTRUCTIVE,
  music: W.NEUTRAL,
  entertainment: W.PASSIVE,
  gaming_video: W.DISTRACTION,
  shorts: W.DISTRACTION,
  other: W.NEUTRAL,
};

// Title keyword rules. Order matters: the first match wins, so the academic
// patterns are checked before the entertainment ones.
const WATCH_RULES = [
  ["lecture", ["lecture", "class ", "chapter", "ncert", "cbse", "icse", "syllabus",
    "full course", "semester", "unit ", "board exam", "physics", "chemistry",
    "biology", "mathematics", "algebra", "calculus", "trigonometry",
    "geometry", "history of", "geography", "economics", "grammar",
    "theorem", "derivation", "numericals"]],
  ["tutorial", ["tutorial", "how to", "explained", "step by step", "beginners",
    "crash course", "masterclass", "programming", "python", "java",
    "javascript", "coding", "learn ", "guide", "walkthrough", "for beginners",
    "basics of", "introduction to"]],
  ["exam_prep", ["revision", "important questions", "previous year", "pyq",
    "mock test", "sample paper", "one shot", "last minute", "jee", "neet",
    "olympiad", "board 2025", "board 2026", "solved", "solutions"]],
  ["edutainment", ["documentary", "science", "experiment", "space", "history",
    "veritasium", "kurzgesagt", "ted", "tedx", "national geographic",
    "how it works", "did you know", "facts about", "why do"]],
  ["music", ["official video", "lyrics", "song", "audio", "album", "soundtrack",
    "cover", "remix", "playlist", "music video", "bgm", "ost"]],
  ["gaming_video", ["gameplay", "walkthrough gaming", "minecraft", "roblox",
    "free fire", "pubg", "bgmi", "gta", "fortnite", "valorant", "fifa",
    "among us", "noob vs pro", "montage", "speedrun", "let's play",
    "lets play", "livestream gaming"]],
  ["entertainment", ["funny", "prank", "vlog", "reaction", "meme", "comedy",
    "roast", "challenge", "tiktok", "compilation", "fails", "asmr",
    "unboxing", "trailer", "movie clip", "web series", "episode"]],
];

// ─── Override rules from the database ───────────────────────────────────────
// Cached briefly: the classifier runs on every ingested row, and a parent
// override must still show up on the next page load rather than after a
// restart. 60 s is short enough to feel immediate and long enough that a
// 250-row batch does one query, not 250.
const RULE_CACHE_TTL_MS = 60 * 1000;
let ruleCache = null;
let ruleCacheAt = 0;

function loadOverrides() {
  const now = Date.now();
  if (ruleCache && now - ruleCacheAt < RULE_CACHE_TTL_MS) return ruleCache;
  const bySource = { app: [], web: [], watch: [] };
  try {
    const rows = db
      .prepare(
        `SELECT source, match_type, match_value, category, academic_weight, origin
           FROM activity_categories
          ORDER BY CASE origin WHEN 'parent' THEN 0 WHEN 'ai' THEN 1 ELSE 2 END,
                   LENGTH(match_value) DESC`
      )
      .all();
    for (const r of rows) {
      if (!bySource[r.source]) continue;
      bySource[r.source].push({
        type: String(r.match_type || "").toLowerCase(),
        value: String(r.match_value || "").toLowerCase(),
        category: r.category,
        weight: Number(r.academic_weight) || 0,
      });
    }
  } catch (err) {
    // A missing table must not stop ingest — seeds still classify everything.
    console.warn(`[categorize] Override rules unavailable: ${err.message}`);
  }
  ruleCache = bySource;
  ruleCacheAt = now;
  return ruleCache;
}

/** Call after writing to activity_categories so the change is visible at once. */
function invalidateRuleCache() {
  ruleCache = null;
  ruleCacheAt = 0;
}

function matchOverride(source, haystacks) {
  const rules = loadOverrides()[source] || [];
  for (const rule of rules) {
    if (!rule.value) continue;
    for (const hay of haystacks) {
      if (!hay) continue;
      const h = hay.toLowerCase();
      const hit =
        rule.type === "exact"
          ? h === rule.value
          : rule.type === "domain"
            ? h === rule.value || h.endsWith(`.${rule.value}`)
            : h.includes(rule.value);
      if (hit) return rule;
    }
  }
  return null;
}

// ─── Public classifiers ─────────────────────────────────────────────────────

function bestSubstringMatch(rules, haystacks) {
  let best = null;
  for (const [category, patterns] of rules) {
    for (const p of patterns) {
      for (const hay of haystacks) {
        if (hay && hay.includes(p) && (!best || p.length > best.length)) {
          best = { category, length: p.length, get 0() { return p; } };
          best.length = p.length;
        }
      }
    }
  }
  return best ? best.category : null;
}

/**
 * Classify a foreground app session.
 * Browsers are forced to 'browser' regardless of any rule: their time is an
 * envelope for the web ledger, and letting a rule score it would double-count.
 */
function classifyApp(session) {
  if (session && Number(session.is_browser) === 1) {
    return { category: "browser", weight: APP_CATEGORIES.browser, source: "app" };
  }
  const proc = String(session?.process_name || "").toLowerCase();
  const name = String(session?.app_name || "").toLowerCase();
  const title = String(session?.window_title || "").toLowerCase();

  const override = matchOverride("app", [proc, name]);
  if (override) {
    return { category: override.category, weight: override.weight, source: "app" };
  }
  // The window title is consulted last and only to break a tie into a *more*
  // specific academic bucket — never to downgrade, so a browser-like title in
  // a code editor cannot turn study time into distraction.
  let category = bestSubstringMatch(APP_RULES, [proc, name]);
  if (!category) category = bestSubstringMatch(APP_RULES, [title]);
  if (!category) category = "other";
  const weight = APP_CATEGORIES[category] ?? 0;
  return { category, weight, source: "app" };
}

function domainMatches(domain, suffix) {
  return domain === suffix || domain.endsWith(`.${suffix}`);
}

function bestDomainMatch(domain) {
  let best = null;
  for (const [category, suffixes] of WEB_RULES) {
    for (const s of suffixes) {
      if (domainMatches(domain, s) && (!best || s.length > best.len)) {
        best = { category, len: s.length };
      }
    }
  }
  return best ? best.category : null;
}

/**
 * Classify one browser visit.
 *
 * Returns BOTH categories so the two ledgers stay separable in the UI:
 *   { source: 'watch', web_category: 'video_other', watch_category: 'lecture' }
 * A YouTube row is scored on its watch_category; its web_category is recorded
 * for context only and is never summed into the web totals.
 */
function classifyWeb(visit) {
  const domain = String(visit?.domain || "").toLowerCase().replace(/^www\./, "");
  const title = String(visit?.title || "").toLowerCase();
  const isYouTube = String(visit?.platform || "web").toLowerCase() === "youtube";

  const override = matchOverride("web", [domain]);
  let webCategory = override
    ? override.category
    : bestDomainMatch(domain) || "other";
  let webWeight = override ? override.weight : (WEB_CATEGORIES[webCategory] ?? 0);

  if (!isYouTube) {
    return {
      source: "web",
      category: webCategory,
      weight: webWeight,
      web_category: webCategory,
      watch_category: null,
    };
  }

  // ── YouTube: its own taxonomy, its own ledger ──
  // Shorts are structural, not editorial: media_kind comes from the URL, so a
  // "physics revision" Short is still a Short. Endless 30-second clips are the
  // single biggest study-time sink and no title can talk its way out of it.
  let watchCategory;
  if (String(visit?.media_kind || "") === "short") {
    watchCategory = "shorts";
  } else {
    const titleOverride = matchOverride("watch", [title, String(visit?.media_id || "")]);
    if (titleOverride) {
      watchCategory = titleOverride.category;
      return {
        source: "watch",
        category: watchCategory,
        weight: titleOverride.weight,
        web_category: webCategory,
        watch_category: watchCategory,
      };
    }
    watchCategory = firstKeywordMatch(WATCH_RULES, title) || "other";
  }
  return {
    source: "watch",
    category: watchCategory,
    weight: WATCH_CATEGORIES[watchCategory] ?? 0,
    web_category: webCategory,
    watch_category: watchCategory,
  };
}

function firstKeywordMatch(rules, title) {
  if (!title) return null;
  for (const [category, keywords] of rules) {
    for (const k of keywords) {
      if (title.includes(k)) return category;
    }
  }
  return null;
}

module.exports = {
  W,
  band,
  APP_CATEGORIES,
  WEB_CATEGORIES,
  WATCH_CATEGORIES,
  classifyApp,
  classifyWeb,
  invalidateRuleCache,
};
