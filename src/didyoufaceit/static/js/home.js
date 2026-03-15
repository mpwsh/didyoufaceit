// ── home.js — Alpine app for the home/search page ──────────────────────────

function homeApp() {
  return {
    youInput: "",
    oppInput: "",
    matchroomInput: "",
    banScanInput: "",

    doSearch() {
      if (this.banScanInput.trim()) {
        this.doBanScan();
        return;
      }
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
      window.location.href = `/vs?a=${encodeURIComponent(you)}&b=${encodeURIComponent(opp)}`;
    },

    doMatchroom() {
      let raw = this.matchroomInput.trim();
      if (!raw) return;
      if (raw.includes("room/"))
        raw = raw.split("room/").pop().split("?")[0].split("/")[0].trim();
      window.location.href = `/matchroom?id=${encodeURIComponent(raw)}`;
    },

    doBanScan() {
      const nick = this.banScanInput.trim();
      if (!nick) return;
      window.location.href = `/banscan?player=${encodeURIComponent(nick)}`;
    },
  };
}
