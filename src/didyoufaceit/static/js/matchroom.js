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
    errorMsg: "",
    _mrEs: null,
    _seenPlayers: {},
    _expandedMaps: new Set(),

    toggleMap(mapId) {
      if (this._expandedMaps.has(mapId)) {
        this._expandedMaps.delete(mapId);
      } else {
        this._expandedMaps.add(mapId);
      }
      // Trigger reactivity (Alpine doesn't track Set mutations)
      this._expandedMaps = new Set(this._expandedMaps);
    },
    isExpanded(mapId) {
      return this._expandedMaps.has(mapId);
    },

    // Auto-expand the best and worst maps once data loads
    _autoExpand() {
      const rows = this.mrMapRows;
      if (rows.length >= 2) {
        this._expandedMaps = new Set([rows[0].id, rows[rows.length - 1].id]);
      }
    },

    // ── Helpers ─────────────────────────────────────────────────────────────
    mrMapName(id) {
      return mapDisplayName(id);
    },
    mrWrColor(wr) {
      return wrColor(wr);
    },
    mrLevelColor(lvl) {
      return levelColor(lvl);
    },
    mrKdColor(kd) {
      return kdColor(kd);
    },
    mrRatingColor(r) {
      return ratingColor(r);
    },

    mrPlayerContribs(faction, mapId) {
      const players = this.mrFactions[faction]?.players || [];
      if (!players.length) return [];
      return players
        .filter((p) => p.map_stats?.[mapId]?.matches > 0)
        .map((p) => {
          const ms = p.map_stats[mapId];
          const level = p.skill_level || eloToLevel(p.elo);
          return {
            nickname: p.nickname,
            wins: ms.wins,
            matches: ms.matches,
            wr: ms.wr,
            elo: p.elo || 0,
            level,
            kd: ms.kd || 0,
            rating: ms.rating || 0,
            strength: playerMapStrength(
              ms.wins,
              ms.matches,
              ms.kd || 1.0,
              level,
              ms.rating || 0,
            ),
          };
        })
        .sort((a, b) => b.strength - a.strength);
    },

    mrTeamScore(faction, mapId) {
      const contribs = this.mrPlayerContribs(faction, mapId);
      const total = (this.mrFactions[faction]?.players || []).length || 5;
      if (!contribs.length) return null;
      return Math.round(
        (contribs.reduce((s, c) => s + c.strength, 0) / total) * 100,
      );
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
      let w = 0,
        m = 0;
      for (const p of players) {
        const ms = p.map_stats?.[mapId];
        if (ms) {
          w += ms.wins;
          m += ms.matches;
        }
      }
      return m === 0 ? null : Math.round((w / m) * 100);
    },

    // ── Single unified map list (sorted best-for-A → best-for-B) ────────────

    // Match-count-weighted average rating for a team's contribs on a map
    _weightedAvgRating(contribs) {
      if (!contribs.length) return 0;
      let totalWeight = 0,
        weightedSum = 0;
      for (const c of contribs) {
        if (c.rating > 0 && c.matches > 0) {
          weightedSum += c.rating * c.matches;
          totalWeight += c.matches;
        }
      }
      return totalWeight > 0 ? weightedSum / totalWeight : 0;
    },

    // Match-count-weighted average WR for a team's contribs on a map
    _weightedAvgWr(contribs) {
      if (!contribs.length) return 0;
      let totalWins = 0,
        totalMatches = 0;
      for (const c of contribs) {
        totalWins += c.wins;
        totalMatches += c.matches;
      }
      return totalMatches > 0 ? (totalWins / totalMatches) * 100 : 0;
    },

    get mrMapRows() {
      const fk = Object.keys(this.mrFactions);
      if (fk.length < 2) return [];
      const [f1, f2] = fk;
      const rows = [];
      for (const mapId of this.mrAllMaps) {
        const f1score = this.mrTeamScore(f1, mapId),
          f2score = this.mrTeamScore(f2, mapId);
        if (f1score === null && f2score === null) continue;
        const f1d = this.mrTeamDepth(f1, mapId),
          f2d = this.mrTeamDepth(f2, mapId);
        const lowData = f1d.withData < 3 || f2d.withData < 3;
        const f1c = this.mrPlayerContribs(f1, mapId),
          f2c = this.mrPlayerContribs(f2, mapId);

        // Match-weighted averages (players with more games count more)
        const f1ar = this._weightedAvgRating(f1c),
          f2ar = this._weightedAvgRating(f2c);
        const f1wwr = this._weightedAvgWr(f1c),
          f2wwr = this._weightedAvgWr(f2c);

        // Confidence curves — separate for WR and HLTV
        const f1am =
          f1c.length > 0
            ? f1c.reduce((s, c) => s + c.matches, 0) / f1c.length
            : 0;
        const f2am =
          f2c.length > 0
            ? f2c.reduce((s, c) => s + c.matches, 0) / f2c.length
            : 0;
        const minAvgMatches = Math.min(f1am, f2am);

        // WR confidence: ramps slowly (full at ~100 avg matches) — WR is noisy with small samples
        const wrConf = 0.15 + 0.85 * Math.min(minAvgMatches / 100, 1);

        // HLTV confidence: ramps fast (full at ~30 avg matches) — per-round rating is reliable earlier
        const hltvConf = 0.4 + 0.6 * Math.min(minAvgMatches / 30, 1);

        // WR delta
        const wrDelta = (f1wwr - f2wwr) * wrConf;

        // HLTV delta — scale to roughly same range as WR delta (~±10)
        const hltvDelta = (f1ar - f2ar) * 50 * hltvConf;

        // Blended score: 80% HLTV, 20% WR
        const delta = Math.round(wrDelta * 0.2 + hltvDelta * 0.8);

        // Display WR (simple team-level for the tooltip / reference)
        const f1wr = this.mrTeamWr(f1, mapId),
          f2wr = this.mrTeamWr(f2, mapId);

        rows.push({
          id: mapId,
          f1wr,
          f2wr,
          f1avgRating: f1ar,
          f2avgRating: f2ar,
          lowData,
          delta,
          f1contribs: f1c,
          f2contribs: f2c,
        });
      }
      rows.sort((a, b) => b.delta - a.delta);
      return rows;
    },

    get mrFactionKeys() {
      return Object.keys(this.mrFactions);
    },
    mrTeamName(idx) {
      const fk = this.mrFactionKeys;
      return idx < fk.length ? this.mrFactions[fk[idx]]?.name || fk[idx] : "";
    },

    // ── Init ────────────────────────────────────────────────────────────────
    init() {
      const p = new URLSearchParams(window.location.search);
      const id = p.get("id")?.trim();
      if (id) this.$nextTick(() => this._startMatchroom(id));
      window.addEventListener("beforeunload", () => {
        if (this._mrEs) {
          this._mrEs.close();
          this._mrEs = null;
        }
      });
    },

    cancelSearch() {
      if (this._mrEs) {
        this._mrEs.close();
        this._mrEs = null;
      }
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
      this._seenPlayers = {};
      this._expandedMaps = new Set();

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
            if (!this._seenPlayers[ev.faction])
              this._seenPlayers[ev.faction] = new Set();
            if (this._seenPlayers[ev.faction].has(ev.player_id)) break;
            this._seenPlayers[ev.faction].add(ev.player_id);
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
            this._autoExpand();
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
        if (this.mrDone || this.mrError) return;
        if (es.readyState === EventSource.CLOSED) return;
        this.mrSearching = false;
        this.mrError = "Connection lost. Please try again.";
        es.close();
        this._mrEs = null;
      };
    },
  };
}
