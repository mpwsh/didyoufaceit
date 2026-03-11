// ── pure helpers ─────────────────────────────────────────────────────────────

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

function wrColor(wr) {
  if (wr >= 55) return "#22c55e";
  if (wr >= 45) return "#f59e0b";
  return "#ef4444";
}

function eloToLevel(elo) {
  if (!elo || elo <= 0) return 0;
  const thresholds = [801, 951, 1101, 1251, 1401, 1551, 1701, 1851, 2001];
  for (let i = 0; i < thresholds.length; i++) {
    if (elo < thresholds[i]) return i + 1;
  }
  return 10;
}

function levelColor(lvl) {
  if (lvl >= 10) return "#ff5500";
  if (lvl >= 7) return "#f59e0b";
  if (lvl >= 4) return "#e8e8e8";
  return "#666666";
}

function wilsonScore(wins, total) {
  if (total === 0) return 0;
  const z = 1.645,
    p = wins / total;
  const denom = 1 + (z * z) / total;
  const center = p + (z * z) / (2 * total);
  const spread =
    z * Math.sqrt((p * (1 - p)) / total + (z * z) / (4 * total * total));
  return (center - spread) / denom;
}

function friendlyError(msg) {
  if (!msg) return "Something went wrong.";
  if (msg.includes("404"))
    return "Player or match not found. Check the spelling and try again.";
  if (msg.includes("401") || msg.includes("403"))
    return "API authentication error. Please try again later.";
  if (msg.includes("503") || msg.includes("502"))
    return "FACEIT servers are busy. Please try again in a moment.";
  if (msg.includes("429"))
    return "Too many requests. Please wait a moment and try again.";
  if (msg.includes("Connection lost")) return msg;
  return (
    msg
      .replace(/\{.*\}/s, "")
      .replace(/FACEIT API \d+ on \/[^\s:]+:?\s*/, "")
      .trim() || "Something went wrong."
  );
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
    mrPerspective: 0,

    // search state
    searching: false,
    statusMsg: "Connecting…",
    progressChecked: 0,
    progressTotal: 0,
    foundCount: 0,

    // results — raw arrays (frozen objects inside)
    you: "",
    opp: "",
    enemies: [],
    teammates: [],
    errorMsg: "",
    cacheNotice: null,

    // filters
    filterMap: "all",
    filterYear: "all",

    // pagination
    enemiesShown: 25,
    teammatesShown: 25,

    // ── CACHED computed results (only recalculated on demand, not on every SSE event) ──
    _cache: {},
    _cacheVersion: 0,

    // Bump version to invalidate cache (called after search done, or filter change)
    _invalidate() {
      this._cacheVersion++;
      this._cache = {};
    },

    // Memoized getter helper
    _memo(key, fn) {
      const v = this._cacheVersion;
      if (this._cache[key]?.v === v) return this._cache[key].r;
      const r = fn.call(this);
      this._cache[key] = { v, r };
      return r;
    },

    get showResults() {
      return this.enemies.length > 0 || this.teammates.length > 0;
    },
    get hasAnyResults() {
      return (
        this.showResults || this.mrDone || this.mrSearching || this.mrError
      );
    },
    get progressPct() {
      return this.progressTotal > 0
        ? Math.min(
            100,
            Math.round((this.progressChecked / this.progressTotal) * 100),
          )
        : 0;
    },

    get availableMaps() {
      return this._memo("availableMaps", function () {
        const maps = new Set(
          [...this.enemies, ...this.teammates]
            .map((m) => m.map)
            .filter(Boolean),
        );
        return ["all", ...Array.from(maps).sort()];
      });
    },
    get availableYears() {
      return this._memo("availableYears", function () {
        const years = new Set(
          [...this.enemies, ...this.teammates]
            .map((m) =>
              m.started_at ? new Date(m.started_at * 1000).getFullYear() : null,
            )
            .filter(Boolean),
        );
        return ["all", ...Array.from(years).sort((a, b) => b - a)];
      });
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
      return this._memo("fE", function () {
        return [...this.enemies]
          .filter((m) => this._passes(m))
          .sort((a, b) => b.started_at - a.started_at);
      });
    },
    get filteredTeammates() {
      return this._memo("fT", function () {
        return [...this.teammates]
          .filter((m) => this._passes(m))
          .sort((a, b) => b.started_at - a.started_at);
      });
    },
    get pagedEnemies() {
      return this.filteredEnemies.slice(0, this.enemiesShown);
    },
    get pagedTeammates() {
      return this.filteredTeammates.slice(0, this.teammatesShown);
    },

    // Win/loss counts — computed once from cached filteredEnemies/filteredTeammates
    get youWins() {
      return this._memo("yW", function () {
        return this.filteredEnemies.filter(
          (m) => scoreInfo(m.score).win === true,
        ).length;
      });
    },
    get oppWins() {
      return this._memo("oW", function () {
        return this.filteredEnemies.filter(
          (m) => scoreInfo(m.score).win === false,
        ).length;
      });
    },
    get draws() {
      return this._memo("dr", function () {
        return this.filteredEnemies.filter(
          (m) => scoreInfo(m.score).win === null,
        ).length;
      });
    },
    get tmWins() {
      return this._memo("tW", function () {
        return this.filteredTeammates.filter(
          (m) => scoreInfo(m.score).win === true,
        ).length;
      });
    },
    get tmLosses() {
      return this._memo("tL", function () {
        return this.filteredTeammates.filter(
          (m) => scoreInfo(m.score).win === false,
        ).length;
      });
    },
    get tmDraws() {
      return this._memo("tD", function () {
        return this.filteredTeammates.filter(
          (m) => scoreInfo(m.score).win === null,
        ).length;
      });
    },
    get tmWr() {
      const t = this.tmWins + this.tmLosses;
      return t > 0 ? Math.round((this.tmWins / t) * 100) : 0;
    },

    get filteredMapWins() {
      return this._memo("fMW", function () {
        const mw = {};
        for (const m of this.filteredTeammates) {
          const key = m.map || "unknown";
          if (!mw[key]) mw[key] = { w: 0, l: 0 };
          const { win } = scoreInfo(m.score);
          if (win === true) mw[key].w++;
          else if (win === false) mw[key].l++;
        }
        return mw;
      });
    },
    get bestMap() {
      return this._memo("bM", function () {
        let best = null,
          bestScore = -1;
        for (const [map, { w, l }] of Object.entries(this.filteredMapWins)) {
          if (w + l < 5) continue;
          const ws = wilsonScore(w, w + l);
          if (ws > bestScore) {
            best = map;
            bestScore = ws;
          }
        }
        return best;
      });
    },
    get worstMap() {
      return this._memo("wM", function () {
        let worst = null,
          worstScore = 2;
        for (const [map, { w, l }] of Object.entries(this.filteredMapWins)) {
          if (w + l < 5 || map === this.bestMap) continue;
          const ws = wilsonScore(w, w + l);
          if (ws < worstScore) {
            worst = map;
            worstScore = ws;
          }
        }
        return worst;
      });
    },
    get bestLineups() {
      return this._memo("bL", function () {
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
          .filter((c) => c.w + c.l >= 2)
          .map((c) => ({
            ...c,
            total: c.w + c.l + c.d,
            wr: c.w + c.l > 0 ? Math.round((c.w / (c.w + c.l)) * 100) : 0,
            wilson: wilsonScore(c.w, c.w + c.l),
          }))
          .sort((a, b) => b.wilson - a.wilson)
          .slice(0, 8);
      });
    },

    // Match card helpers
    matchDate(m) {
      return fmtDate(m.started_at);
    },
    matchScore(m) {
      return scoreInfo(m.score);
    },
    matchMap(m) {
      return m.map || "UNKNOWN";
    },
    matchIsTournament(m) {
      return m.competition_type && m.competition_type !== "matchmaking";
    },
    matchTournamentLabel(m) {
      return m.competition_name || m.competition_type;
    },
    matchRosterYours(m) {
      return (m.teams[m.your_team] || []).map((n) => ({
        name: n,
        cls: n === this.you ? "text-brand" : n === this.opp ? "text-blue" : "",
      }));
    },

    // ── Matchroom computed ──────────────────────────────────────────────────

    mrMapName(id) {
      return mapDisplayName(id);
    },
    mrWrColor(wr) {
      return wrColor(wr);
    },
    mrLevelColor(lvl) {
      return levelColor(lvl);
    },

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
          return {
            nickname: p.nickname,
            wins: ms.wins,
            matches: ms.matches,
            wr: ms.wr,
            wrc: Math.round((ms.wins / totalMatches) * 100),
            elo: p.elo || 0,
            level: p.skill_level || eloToLevel(p.elo),
          };
        })
        .sort((a, b) => b.wrc - a.wrc);
    },

    mrTeamDepth(faction, mapId) {
      const players = this.mrFactions[faction]?.players || [];
      let experienced = 0,
        withData = 0;
      for (const p of players) {
        const ms = p.map_stats?.[mapId];
        if (ms && ms.matches > 0) {
          withData++;
          if (ms.matches >= 20) experienced++;
        }
      }
      return { experienced, withData, total: players.length };
    },

    get mrAllMaps() {
      const maps = new Set();
      for (const f of Object.values(this.mrFactions))
        for (const p of f.players || [])
          for (const mapId of Object.keys(p.map_stats || {})) maps.add(mapId);
      return Array.from(maps).sort();
    },

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
      return totalMatches === 0
        ? null
        : Math.round((totalWins / totalMatches) * 100);
    },

    get mrMapRecommendation() {
      const fk = Object.keys(this.mrFactions);
      if (fk.length < 2) return [];
      const [f1, f2] = fk;
      const rows = [];
      for (const mapId of this.mrAllMaps) {
        const f1wr = this.mrTeamWr(f1, mapId),
          f2wr = this.mrTeamWr(f2, mapId);
        if (f1wr === null && f2wr === null) continue;
        const rawDelta = (f1wr || 0) - (f2wr || 0);
        rows.push({
          id: mapId,
          f1wr,
          f2wr,
          rawDelta,
          delta: this.mrPerspective === 0 ? rawDelta : -rawDelta,
          f1depth: this.mrTeamDepth(f1, mapId),
          f2depth: this.mrTeamDepth(f2, mapId),
          f1contribs: this.mrPlayerContribs(f1, mapId),
          f2contribs: this.mrPlayerContribs(f2, mapId),
        });
      }
      rows.sort((a, b) => b.delta - a.delta);
      return rows;
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
      this.enemiesShown = 25;
      this.teammatesShown = 25;
      this._invalidate();
    },

    newSearch() {
      this._resetMatchroom();
      this._resetPlayerSearch();
      this.matchroomInput = "";
      const url = new URL(window.location);
      url.search = "";
      window.history.replaceState({}, "", url);
    },

    // ── SSE: Player vs Player ────────────────────────────────────────────────
    _es: null,
    _pendingMatches: [], // buffer matches during streaming
    _flushTimer: null,

    doSearch() {
      if (this.matchroomInput.trim()) {
        this.doMatchroom();
        return;
      }
      const you = this.youInput.trim(),
        opp = this.oppInput.trim();
      if (!you || !opp) {
        alert("Please enter both nicknames.");
        return;
      }

      this._resetMatchroom();
      if (this._es) {
        this._es.close();
        this._es = null;
      }
      this.errorMsg = "";
      this.cacheNotice = null;
      this.enemies = [];
      this.teammates = [];
      this.filterMap = "all";
      this.filterYear = "all";
      this.enemiesShown = 25;
      this.teammatesShown = 25;
      this._invalidate();
      this._pendingMatches = [];

      this.you = you;
      this.opp = opp;
      this.searching = true;

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
          case "match": {
            this.foundCount++;
            const slim = Object.freeze({
              match_id: ev.match_id,
              started_at: ev.started_at,
              map: ev.map,
              score: ev.score,
              faceit_url: ev.faceit_url,
              your_team: ev.your_team,
              same_team: ev.same_team,
              teams: Object.freeze(ev.teams),
              teams_ids: Object.freeze(ev.teams_ids || ev.teams),
              competition_type: ev.competition_type || "",
              competition_name: ev.competition_name || "",
            });
            // Buffer matches and flush in batches to reduce reactive updates
            this._pendingMatches.push(slim);
            if (!this._flushTimer) {
              this._flushTimer = setTimeout(() => {
                this._flushMatches();
                this._flushTimer = null;
              }, 200);
            }
            break;
          }
          case "done":
            this._flushMatches(); // flush any remaining
            if (this._flushTimer) {
              clearTimeout(this._flushTimer);
              this._flushTimer = null;
            }
            this.searching = false;
            this._invalidate();
            this._finalize(ev);
            es.close();
            this._es = null;
            break;
          case "error":
            this._flushMatches();
            if (this._flushTimer) {
              clearTimeout(this._flushTimer);
              this._flushTimer = null;
            }
            this.searching = false;
            this._invalidate();
            this.errorMsg = friendlyError(ev.msg);
            es.close();
            this._es = null;
            break;
        }
      };

      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) return;
        this._flushMatches();
        if (this._flushTimer) {
          clearTimeout(this._flushTimer);
          this._flushTimer = null;
        }
        this.searching = false;
        this._invalidate();
        this.errorMsg = "Connection lost. Please try again.";
        es.close();
        this._es = null;
      };
    },

    _flushMatches() {
      if (this._pendingMatches.length === 0) return;
      const batch = this._pendingMatches;
      this._pendingMatches = [];
      const newEnemies = [],
        newTeammates = [];
      for (const m of batch) {
        if (m.same_team) newTeammates.push(m);
        else newEnemies.push(m);
      }
      if (newEnemies.length) this.enemies = [...this.enemies, ...newEnemies];
      if (newTeammates.length)
        this.teammates = [...this.teammates, ...newTeammates];
      // Don't invalidate cache during streaming — too expensive
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
            if (!factions[ev.faction])
              factions[ev.faction] = {
                name: ev.faction,
                players: [],
                map_scores: {},
              };
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
            this.mrError = friendlyError(ev.msg);
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
