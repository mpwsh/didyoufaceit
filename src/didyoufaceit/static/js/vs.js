// ── vs.js — Alpine app for player vs player results ─────────────────────────

function vsApp() {
  return {
    ...matchCardHelpers,

    // search state
    searching: false,
    statusMsg: "Connecting…",
    progressChecked: 0,
    progressTotal: 0,
    foundCount: 0,

    // results
    you: "",
    opp: "",
    youId: "",
    oppId: "",
    enemies: [],
    teammates: [],
    errorMsg: "",

    // filters
    filterMap: "all",
    filterYear: "all",
    filterMode: "all",

    // pagination
    enemiesShown: 25,
    teammatesShown: 25,

    // Derived values, rebuilt by recompute() whenever the matches or the filters change.
    stats: {
      availableMaps: ["all"],
      availableYears: ["all"],
      availableModes: ["all"],
      filteredEnemies: [],
      filteredTeammates: [],
      youWins: 0,
      oppWins: 0,
      draws: 0,
      tmWins: 0,
      tmLosses: 0,
      tmDraws: 0,
      tmWr: 0,
      mapWins: {},
      bestMap: null,
      worstMap: null,
      bestLineups: [],
    },

    // SSE
    _es: null,
    _pendingMatches: [],
    _flushTimer: null,
    _done: false,

    // dedup tracking sets
    _enemyIds: new Set(),
    _teammateIds: new Set(),

    get showResults() {
      return this.enemies.length > 0 || this.teammates.length > 0;
    },
    get progressPct() {
      return this.progressTotal > 0
        ? Math.min(
            100,
            Math.round((this.progressChecked / this.progressTotal) * 100),
          )
        : 0;
    },

    _modeOf(m) {
      if (!m.competition_type || m.competition_type === "matchmaking")
        return "matchmaking";
      return m.competition_name || m.competition_type;
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
      if (this.filterMode !== "all" && this._modeOf(m) !== this.filterMode)
        return false;
      return true;
    },

    get pagedEnemies() {
      return this.stats.filteredEnemies.slice(0, this.enemiesShown);
    },
    get pagedTeammates() {
      return this.stats.filteredTeammates.slice(0, this.teammatesShown);
    },

    // ── Derived stats ───────────────────────────────────────────────────────
    recompute() {
      const all = [...this.enemies, ...this.teammates];
      const byDate = (a, b) => b.started_at - a.started_at;
      const filteredEnemies = this.enemies
        .filter((m) => this._passes(m))
        .sort(byDate);
      const filteredTeammates = this.teammates
        .filter((m) => this._passes(m))
        .sort(byDate);
      const e = this._tally(filteredEnemies);
      const t = this._tally(filteredTeammates);
      const mapWins = this._mapWins(filteredTeammates);
      const bestMap = this._bestMap(mapWins);
      const years = all
        .map((m) =>
          m.started_at ? new Date(m.started_at * 1000).getFullYear() : null,
        )
        .filter(Boolean);
      this.stats = {
        availableMaps: [
          "all",
          ...[...new Set(all.map((m) => m.map).filter(Boolean))].sort(),
        ],
        availableYears: ["all", ...[...new Set(years)].sort((a, b) => b - a)],
        availableModes: [
          "all",
          ...[...new Set(all.map((m) => this._modeOf(m)))].sort(),
        ],
        filteredEnemies,
        filteredTeammates,
        youWins: e.w,
        oppWins: e.l,
        draws: e.d,
        tmWins: t.w,
        tmLosses: t.l,
        tmDraws: t.d,
        tmWr: t.w + t.l > 0 ? Math.round((t.w / (t.w + t.l)) * 100) : 0,
        mapWins,
        bestMap,
        worstMap: this._worstMap(mapWins, bestMap),
        bestLineups: this._bestLineups(filteredTeammates),
      };
    },

    _tally(matches) {
      let w = 0,
        l = 0,
        d = 0;
      for (const m of matches) {
        const { win } = scoreInfo(m.score);
        if (win === true) w++;
        else if (win === false) l++;
        else d++;
      }
      return { w, l, d };
    },

    _mapWins(teammates) {
      const mw = {};
      for (const m of teammates) {
        const key = m.map || "unknown";
        // Per-map records only: skip unknown maps and best-of-3 series ("a / b / c")
        if (key === "unknown" || key.includes(" / ")) continue;
        if (!mw[key]) mw[key] = { w: 0, l: 0 };
        const { win } = scoreInfo(m.score);
        if (win === true) mw[key].w++;
        else if (win === false) mw[key].l++;
      }
      return mw;
    },

    // Best map: highest Wilson lower bound (confidently good).
    _bestMap(mapWins) {
      let best = null,
        bestScore = -1;
      for (const [map, { w, l }] of Object.entries(mapWins)) {
        if (w + l < 5) continue;
        const ws = wilsonScore(w, w + l);
        if (ws > bestScore) {
          best = map;
          bestScore = ws;
        }
      }
      return best;
    },

    // Worst map: lowest Wilson upper bound (confidently bad), so a short
    // unlucky run doesn't outrank a long losing record.
    _worstMap(mapWins, bestMap) {
      let worst = null,
        worstScore = 2;
      for (const [map, { w, l }] of Object.entries(mapWins)) {
        if (w + l < 5 || map === bestMap) continue;
        const upper = 1 - wilsonScore(l, w + l);
        if (upper < worstScore) {
          worst = map;
          worstScore = upper;
        }
      }
      return worst;
    },

    _bestLineups(teammates) {
      const counts = {};
      for (const m of teammates) {
        const ids = ((m.teams_ids || m.teams)[m.your_team] || [])
          .slice()
          .sort();
        const key = ids.join("|");
        if (!counts[key])
          counts[key] = {
            key,
            roster: factionRoster(m, m.your_team).map((p) => ({
              name: p.name,
              cls: this._rosterCls(p),
            })),
            w: 0,
            l: 0,
            d: 0,
          };
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
    },

    mapStatsLabel(map) {
      const s = this.stats.mapWins[map];
      if (!s) return "";
      const wr = s.w + s.l > 0 ? Math.round((s.w / (s.w + s.l)) * 100) : 0;
      return `(${wr}% · ${s.w}W ${s.l}L)`;
    },

    _rosterCls(p) {
      if (isSamePlayer(p, this.youId, this.you)) return "text-brand";
      if (isSamePlayer(p, this.oppId, this.opp)) return "text-blue";
      return "";
    },
    matchRosterYours(m) {
      return factionRoster(m, m.your_team).map((p) => ({
        name: p.name,
        cls: this._rosterCls(p),
      }));
    },

    // ── Init ────────────────────────────────────────────────────────────────
    init() {
      this.$watch("filterMap", () => this.recompute());
      this.$watch("filterYear", () => this.recompute());
      this.$watch("filterMode", () => this.recompute());
      const p = new URLSearchParams(window.location.search);
      const a = p.get("a")?.trim();
      const b = p.get("b")?.trim();
      if (a && b) {
        this.you = a;
        this.opp = b;
        this.$nextTick(() => this._startSearch(a, b));
      }
    },

    cancelSearch() {
      if (this._es) {
        this._es.close();
        this._es = null;
      }
      this._flushMatches();
      if (this._flushTimer) {
        clearTimeout(this._flushTimer);
        this._flushTimer = null;
      }
      this.searching = false;
    },

    _startSearch(you, opp) {
      this.errorMsg = "";
      this.youId = "";
      this.oppId = "";
      this.enemies = [];
      this.teammates = [];
      this._enemyIds = new Set();
      this._teammateIds = new Set();
      this._done = false;
      this.filterMap = "all";
      this.filterYear = "all";
      this.filterMode = "all";
      this.enemiesShown = 25;
      this.teammatesShown = 25;
      this._pendingMatches = [];
      this.recompute();
      this.searching = true;
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
          case "players_resolved":
            this.youId = ev.you?.player_id || "";
            this.oppId = ev.opponent?.player_id || "";
            if (ev.you?.nickname) this.you = ev.you.nickname;
            if (ev.opponent?.nickname) this.opp = ev.opponent.nickname;
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
              teams_ids: ev.teams_ids ? Object.freeze(ev.teams_ids) : null,
              competition_type: ev.competition_type || "",
              competition_name: ev.competition_name || "",
            });
            this._pendingMatches.push(slim);
            if (!this._flushTimer) {
              this._flushTimer = setTimeout(() => {
                this._flushMatches();
                this._flushTimer = null;
              }, 500);
            }
            break;
          }
          case "done":
            this._flushMatches();
            if (this._flushTimer) {
              clearTimeout(this._flushTimer);
              this._flushTimer = null;
            }
            this.recompute();
            this.searching = false;
            this._done = true;
            this._finalize();
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
            this.errorMsg = friendlyError(ev.msg, ev.status);
            es.close();
            this._es = null;
            break;
        }
      };

      es.onerror = () => {
        // After "done" or "error" we already called es.close() — ignore the
        // onerror that fires when the server then drops the TCP connection.
        if (this._done || this.errorMsg) return;
        this._flushMatches();
        if (this._flushTimer) {
          clearTimeout(this._flushTimer);
          this._flushTimer = null;
        }
        this.searching = false;
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
        if (m.same_team) {
          if (!this._teammateIds.has(m.match_id)) {
            this._teammateIds.add(m.match_id);
            newTeammates.push(m);
          }
        } else {
          if (!this._enemyIds.has(m.match_id)) {
            this._enemyIds.add(m.match_id);
            newEnemies.push(m);
          }
        }
      }
      if (newEnemies.length) this.enemies = [...this.enemies, ...newEnemies];
      if (newTeammates.length)
        this.teammates = [...this.teammates, ...newTeammates];
      this.recompute();
    },

    _finalize() {
      if (!this.enemies.length && !this.teammates.length) {
        this.errorMsg = `No matches found between ${this.you} and ${this.opp}.`;
      }
    },
  };
}
