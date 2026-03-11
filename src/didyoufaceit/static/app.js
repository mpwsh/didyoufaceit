// ── pure helpers (no DOM, no Alpine deps) ─────────────────────────────────────

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
    d.toLocaleDateString("en-GB", {
      day: "2-digit",
      month: "short",
      year: "2-digit",
    }),
    d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }),
  ];
}

function rosterHtml(teams, yourTeam, you, opp) {
  const yours = (teams[yourTeam] || [])
    .map((n) => {
      if (n === you) return `<span class="text-brand">${n}</span>`;
      if (n === opp) return `<span class="text-blue">${n}</span>`;
      return n;
    })
    .join(", ");
  const theirs = Object.entries(teams)
    .filter(([t]) => t !== yourTeam)
    .flatMap(([, r]) =>
      r.map((n) => (n === opp ? `<span class="text-blue">${n}</span>` : n)),
    )
    .join(", ");
  return theirs
    ? `${yours}&nbsp;&nbsp;<span class="text-border">|</span>&nbsp;&nbsp;${theirs}`
    : yours;
}

function tournamentBadgeHtml(m) {
  if (!m.competition_type || m.competition_type === "matchmaking") return "";
  const label = m.competition_name || m.competition_type;
  return `<span class="font-mono text-[9px] tracking-[1px] uppercase text-gold bg-[#1a1200] border border-[#3a2800] px-1.5 py-0.5 rounded-sm truncate max-w-[200px]" title="${label}">🏆 ${label}</span>`;
}

// Map ID → display name
const MAP_NAMES = {
  de_dust2: "Dust2",
  de_mirage: "Mirage",
  de_inferno: "Inferno",
  de_nuke: "Nuke",
  de_ancient: "Ancient",
  de_anubis: "Anubis",
  de_vertigo: "Vertigo",
  de_train: "Train",
  de_cache: "Cache",
  de_overpass: "Overpass",
};

function mapDisplayName(id) {
  return (
    MAP_NAMES[id] ||
    id.replace("de_", "").replace(/^\w/, (c) => c.toUpperCase())
  );
}

// WR → color
function wrColor(wr) {
  if (wr >= 55) return "#22c55e";
  if (wr >= 45) return "#f59e0b";
  return "#ef4444";
}

// FACEIT elo → level (CS2 thresholds)
function eloToLevel(elo) {
  if (!elo || elo <= 0) return 0;
  if (elo < 801) return 1;
  if (elo < 951) return 2;
  if (elo < 1101) return 3;
  if (elo < 1251) return 4;
  if (elo < 1401) return 5;
  if (elo < 1551) return 6;
  if (elo < 1701) return 7;
  if (elo < 1851) return 8;
  if (elo < 2001) return 9;
  return 10;
}

function levelColor(lvl) {
  if (lvl >= 10) return "#ff5500"; // brand orange for lvl 10
  if (lvl >= 7) return "#f59e0b"; // gold
  if (lvl >= 4) return "#e8e8e8"; // white
  return "#666666"; // muted for low levels
}

// ── Alpine app ────────────────────────────────────────────────────────────────

function app() {
  return {
    // inputs
    youInput: "",
    oppInput: "",
    matchroomInput: "",

    // matchroom state
    mrMatchId: "",
    mrSearching: false,
    mrError: "",
    mrStatus: "Connecting…",
    mrFactions: {},
    mrMapPool: [],
    mrPickedMap: null,
    mrDone: false,

    // search state
    searching: false,
    statusMsg: "Connecting…",
    progressChecked: 0,
    progressTotal: 0,
    foundCount: 0,

    // results
    you: "",
    opp: "",
    enemies: [],
    teammates: [],
    errorMsg: "",
    cacheNotice: null,

    // filters
    filterMap: "all",
    filterYear: "all",

    // ── computed (player vs player) ──────────────────────────────────────────

    get showResults() {
      return this.enemies.length > 0 || this.teammates.length > 0;
    },
    get hasAnyResults() {
      return (
        this.showResults || this.mrDone || this.mrSearching || this.mrError
      );
    },
    get availableMaps() {
      const maps = new Set(
        [...this.enemies, ...this.teammates].map((m) => m.map).filter(Boolean),
      );
      return ["all", ...Array.from(maps).sort()];
    },
    get availableYears() {
      const years = new Set(
        [...this.enemies, ...this.teammates]
          .map((m) =>
            m.started_at ? new Date(m.started_at * 1000).getFullYear() : null,
          )
          .filter(Boolean),
      );
      return ["all", ...Array.from(years).sort((a, b) => b - a)];
    },
    _passes(m) {
      if (this.filterMap !== "all" && m.map !== this.filterMap) return false;
      if (
        this.filterYear !== "all" &&
        (!m.started_at ||
          new Date(m.started_at * 1000).getFullYear() !==
            parseInt(this.filterYear))
      )
        return false;
      return true;
    },
    get filteredEnemies() {
      return [...this.enemies]
        .filter((m) => this._passes(m))
        .sort((a, b) => b.started_at - a.started_at);
    },
    get filteredTeammates() {
      return [...this.teammates]
        .filter((m) => this._passes(m))
        .sort((a, b) => b.started_at - a.started_at);
    },
    get youWins() {
      return this.filteredEnemies.filter((m) => scoreInfo(m.score).win === true)
        .length;
    },
    get oppWins() {
      return this.filteredEnemies.filter(
        (m) => scoreInfo(m.score).win === false,
      ).length;
    },
    get draws() {
      return this.filteredEnemies.filter((m) => scoreInfo(m.score).win === null)
        .length;
    },
    get tmWins() {
      return this.filteredTeammates.filter(
        (m) => scoreInfo(m.score).win === true,
      ).length;
    },
    get tmLosses() {
      return this.filteredTeammates.filter(
        (m) => scoreInfo(m.score).win === false,
      ).length;
    },
    get tmDraws() {
      return this.filteredTeammates.filter(
        (m) => scoreInfo(m.score).win === null,
      ).length;
    },
    get tmWr() {
      const t = this.tmWins + this.tmLosses;
      return t > 0 ? Math.round((this.tmWins / t) * 100) : 0;
    },
    get progressPct() {
      return this.progressTotal > 0
        ? Math.min(
            100,
            Math.round((this.progressChecked / this.progressTotal) * 100),
          )
        : 0;
    },
    get filteredMapWins() {
      const mw = {};
      for (const m of this.filteredTeammates) {
        const key = m.map || "unknown";
        if (!mw[key]) mw[key] = { w: 0, l: 0 };
        const { win } = scoreInfo(m.score);
        if (win === true) mw[key].w++;
        else if (win === false) mw[key].l++;
      }
      return mw;
    },
    get bestMap() {
      let best = null,
        bestWr = -1;
      for (const [map, { w, l }] of Object.entries(this.filteredMapWins)) {
        if (w + l < 2) continue;
        const wr = w / (w + l);
        if (wr > bestWr) {
          best = map;
          bestWr = wr;
        }
      }
      return best;
    },
    get worstMap() {
      let worst = null,
        worstWr = 2;
      for (const [map, { w, l }] of Object.entries(this.filteredMapWins)) {
        if (w + l < 2 || map === this.bestMap) continue;
        const wr = w / (w + l);
        if (wr < worstWr) {
          worst = map;
          worstWr = wr;
        }
      }
      return worst;
    },
    get bestLineups() {
      const counts = {};
      for (const m of this.teammates) {
        const ids = ((m.teams_ids || m.teams)[m.your_team] || [])
          .slice()
          .sort();
        const key = ids.join("|");
        const displayRoster = m.teams[m.your_team] || [];
        if (!counts[key])
          counts[key] = { roster: displayRoster, w: 0, l: 0, d: 0 };
        const { win } = scoreInfo(m.score);
        if (win === true) counts[key].w++;
        else if (win === false) counts[key].l++;
        else counts[key].d++;
      }
      return Object.values(counts)
        .filter((c) => c.w + c.l + c.d >= 2)
        .sort((a, b) => {
          const wrA = a.w + a.l > 0 ? a.w / (a.w + a.l) : 0;
          const wrB = b.w + b.l > 0 ? b.w / (b.w + b.l) : 0;
          return wrB !== wrA ? wrB - wrA : b.w + b.l + b.d - (a.w + a.l + a.d);
        })
        .slice(0, 5);
    },

    // ── Matchroom computed ──────────────────────────────────────────────────

    // Perspective: 0 = sort by advantage for first faction, 1 = second
    mrPerspective: 0,

    mrMapName(id) {
      return mapDisplayName(id);
    },

    mrWrColor(wr) {
      return wrColor(wr);
    },

    mrLevelColor(lvl) {
      return levelColor(lvl);
    },

    // For a faction + map, calculate each player's WRc
    mrPlayerContribs(faction, mapId) {
      const players = this.mrFactions[faction]?.players || [];
      const totalMatches = players.reduce(
        (s, p) => s + (p.map_stats?.[mapId]?.matches || 0),
        0,
      );
      if (!totalMatches) return [];
      return players
        .filter((p) => p.map_stats?.[mapId]?.matches > 0)
        .map((p) => {
          const ms = p.map_stats[mapId];
          const wrc =
            totalMatches > 0 ? Math.round((ms.wins / totalMatches) * 100) : 0;
          return {
            nickname: p.nickname,
            wins: ms.wins,
            matches: ms.matches,
            wr: ms.wr,
            wrc,
            elo: p.elo || 0,
            level: p.skill_level || eloToLevel(p.elo),
          };
        })
        .sort((a, b) => b.wrc - a.wrc);
    },

    // Team depth: how many players have 20+ matches on a map
    mrTeamDepth(faction, mapId) {
      const players = this.mrFactions[faction]?.players || [];
      let experienced = 0;
      let withData = 0;
      for (const p of players) {
        const ms = p.map_stats?.[mapId];
        if (ms && ms.matches > 0) {
          withData++;
          if (ms.matches >= 20) experienced++;
        }
      }
      return { experienced, withData, total: players.length };
    },

    // All maps that have data from player stats (union across both teams)
    get mrAllMaps() {
      const maps = new Set();
      for (const f of Object.values(this.mrFactions)) {
        for (const p of f.players || []) {
          for (const mapId of Object.keys(p.map_stats || {})) {
            maps.add(mapId);
          }
        }
      }
      return Array.from(maps).sort();
    },

    // Team WR for a given faction + map (computed from player stats)
    mrTeamWr(faction, mapId) {
      const players = this.mrFactions[faction]?.players || [];
      let totalWins = 0,
        totalMatches = 0;
      for (const p of players) {
        const ms = p.map_stats?.[mapId];
        if (ms) {
          totalWins += ms.wins;
          totalMatches += ms.matches;
        }
      }
      if (totalMatches === 0) return null;
      return Math.round((totalWins / totalMatches) * 100);
    },

    // Map recommendation: sorted by advantage for the selected perspective team
    // Returns [{id, f1wr, f2wr, delta, f1depth, f2depth, f1contribs, f2contribs}]
    get mrMapRecommendation() {
      const factionKeys = Object.keys(this.mrFactions);
      if (factionKeys.length < 2) return [];
      const [f1, f2] = factionKeys;

      const rows = [];
      for (const mapId of this.mrAllMaps) {
        const f1wr = this.mrTeamWr(f1, mapId);
        const f2wr = this.mrTeamWr(f2, mapId);
        if (f1wr === null && f2wr === null) continue;
        // Delta is always from f1's perspective in raw data
        const rawDelta = (f1wr || 0) - (f2wr || 0);
        rows.push({
          id: mapId,
          f1wr: f1wr !== null ? f1wr : null,
          f2wr: f2wr !== null ? f2wr : null,
          rawDelta: rawDelta,
          // delta flipped based on perspective
          delta: this.mrPerspective === 0 ? rawDelta : -rawDelta,
          f1depth: this.mrTeamDepth(f1, mapId),
          f2depth: this.mrTeamDepth(f2, mapId),
          f1contribs: this.mrPlayerContribs(f1, mapId),
          f2contribs: this.mrPlayerContribs(f2, mapId),
        });
      }
      // Sort by delta descending (best maps for selected perspective team first)
      rows.sort((a, b) => b.delta - a.delta);
      return rows;
    },

    // Which faction index is currently the "advantage" perspective
    get mrActiveTeamIdx() {
      return this.mrPerspective;
    },
    get mrOtherTeamIdx() {
      return this.mrPerspective === 0 ? 1 : 0;
    },

    get mrFactionKeys() {
      return Object.keys(this.mrFactions);
    },

    // ── init ──────────────────────────────────────────────────────────────────

    init() {
      const p = new URLSearchParams(window.location.search);
      const room = p.get("room")?.trim();
      const a = p.get("a")?.trim();
      const b = p.get("b")?.trim();
      if (room) {
        this.matchroomInput = room;
        this.$nextTick(() => this.doSearch());
      } else {
        if (a) this.youInput = a;
        if (b) this.oppInput = b;
        if (a && b) this.$nextTick(() => this.doSearch());
      }
    },

    // ── Reset helpers ────────────────────────────────────────────────────────

    _resetMatchroom() {
      if (this._mrEs) {
        this._mrEs.close();
        this._mrEs = null;
      }
      this.mrMatchId = "";
      this.mrSearching = false;
      this.mrError = "";
      this.mrStatus = "Connecting…";
      this.mrFactions = {};
      this.mrMapPool = [];
      this.mrPickedMap = null;
      this.mrDone = false;
      this.mrPerspective = 0;
    },

    _resetPlayerSearch() {
      if (this._es) {
        this._es.close();
        this._es = null;
      }
      this.searching = false;
      this.statusMsg = "Connecting…";
      this.progressChecked = 0;
      this.progressTotal = 0;
      this.foundCount = 0;
      this.you = "";
      this.opp = "";
      this.enemies = [];
      this.teammates = [];
      this.errorMsg = "";
      this.cacheNotice = null;
      this.filterMap = "all";
      this.filterYear = "all";
    },

    newSearch() {
      this._resetMatchroom();
      this._resetPlayerSearch();
      this.matchroomInput = "";
      // Clear URL params
      const url = new URL(window.location);
      url.search = "";
      window.history.replaceState({}, "", url);
    },

    // ── Match card HTML ──────────────────────────────────────────────────────

    matchCard(m) {
      const { label, cls, win } = scoreInfo(m.score);
      const resultText = win === true ? "win" : win === false ? "loss" : "draw";
      const resultColor =
        win === true ? "text-win" : win === false ? "text-lose" : "text-muted";
      const [d, t] = fmtDate(m.started_at);
      return `
        <a href="${m.faceit_url || "#"}" target="_blank" rel="noopener"
           class="bg-surface border border-border px-5 py-3.5 grid grid-cols-[72px_1fr_auto] gap-4 items-center transition-colors hover:border-brand no-underline text-[#e8e8e8] card-enter block">
          <div class="font-mono text-[11px] text-muted leading-relaxed">${d}<br>${t}</div>
          <div class="flex flex-col gap-1 min-w-0">
            <div class="flex items-center gap-2">
              <span class="font-display text-lg tracking-widest">${m.map || "UNKNOWN"}</span>
              ${tournamentBadgeHtml(m)}
            </div>
            <div class="font-mono text-[11px] text-muted truncate">${rosterHtml(m.teams, m.your_team, this.you, this.opp)}</div>
          </div>
          <div class="text-right flex-shrink-0">
            <div class="font-display text-[22px] tracking-widest leading-none ${cls}">${label}</div>
            <div class="font-mono text-[10px] tracking-[1px] uppercase mt-0.5 ${resultColor}">${resultText}</div>
          </div>
        </a>`;
    },

    // ── SSE: Player vs Player search ─────────────────────────────────────────

    _es: null,

    doSearch() {
      // If matchroom input has content, run matchroom mode
      if (this.matchroomInput.trim()) {
        this.doMatchroom();
        return;
      }
      const you = this.youInput.trim();
      const opp = this.oppInput.trim();
      if (!you || !opp) {
        alert("Please enter both nicknames.");
        return;
      }

      // Reset matchroom state when starting player search
      this._resetMatchroom();

      if (this._es) {
        this._es.close();
        this._es = null;
      }

      // reset player search
      this.errorMsg = "";
      this.cacheNotice = null;
      this.enemies = [];
      this.teammates = [];
      this.filterMap = "all";
      this.filterYear = "all";

      this.you = you;
      this.opp = opp;
      this.searching = true;

      // make the URL shareable
      const url = new URL(window.location);
      url.searchParams.delete("room");
      url.searchParams.set("a", you);
      url.searchParams.set("b", opp);
      window.history.replaceState({}, "", url);
      this.statusMsg = "Connecting…";
      this.progressChecked = 0;
      this.progressTotal = 0;
      this.foundCount = 0;

      const es = new EventSource(
        `/api/search?you=${encodeURIComponent(you)}&opponent=${encodeURIComponent(opp)}`,
      );
      this._es = es;

      es.onmessage = (e) => {
        let ev;
        try {
          ev = JSON.parse(e.data);
        } catch {
          return;
        }

        switch (ev.type) {
          case "status":
            this.statusMsg = ev.msg;
            break;

          case "progress":
            if (ev.total) this.progressTotal = ev.total;
            if (ev.checked) this.progressChecked = ev.checked;
            if (ev.found > 0) {
              this.foundCount = ev.found;
              this.statusMsg = `Found ${ev.found} match${ev.found !== 1 ? "es" : ""} so far…`;
            }
            break;

          case "match":
            this.foundCount++;
            this._ingest(ev);
            break;

          case "done":
            this.searching = false;
            this._finalize(ev);
            es.close();
            this._es = null;
            break;

          case "error":
            this.searching = false;
            this.errorMsg = ev.msg;
            es.close();
            this._es = null;
            break;
        }
      };

      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) return;
        this.searching = false;
        this.errorMsg = "Connection lost. Please try again.";
        es.close();
        this._es = null;
      };
    },

    _ingest(m) {
      if (m.same_team) {
        this.teammates.push(m);
      } else {
        this.enemies.push(m);
      }
    },

    _finalize(ev) {
      if (!this.enemies.length && !this.teammates.length) {
        this.errorMsg = `No matches found between ${this.you} and ${this.opp}.`;
        return;
      }
      const hit = ev.cached;
      const msg = hit
        ? `loaded from cache · ${ev.total_checked.toLocaleString()} matches indexed`
        : `scan complete · ${ev.total_checked.toLocaleString()} indexed · ${ev.new_fetched.toLocaleString()} fetched from api`;
      this.cacheNotice = { hit, msg };
    },

    // ── SSE: Matchroom ───────────────────────────────────────────────────────
    _mrEs: null,

    doMatchroom() {
      let raw = this.matchroomInput.trim();
      if (!raw) return;

      // Reset player search state when starting matchroom
      this._resetPlayerSearch();

      if (raw.includes("room/"))
        raw = raw.split("room/").pop().split("?")[0].split("/")[0].trim();
      this.mrMatchId = raw;
      this.mrSearching = true;
      this.mrError = "";
      this.mrStatus = "Connecting…";
      this.mrFactions = {};
      this.mrMapPool = [];
      this.mrPickedMap = null;
      this.mrDone = false;

      // make the URL shareable
      const url = new URL(window.location);
      url.searchParams.delete("a");
      url.searchParams.delete("b");
      url.searchParams.set("room", raw);
      window.history.replaceState({}, "", url);

      if (this._mrEs) {
        this._mrEs.close();
        this._mrEs = null;
      }

      const es = new EventSource(
        `/api/matchroom?id=${encodeURIComponent(raw)}`,
      );
      this._mrEs = es;

      es.onmessage = (e) => {
        let ev;
        try {
          ev = JSON.parse(e.data);
        } catch {
          return;
        }

        switch (ev.type) {
          case "status":
            this.mrStatus = ev.msg;
            break;

          case "player": {
            const factions = { ...this.mrFactions };
            if (!factions[ev.faction]) {
              factions[ev.faction] = {
                name: ev.faction,
                players: [],
                map_scores: {},
              };
            }
            factions[ev.faction] = {
              ...factions[ev.faction],
              players: [
                ...factions[ev.faction].players,
                {
                  player_id: ev.player_id,
                  nickname: ev.nickname,
                  elo: ev.elo,
                  skill_level: ev.skill_level || 0,
                  map_stats: ev.map_stats,
                },
              ],
            };
            this.mrFactions = factions;
            break;
          }

          case "summary": {
            const updated = { ...this.mrFactions };
            for (const [faction, data] of Object.entries(ev.factions)) {
              if (!updated[faction]) updated[faction] = { players: [] };
              updated[faction] = {
                ...updated[faction],
                name: data.name,
                map_scores: data.map_scores,
              };
            }
            this.mrFactions = updated;
            this.mrMapPool = ev.map_pool;
            this.mrPickedMap = ev.picked_map;
            break;
          }

          case "done":
            this.mrSearching = false;
            this.mrDone = true;
            es.close();
            this._mrEs = null;
            break;

          case "error":
            this.mrSearching = false;
            this.mrError = ev.msg;
            es.close();
            this._mrEs = null;
            break;
        }
      };

      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) return;
        this.mrSearching = false;
        this.mrError = "Connection lost. Please try again.";
        es.close();
        this._mrEs = null;
      };
    },
  };
}
