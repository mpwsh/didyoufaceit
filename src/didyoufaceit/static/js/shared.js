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

// Display-name overrides for map ids whose name isn't just the id without "de_",
// title-cased (e.g. { cs_office: "Office" }). Every current map follows the rule.
const MAP_NAMES = {};

function mapDisplayName(id) {
  if (!id) return "";
  if (id === "unknown") return "Unknown";
  // Best-of-3 rows carry every map: "de_nuke / de_inferno / de_mirage"
  if (id.includes(" / ")) return id.split(" / ").map(mapDisplayName).join(" / ");
  return MAP_NAMES[id] || id.replace(/^de_/, "").replace(/^\w/, (c) => c.toUpperCase());
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

function wilsonScore(wins, total) {
  if (total === 0) return 0;
  const z = 1.645, p = wins / total;
  const denom = 1 + (z * z) / total;
  const center = p + (z * z) / (2 * total);
  const spread = z * Math.sqrt((p * (1 - p)) / total + (z * z) / (4 * total * total));
  return (center - spread) / denom;
}

// Turn a backend error event into a message for the page. `status` is the HTTP
// status the backend saw (or null); only when it's missing do we sniff the text.
function friendlyError(msg, status) {
  if (typeof status === "number") {
    if (status === 404) return "Player or match not found. Check the spelling and try again.";
    if (status === 401 || status === 403) return "API authentication error. Please try again later.";
    if (status === 429) return "Too many requests. Please wait a moment and try again.";
    if (status === 502 || status === 503 || status === 504) return "FACEIT servers are busy. Please try again in a moment.";
    if (status === 400) return msg || "Invalid input. Check the spelling and try again.";
  }
  if (!msg) return "Something went wrong.";
  if (typeof status !== "number") {
    if (msg.includes("404")) return "Player or match not found. Check the spelling and try again.";
    if (msg.includes("401") || msg.includes("403")) return "API authentication error. Please try again later.";
    if (msg.includes("503") || msg.includes("502")) return "FACEIT servers are busy. Please try again in a moment.";
    if (msg.includes("429")) return "Too many requests. Please wait a moment and try again.";
  }
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

// One faction's roster as [{ name, id }]: `teams` holds nicknames and `teams_ids`
// the player ids for the same faction, in the same order.
function factionRoster(m, faction) {
  const names = m.teams?.[faction] || [];
  const ids = m.teams_ids?.[faction] || [];
  return names.map((name, i) => ({ name, id: ids[i] || "" }));
}

// True when a roster entry is the given player: by id when both sides have one,
// otherwise by case-insensitive nickname.
function isSamePlayer(p, id, nickname) {
  if (id && p.id) return p.id === id;
  return !!nickname && p.name.toLowerCase() === nickname.toLowerCase();
}

// Match card helpers shared by the vs and ban scan pages (spread into the Alpine apps).
const matchCardHelpers = {
  matchDate(m) {
    return fmtDate(m.started_at);
  },
  matchScore(m) {
    return scoreInfo(m.score);
  },
  matchMap(m) {
    return mapDisplayName(m.map || "unknown");
  },
  matchIsTournament(m) {
    return m.competition_type && m.competition_type !== "matchmaking";
  },
  matchTournamentLabel(m) {
    return m.competition_name || m.competition_type;
  },
};
