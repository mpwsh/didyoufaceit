// ── shared.js — pure helpers used across all pages ──────────────────────────

function scoreInfo(scoreStr) {
  if (!scoreStr || scoreStr === "N/A")
    return { label: "N/A", cls: "text-[#888]", win: null };
  const [a, b] = scoreStr.split("/").map((s) => parseInt(s.trim(), 10));
  if (isNaN(a) || isNaN(b))
    return { label: scoreStr, cls: "text-[#888]", win: null };
  if (a > b) return { label: scoreStr, cls: "text-win", win: true };
  if (a < b) return { label: scoreStr, cls: "text-lose", win: false };
  return { label: scoreStr, cls: "text-[#888]", win: null };
}

function fmtDate(unix) {
  if (!unix) return ["—", ""];
  const d = new Date(unix * 1000);
  return [
    d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "2-digit" }),
    d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }),
  ];
}

const MAP_NAMES = {
  de_dust2: "Dust2", de_mirage: "Mirage", de_inferno: "Inferno",
  de_nuke: "Nuke", de_ancient: "Ancient", de_anubis: "Anubis",
  de_vertigo: "Vertigo", de_train: "Train", de_cache: "Cache", de_overpass: "Overpass",
};

function mapDisplayName(id) {
  return MAP_NAMES[id] || id.replace("de_", "").replace(/^\w/, (c) => c.toUpperCase());
}

function wrColor(wr) {
  if (wr >= 55) return "#22c55e";
  if (wr >= 45) return "#f59e0b";
  return "#ef4444";
}

function eloToLevel(elo) {
  if (!elo || elo <= 0) return 0;
  if (elo <= 500) return 1; if (elo <= 750) return 2; if (elo <= 900) return 3;
  if (elo <= 1050) return 4; if (elo <= 1200) return 5; if (elo <= 1350) return 6;
  if (elo <= 1530) return 7; if (elo <= 1750) return 8; if (elo <= 2000) return 9;
  return 10;
}

function levelColor(lvl) {
  if (lvl >= 10) return "#ff2248";
  if (lvl >= 8) return "#ff6c20";
  if (lvl >= 4) return "#ffcd25";
  if (lvl >= 2) return "#47e36e";
  return "#CCCCCC";
}

function kdColor(kd) {
  if (kd >= 1.2) return "#22c55e";
  if (kd >= 1.0) return "#f59e0b";
  return "#ef4444";
}

function ratingColor(r) {
  if (r >= 1.15) return "#22c55e";
  if (r >= 0.95) return "#f59e0b";
  return "#ef4444";
}

function playerMapStrength(wins, matches, kd, level, rating) {
  if (matches === 0) return 0;
  const confidence = 0.3 + 0.7 * Math.min(matches / 80, 1);
  const r = rating > 0 ? rating : kd > 0 ? 0.5 + 0.4 * kd : 0.5;
  return Math.round(r * confidence * 100);
}

function wilsonScore(wins, total) {
  if (total === 0) return 0;
  const z = 1.645, p = wins / total;
  const denom = 1 + (z * z) / total;
  const center = p + (z * z) / (2 * total);
  const spread = z * Math.sqrt((p * (1 - p)) / total + (z * z) / (4 * total * total));
  return (center - spread) / denom;
}

function friendlyError(msg) {
  if (!msg) return "Something went wrong.";
  if (msg.includes("404")) return "Player or match not found. Check the spelling and try again.";
  if (msg.includes("401") || msg.includes("403")) return "API authentication error. Please try again later.";
  if (msg.includes("503") || msg.includes("502")) return "FACEIT servers are busy. Please try again in a moment.";
  if (msg.includes("429")) return "Too many requests. Please wait a moment and try again.";
  if (msg.includes("Connection lost")) return msg;
  return msg.replace(/\{.*\}/s, "").replace(/FACEIT API \d+ on \/[^\s:]+:?\s*/, "").trim() || "Something went wrong.";
}

function fmtBanReason(reason) {
  if (!reason) return "Unknown";
  return reason.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function normalizeBanReason(reason) {
  if (!reason) return "unknown";
  const r = reason.toLowerCase().trim();
  if (r.includes("smurf") || r.includes("multiple accounts")) return "smurfing";
  if (r.includes("cheat")) return "cheating";
  if (r.includes("verbal abuse") || r.includes("offensive language")) return "verbal abuse";
  if (r.includes("unsportsmanlike") || r.includes("griefing") || r.includes("ghosting")) return "unsportsmanlike conduct";
  if (r.includes("platform abuse")) return "platform abuse";
  return r.replace(/_/g, " ").replace(/\s+/g, " ").trim();
}

function banReasonLabel(normalized) {
  if (!normalized) return "Unknown";
  return normalized.replace(/\b\w/g, (c) => c.toUpperCase());
}

function fmtBanDate(dateStr) {
  if (!dateStr) return "—";
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
  } catch { return dateStr; }
}

// Inline SVG for the crosshair logo — used in headers
const LOGO_CROSSHAIR = `<svg class="inline-block size-[0.6em] mx-[0.1em] align-middle" viewBox="0 0 64 64" fill="#ff5500"><rect x="28" y="8" width="8" height="18" rx="1"/><rect x="28" y="38" width="8" height="18" rx="1"/><rect x="8" y="28" width="18" height="8" rx="1"/><rect x="38" y="28" width="18" height="8" rx="1"/></svg>`;
