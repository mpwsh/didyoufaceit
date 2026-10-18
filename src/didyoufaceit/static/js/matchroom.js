// ── matchroom.js — Alpine app for matchroom analysis ─────────────────────────

const FINAL_STATUSES = ["FINISHED", "CANCELLED", "ABORTED"];
const POOL_POLL_MS = 15000;
const POOL_POLL_MAX = 40; // ~10 minutes

const MATCH_ID_RE =
  /1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

function matchroomApp() {
  return {
    mrSearching: false,
    mrError: "",
    mrStatus: "Connecting…",
    mrFactions: {},
    mrMapPool: [],
    mrMapNames: {},
    mrPickedMap: null,
    mrPoolSource: "match", // "default" until FACEIT sends this room's own pool
    mrMatchStatus: "",
    mrMatchId: "",
    mrResult: null, // {winner, score} once the match is over
    mrDone: false,
    mrSel: null, // map whose players show in the shared table
    _mrEs: null,
    _seenPlayers: {},
    _poolTimer: null,
    _poolTries: 0,

    // ── Helpers ─────────────────────────────────────────────────────────────
    mrMapName(id) {
      if (!id) return "";
      return this.mrMapNames[id] || mapDisplayName(id);
    },
    mrSelect(mapId) {
      this.mrSel = mapId;
    },
    fmtDiff(d) {
      if (d === null) return "no data";
      if (Math.abs(d) < 0.005) return "±0.00";
      return (d > 0 ? "+" : "−") + Math.abs(d).toFixed(2);
    },
    diffClass(d) {
      if (d === null || Math.abs(d) < 0.005) return "text-muted bg-surface-alt";
      return d > 0 ? "text-win bg-win/10" : "text-lose bg-lose/10";
    },

    // Maps to show: the match's own pool (from FACEIT voting, or the server's
    // fallback pool). Only when no pool arrived, every map any player has played.
    get mrAllMaps() {
      if (this.mrMapPool.length) return [...this.mrMapPool];
      const maps = new Set();
      for (const f of Object.values(this.mrFactions))
        for (const p of f.players || [])
          for (const mapId of Object.keys(p.map_stats || {})) maps.add(mapId);
      return Array.from(maps);
    },

    // One team on one map.
    // - rating: plain mean over the whole roster, so one veteran can't outweigh four
    //   teammates. The server already pulls each player's map rating toward their current
    //   form; a player with no games on the map counts at their overall rating.
    // - winPct: mean of the win % of players who have played it; matches: their total.
    _teamOnMap(faction, mapId) {
      const players = this.mrFactions[faction]?.players || [];
      const played = players.filter((p) => p.map_stats?.[mapId]?.matches > 0);
      const ratings = players
        .map((p) => p.map_stats?.[mapId]?.rating || p.overall_rating || 0)
        .filter((r) => r > 0);
      return {
        rating: ratings.length ? ratings.reduce((s, r) => s + r, 0) / ratings.length : 0,
        winPct: played.length
          ? played.reduce((s, p) => s + p.map_stats[mapId].wr, 0) / played.length
          : null,
        matches: played.reduce((s, p) => s + p.map_stats[mapId].matches, 0),
        withData: played.length,
      };
    },

    // Every map with both teams' numbers and the rating diff (team 0 minus team 1).
    // A side where nobody has played the map has no diff: it would compare against a guess.
    get mrMapStats() {
      const fk = this.mrFactionKeys;
      if (fk.length < 2) return [];
      return this.mrAllMaps.map((id) => {
        const t0 = this._teamOnMap(fk[0], id);
        const t1 = this._teamOnMap(fk[1], id);
        const noData = t0.withData === 0 || t1.withData === 0;
        return { id, teams: [t0, t1], diff: noData ? null : t0.rating - t1.rating };
      });
    },

    // One column per team: its maps from best to worst against this opponent, by diff.
    mrColumn(idx) {
      const sign = idx === 0 ? 1 : -1;
      const rows = this.mrMapStats.map((m) => {
        const t = m.teams[idx];
        return {
          id: m.id,
          diff: m.diff === null ? null : sign * m.diff,
          rating: t.withData ? t.rating.toFixed(2) : "—",
          ratingColor: t.withData ? ratingColor(t.rating) : "",
          winPct: t.winPct === null ? "—" : Math.round(t.winPct) + "%",
          winColor: t.winPct === null ? "" : wrColor(t.winPct),
          matches: t.matches.toLocaleString("en-US"),
          lowData: t.withData < 3,
        };
      });
      rows.sort((a, b) => (a.diff === null) - (b.diff === null) || (b.diff ?? 0) - (a.diff ?? 0));
      rows.forEach((r, i) => (r.rank = r.diff === null ? "–" : String(i + 1)));
      return rows;
    },

    // The selected map, one group per team: the team's numbers, then its players by rating.
    get mrSelTeams() {
      const mapId = this.mrSel;
      const m = this.mrMapStats.find((x) => x.id === mapId);
      if (!m) return [];
      return this.mrFactionKeys.slice(0, 2).map((f, idx) => {
        const t = m.teams[idx];
        const players = (this.mrFactions[f]?.players || []).map((p) => {
          const ms = p.map_stats?.[mapId];
          const played = ms?.matches > 0;
          return {
            key: f + p.player_id,
            level: p.skill_level || eloToLevel(p.elo),
            nickname: p.nickname,
            faceitUrl: "https://www.faceit.com/en/players/" + encodeURIComponent(p.nickname),
            steamUrl: p.steam_id_64 ? "https://steamcommunity.com/profiles/" + p.steam_id_64 : "",
            ripUrl: p.steam_id_64 ? "https://steamcommunity.rip/profiles/" + p.steam_id_64 : "",
            winPct: played ? ms.wr : null,
            kd: played ? ms.kd : null,
            rating: played ? ms.rating : null,
            matches: played ? ms.matches : 0,
          };
        });
        players.sort((x, y) => (y.rating ?? -1) - (x.rating ?? -1));
        return {
          key: f,
          name: this.mrTeamName(idx),
          color: idx === 0 ? "text-brand" : "text-blue",
          dot: idx === 0 ? "bg-brand" : "bg-blue",
          rating: t.withData ? t.rating.toFixed(2) : "—",
          ratingColor: t.withData ? ratingColor(t.rating) : "",
          winPct: t.winPct === null ? "—" : Math.round(t.winPct) + "%",
          winColor: t.winPct === null ? "" : wrColor(t.winPct),
          diff: m.diff === null ? null : (idx === 0 ? 1 : -1) * m.diff,
          players,
        };
      });
    },

    // Only worth a line when the teams can't be compared; otherwise the group headers say it all.
    get mrSelSummary() {
      const m = this.mrMapStats.find((x) => x.id === this.mrSel);
      return m && m.diff === null ? "Not enough games on this map to compare the teams" : "";
    },

    get mrPoolNote() {
      return this.mrPoolSource === "match" ? "" : "Unable to load the map list with vetos.";
    },

    _applyPool(ev) {
      this.mrMapPool = ev.map_pool || [];
      this.mrMapNames = ev.map_names || {};
      this.mrPickedMap = ev.picked_map || null;
      this.mrPoolSource = ev.map_pool_source || "match";
      this.mrMatchStatus = ev.match_status || "";
    },

    // A live room may not have its pool or pick yet: re-check it (one cheap call) until it does.
    _watchPool(id) {
      clearTimeout(this._poolTimer);
      const settled = this.mrPoolSource === "match" && this.mrPickedMap;
      if (settled || FINAL_STATUSES.includes(this.mrMatchStatus)) return;
      if (this._poolTries >= POOL_POLL_MAX) return;
      this._poolTimer = setTimeout(async () => {
        this._poolTries++;
        try {
          const r = await fetch(`/api/matchroom/pool?id=${encodeURIComponent(id)}`);
          if (r.ok) {
            this._applyPool(await r.json());
            if (!this.mrAllMaps.includes(this.mrSel)) this.mrSel = this.mrColumn(0)[0]?.id ?? null;
          }
        } catch {
          // offline for a moment: try again next tick
        }
        this._watchPool(id);
      }, POOL_POLL_MS);
    },

    get mrFaceitUrl() {
      return this.mrMatchId ? `https://www.faceit.com/en/cs2/room/${this.mrMatchId}` : "";
    },

    // Final score in column order (team one first), with who won
    get mrScoreline() {
      const r = this.mrResult;
      const fk = this.mrFactionKeys;
      if (!r || fk.length < 2) return null;
      return [0, 1].map((idx) => ({
        name: this.mrTeamName(idx),
        score: r.score ? r.score[fk[idx]] ?? "–" : "",
        won: r.winner === fk[idx],
        color: idx === 0 ? "text-brand" : "text-blue",
      }));
    },

    get mrStatusLabel() {
      const s = this.mrMatchStatus;
      if (s === "CANCELLED") return "Cancelled";
      if (s === "ABORTED") return "Aborted";
      return "";
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
    },

    cancelSearch() {
      if (this._mrEs) {
        this._mrEs.close();
        this._mrEs = null;
      }
      this.mrSearching = false;
    },

    _startMatchroom(raw) {
      // Same check as the server: a match id, on its own or inside a room link
      const found = String(raw).toLowerCase().match(MATCH_ID_RE);
      if (!found) {
        this.mrSearching = false;
        this.mrError = "That doesn't look like a FACEIT match room link or id.";
        return;
      }
      raw = found[0];
      this.mrMatchId = raw;
      this.mrResult = null;
      this.mrSearching = true;
      this.mrError = "";
      this.mrStatus = "Connecting…";
      this.mrFactions = {};
      this.mrMapPool = [];
      this.mrMapNames = {};
      this.mrPickedMap = null;
      this.mrPoolSource = "match";
      this.mrMatchStatus = "";
      clearTimeout(this._poolTimer);
      this._poolTries = 0;
      this.mrDone = false;
      this.mrSel = null;
      this._seenPlayers = {};

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
              factions[ev.faction] = { name: ev.faction, players: [] };
            factions[ev.faction] = {
              ...factions[ev.faction],
              players: [
                ...factions[ev.faction].players,
                {
                  player_id: ev.player_id,
                  nickname: ev.nickname,
                  elo: ev.elo,
                  skill_level: ev.skill_level || 0,
                  steam_id_64: /^\d{17}$/.test(ev.steam_id_64 || "") ? ev.steam_id_64 : "",
                  map_stats: ev.map_stats,
                  overall_rating: ev.overall_rating ?? null,
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
              updated[faction] = { ...updated[faction], name: data.name };
            }
            this.mrFactions = updated;
            this._applyPool(ev);
            this.mrResult = ev.result || null;
            break;
          }
          case "done":
            this.mrSearching = false;
            this.mrDone = true;
            this.mrSel = this.mrColumn(0)[0]?.id ?? null; // team one's best map first
            this._watchPool(raw);
            es.close();
            this._mrEs = null;
            break;
          case "error":
            this.mrSearching = false;
            this.mrError = friendlyError(ev.msg, ev.status);
            es.close();
            this._mrEs = null;
            break;
        }
      };
      es.onerror = () => {
        if (this.mrDone || this.mrError) return;
        this.mrSearching = false;
        this.mrError = "Connection lost. Please try again.";
        es.close();
        this._mrEs = null;
      };
    },
  };
}
