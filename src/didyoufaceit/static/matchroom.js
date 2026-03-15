// ── matchroom.js — Alpine app for matchroom analysis ─────────────────────────

function matchroomApp() {
  return {
    mrMatchId: "",
    mrSearching: false,
    mrError: "",
    mrStatus: "Connecting…",
    mrFactions: {},
    mrMapPool: [],
    mrPickedMap: null,
    mrDone: false,
    mrPerspective: 0,
    errorMsg: "",
    _mrEs: null,

    // ── Matchroom computed ──────────────────────────────────────────────────
    mrMapName(id) { return mapDisplayName(id); },
    mrWrColor(wr) { return wrColor(wr); },
    mrLevelColor(lvl) { return levelColor(lvl); },
    mrKdColor(kd) { return kdColor(kd); },
    mrRatingColor(r) { return ratingColor(r); },

    mrPlayerContribs(faction, mapId) {
      const players = this.mrFactions[faction]?.players || [];
      if (!players.length) return [];
      return players
        .filter((p) => p.map_stats?.[mapId]?.matches > 0)
        .map((p) => {
          const ms = p.map_stats[mapId];
          const level = p.skill_level || eloToLevel(p.elo);
          return {
            nickname: p.nickname, wins: ms.wins, matches: ms.matches,
            wr: ms.wr, elo: p.elo || 0, level, kd: ms.kd || 0,
            rating: ms.rating || 0,
            strength: playerMapStrength(ms.wins, ms.matches, ms.kd || 1.0, level, ms.rating || 0),
          };
        })
        .sort((a, b) => b.strength - a.strength);
    },

    mrTeamScore(faction, mapId) {
      const contribs = this.mrPlayerContribs(faction, mapId);
      const totalPlayers = (this.mrFactions[faction]?.players || []).length || 5;
      if (!contribs.length) return null;
      const sum = contribs.reduce((s, c) => s + c.strength, 0);
      return Math.round((sum / totalPlayers) * 100);
    },

    mrTeamDepth(faction, mapId) {
      const players = this.mrFactions[faction]?.players || [];
      let experienced = 0, withData = 0;
      for (const p of players) {
        const ms = p.map_stats?.[mapId];
        if (ms && ms.matches > 0) { withData++; if (ms.matches >= 20) experienced++; }
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
      let totalWins = 0, totalMatches = 0;
      for (const p of players) {
        const ms = p.map_stats?.[mapId];
        if (ms) { totalWins += ms.wins; totalMatches += ms.matches; }
      }
      return totalMatches === 0 ? null : Math.round((totalWins / totalMatches) * 100);
    },

    get mrMapRecommendation() {
      const fk = Object.keys(this.mrFactions);
      if (fk.length < 2) return [];
      const [f1, f2] = fk;
      const rows = [];
      for (const mapId of this.mrAllMaps) {
        const f1wr = this.mrTeamWr(f1, mapId), f2wr = this.mrTeamWr(f2, mapId);
        const f1score = this.mrTeamScore(f1, mapId), f2score = this.mrTeamScore(f2, mapId);
        if (f1score === null && f2score === null) continue;
        const rawDelta = (f1score || 0) - (f2score || 0);
        const f1d = this.mrTeamDepth(f1, mapId), f2d = this.mrTeamDepth(f2, mapId);
        const lowData = f1d.withData < 3 || f2d.withData < 3;
        const minCoverage = Math.min(f1d.withData, f2d.withData) / 5;
        const confidence = 0.2 + 0.8 * minCoverage;
        const adjustedDelta = Math.round(rawDelta * confidence);
        const f1contribs = this.mrPlayerContribs(f1, mapId);
        const f2contribs = this.mrPlayerContribs(f2, mapId);
        const f1avgMatches = f1contribs.length > 0
          ? f1contribs.reduce((s, c) => s + c.matches, 0) / f1contribs.length : 0;
        const f2avgMatches = f2contribs.length > 0
          ? f2contribs.reduce((s, c) => s + c.matches, 0) / f2contribs.length : 0;
        const avgMatches = Math.min(f1avgMatches, f2avgMatches);
        const wrConfidence = 0.15 + 0.85 * Math.min(avgMatches / 100, 1);
        const rawWrDelta = (f1wr || 0) - (f2wr || 0);
        const adjustedWrDelta = Math.round(rawWrDelta * wrConfidence);
        const wrDelta = this.mrPerspective === 0 ? adjustedWrDelta : -adjustedWrDelta;
        const f1avgRating = f1contribs.length > 0
          ? f1contribs.reduce((s, c) => s + (c.rating || 0), 0) / f1contribs.length : 0;
        const f2avgRating = f2contribs.length > 0
          ? f2contribs.reduce((s, c) => s + (c.rating || 0), 0) / f2contribs.length : 0;
        rows.push({
          id: mapId, f1wr, f2wr, f1score, f2score, f1avgRating, f2avgRating,
          rawDelta, lowData, confidence,
          delta: this.mrPerspective === 0 ? adjustedDelta : -adjustedDelta,
          wrDelta, f1depth: f1d, f2depth: f2d, f1contribs, f2contribs,
        });
      }
      rows.sort((a, b) => b.wrDelta - a.wrDelta);
      return rows;
    },

    get mrFactionKeys() { return Object.keys(this.mrFactions); },

    // ── Init ────────────────────────────────────────────────────────────────
    init() {
      const p = new URLSearchParams(window.location.search);
      const id = p.get("id")?.trim();
      if (id) {
        this.$nextTick(() => this._startMatchroom(id));
      }
      window.addEventListener("beforeunload", () => {
        if (this._mrEs) { this._mrEs.close(); this._mrEs = null; }
      });
    },

    cancelSearch() {
      if (this._mrEs) { this._mrEs.close(); this._mrEs = null; }
      this.mrSearching = false;
    },

    _startMatchroom(raw) {
      this.mrMatchId = raw;
      this.mrSearching = true;
      this.mrError = "";
      this.mrStatus = "Connecting…";
      this.mrFactions = {};
      this.mrMapPool = [];
      this.mrPickedMap = null;
      this.mrDone = false;

      const es = new EventSource(`/api/matchroom?id=${encodeURIComponent(raw)}`);
      this._mrEs = es;

      es.onmessage = (e) => {
        let ev;
        try { ev = JSON.parse(e.data); } catch { return; }
        switch (ev.type) {
          case "status":
            this.mrStatus = ev.msg;
            break;
          case "player": {
            const factions = { ...this.mrFactions };
            if (!factions[ev.faction])
              factions[ev.faction] = { name: ev.faction, players: [], map_scores: {} };
            factions[ev.faction] = {
              ...factions[ev.faction],
              players: [...factions[ev.faction].players, {
                player_id: ev.player_id, nickname: ev.nickname,
                elo: ev.elo, skill_level: ev.skill_level || 0, map_stats: ev.map_stats,
              }],
            };
            this.mrFactions = factions;
            break;
          }
          case "summary": {
            const updated = { ...this.mrFactions };
            for (const [faction, data] of Object.entries(ev.factions)) {
              if (!updated[faction]) updated[faction] = { players: [] };
              updated[faction] = { ...updated[faction], name: data.name, map_scores: data.map_scores };
            }
            this.mrFactions = updated;
            this.mrMapPool = ev.map_pool;
            this.mrPickedMap = ev.picked_map;
            break;
          }
          case "done":
            this.mrSearching = false; this.mrDone = true;
            es.close(); this._mrEs = null;
            break;
          case "error":
            this.mrSearching = false; this.mrError = friendlyError(ev.msg);
            es.close(); this._mrEs = null;
            break;
        }
      };
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) return;
        this.mrSearching = false; this.mrError = "Connection lost. Please try again.";
        es.close(); this._mrEs = null;
      };
    },
  };
}
