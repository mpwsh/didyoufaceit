// ── banscan.js — Alpine app for ban scan results ─────────────────────────────

function banscanApp() {
  return {
    ...matchCardHelpers,

    banScanning: false,
    banScanNick: "",
    banScanPlayerId: "",
    banScanStatus: "Connecting…",
    banScanError: "",
    banScanDone: false,
    banScanProgress: {
      checked: 0,
      unique_opponents: 0,
      total_matches: 0,
    },
    bannedPlayers: [],
    banScanHasMore: false,
    banScanNextBeforeTs: null,
    banScanExpandedPlayer: null,
    banFilterReason: "all",
    banKnownReasons: [],
    _banEs: null,

    get showBanResults() {
      return this.banScanDone || this.banScanning || this.banScanError;
    },

    get availableBanReasons() {
      if (this.banKnownReasons.length === 0) return [];
      return ["all", ...this.banKnownReasons];
    },

    banReasonLabel(reason) {
      return reason === "all"
        ? `All (${this.bannedPlayers.length})`
        : banReasonLabel(reason);
    },

    get filteredBannedPlayers() {
      if (this.banScanning || this.banFilterReason === "all")
        return this.bannedPlayers;
      return this.bannedPlayers.filter((bp) =>
        bp.bans.some(
          (b) => normalizeBanReason(b.reason) === this.banFilterReason,
        ),
      );
    },

    banMatchRoster(m) {
      return factionRoster(m, m.your_team).map((p) => ({
        name: p.name,
        cls: isSamePlayer(p, this.banScanPlayerId, this.banScanNick)
          ? "text-brand"
          : "",
      }));
    },

    // ── Init ────────────────────────────────────────────────────────────────
    init() {
      const p = new URLSearchParams(window.location.search);
      const player = p.get("player")?.trim();
      if (player) {
        this.banScanNick = player;
        this.$nextTick(() => this._startBanScan(player));
      }
    },

    cancelSearch() {
      if (this._banEs) {
        this._banEs.close();
        this._banEs = null;
      }
      this.banScanning = false;
    },

    _startBanScan(nick, loadMoreBeforeTs) {
      const isLoadMore = typeof loadMoreBeforeTs === "number";

      const prevChecked = isLoadMore ? this.banScanProgress.checked : 0;
      const prevOpponents = isLoadMore
        ? this.banScanProgress.unique_opponents
        : 0;
      const prevMatches = isLoadMore ? this.banScanProgress.total_matches : 0;

      if (!isLoadMore) {
        this.banScanNick = nick;
        this.banScanPlayerId = "";
        this.banScanError = "";
        this.banScanDone = false;
        this.banScanProgress = {
          checked: 0,
          unique_opponents: 0,
          total_matches: 0,
        };
        this.bannedPlayers = [];
        this.banScanHasMore = false;
        this.banScanNextBeforeTs = null;
        this.banScanExpandedPlayer = null;
        this.banFilterReason = "all";
        this.banKnownReasons = [];
      }

      if (this._banEs) {
        this._banEs.close();
        this._banEs = null;
      }

      this.banScanning = true;
      this.banScanError = "";
      this.banScanDone = false;
      this.banScanStatus = isLoadMore ? "Loading more matches…" : "Connecting…";

      let apiUrl = `/api/banscan?player=${encodeURIComponent(nick)}&limit=100`;
      if (isLoadMore) apiUrl += `&before=${loadMoreBeforeTs}`;
      const es = new EventSource(apiUrl);
      this._banEs = es;

      es.onmessage = (e) => {
        let ev;
        try {
          ev = JSON.parse(e.data);
        } catch {
          return;
        }

        switch (ev.type) {
          case "status":
            this.banScanStatus = ev.msg;
            break;
          case "player_resolved":
            this.banScanNick = ev.nickname;
            this.banScanPlayerId = ev.player_id || "";
            break;
          case "progress":
            this.banScanProgress = {
              checked: prevChecked + (ev.checked || 0),
              unique_opponents: prevOpponents + (ev.unique_opponents || 0),
              total_matches: prevMatches + (ev.total_matches || 0),
            };
            break;
          case "banned_player": {
            const newMatches = (ev.matches || []).sort(
              (a, b) => b.started_at - a.started_at,
            );
            const existingIdx = this.bannedPlayers.findIndex(
              (x) => x.player_id === ev.player_id,
            );
            if (existingIdx !== -1) {
              const existing = this.bannedPlayers[existingIdx];
              const knownIds = new Set(existing.matches.map((m) => m.match_id));
              const added = newMatches.filter((m) => !knownIds.has(m.match_id));
              if (added.length > 0) {
                const merged = [...existing.matches, ...added].sort(
                  (a, b) => b.started_at - a.started_at,
                );
                const updated = {
                  ...existing,
                  // Keep the first page's flag: it was computed against your most recent
                  // match with this player; older pages would flag bans you played after.
                  matches: merged,
                  matches_against: merged.length,
                };
                const arr = [...this.bannedPlayers];
                arr[existingIdx] = updated;
                this.bannedPlayers = arr;
              }
            } else {
              this.bannedPlayers = [
                ...this.bannedPlayers,
                {
                  player_id: ev.player_id,
                  nickname: ev.nickname,
                  steam_id_64: ev.steam_id_64 || "",
                  avatar: ev.avatar || "",
                  bans: ev.bans || [],
                  matches_against: newMatches.length,
                  matches: newMatches,
                },
              ];
            }
            for (const b of ev.bans || []) {
              const norm = normalizeBanReason(b.reason);
              if (norm && !this.banKnownReasons.includes(norm)) {
                this.banKnownReasons = [...this.banKnownReasons, norm];
              }
            }
            break;
          }
          case "done":
            // Most recent match against them first
            this.bannedPlayers = [...this.bannedPlayers].sort((a, b) => {
              const aDate = a.matches[0]?.started_at || 0;
              const bDate = b.matches[0]?.started_at || 0;
              return bDate - aDate;
            });
            this.banScanning = false;
            this.banScanDone = true;
            this.banScanHasMore = ev.has_more || false;
            this.banScanNextBeforeTs = ev.next_before_ts || null;
            es.close();
            this._banEs = null;
            break;
          case "error":
            this.banScanning = false;
            this.banScanError = friendlyError(ev.msg, ev.status);
            es.close();
            this._banEs = null;
            break;
        }
      };

      es.onerror = () => {
        // After "done" or "error" we already called es.close() —
        // ignore the onerror that fires when the server drops the TCP connection.
        if (this.banScanDone || this.banScanError) return;
        this.banScanning = false;
        this.banScanError = "Connection lost. Please try again.";
        es.close();
        this._banEs = null;
      };
    },

    loadMoreBans() {
      if (this.banScanNextBeforeTs !== null) {
        this._startBanScan(this.banScanNick, this.banScanNextBeforeTs);
      }
    },

    toggleBanPlayer(playerId) {
      this.banScanExpandedPlayer =
        this.banScanExpandedPlayer === playerId ? null : playerId;
    },
  };
}
