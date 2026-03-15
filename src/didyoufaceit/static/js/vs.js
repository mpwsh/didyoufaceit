// ── vs.js — Alpine app for player vs player results ─────────────────────────

function vsApp() {
  return {
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
    filterMode: "all",

    // pagination
    enemiesShown: 25,
    teammatesShown: 25,

    // cache
    _cache: {},
    _cacheVersion: 0,
    _invalidate() {
      this._cacheVersion++;
      this._cache = {};
    },
    _memo(key, fn) {
      const v = this._cacheVersion;
      if (this._cache[key]?.v === v) return this._cache[key].r;
      const r = fn.call(this);
      this._cache[key] = { v, r };
      return r;
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
    get availableModes() {
      const modes = new Set(
        [...this.enemies, ...this.teammates].map((m) => {
          if (!m.competition_type || m.competition_type === "matchmaking")
            return "matchmaking";
          return m.competition_name || m.competition_type;
        }),
      );
      return ["all", ...Array.from(modes).sort()];
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
      if (this.filterMode !== "all") {
        const mode =
          !m.competition_type || m.competition_type === "matchmaking"
            ? "matchmaking"
            : m.competition_name || m.competition_type;
        if (mode !== this.filterMode) return false;
      }
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
    get pagedEnemies() {
      return this.filteredEnemies.slice(0, this.enemiesShown);
    },
    get pagedTeammates() {
      return this.filteredTeammates.slice(0, this.teammatesShown);
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

    mapStatsLabel(map) {
      const s = this.filteredMapWins[map];
      if (!s) return "";
      const wr = s.w + s.l > 0 ? Math.round((s.w / (s.w + s.l)) * 100) : 0;
      return `(${wr}%\u00a0·\u00a0${s.w}W\u00a0${s.l}L)`;
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

    // ── Init ────────────────────────────────────────────────────────────────
    init() {
      const p = new URLSearchParams(window.location.search);
      const a = p.get("a")?.trim();
      const b = p.get("b")?.trim();
      if (a && b) {
        this.you = a;
        this.opp = b;
        this.$nextTick(() => this._startSearch(a, b));
      }
      window.addEventListener("beforeunload", () => {
        if (this._es) {
          this._es.close();
          this._es = null;
        }
      });
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
      this._invalidate();
    },

    _startSearch(you, opp) {
      this.errorMsg = "";
      this.cacheNotice = null;
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
      this._invalidate();
      this._pendingMatches = [];
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
            this.searching = false;
            this._done = true;
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
        // After "done" or "error" we already called es.close() — ignore the
        // onerror that fires when the server then drops the TCP connection.
        if (this._done || this.errorMsg) return;
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
  };
}
