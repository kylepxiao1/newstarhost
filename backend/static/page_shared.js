(function () {
  "use strict";

  function api(path, opts) {
    const o = opts || {};
    return fetch(path, {
      method: o.method || "GET",
      headers: {"Content-Type": "application/json"},
      body: o.body ? JSON.stringify(o.body) : undefined,
    });
  }

  function isTypingTarget(target) {
    if (!target) return false;
    var tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
  }

  function normalizeVolume(val) {
    var v = parseFloat(val);
    if (!Number.isFinite(v)) return 1;
    if (v <= 1) return Math.max(0, Math.min(1, v));
    return Math.max(0, Math.min(1, v / 200));
  }

  function getGlobalAudioPlayer() {
    if (window.top && window.top !== window) {
      var gp = window.top.document.getElementById("globalPlayer");
      if (gp) return gp;
    }
    if (!window._sharedAudio) {
      window._sharedAudio = new Audio();
    }
    return window._sharedAudio;
  }

  function readPracticeModeLocal() {
    try {
      return localStorage.getItem("loop_same_song_after_finish") === "1";
    } catch (e) {
      return false;
    }
  }

  function writePracticeModeLocal(enabled) {
    try {
      localStorage.setItem("loop_same_song_after_finish", enabled ? "1" : "0");
    } catch (e) {}
  }

  async function fetchAppSettings(apiFn) {
    var callApi = apiFn || api;
    var out = {
      fade_in: true,
      fade_out: true,
      loop_same_song_after_finish: readPracticeModeLocal(),
      hotkeys: {},
    };
    try {
      var res = await callApi("/settings/data");
      var data = await res.json();
      out.fade_in = data.fade_in !== false;
      out.fade_out = data.fade_out !== false;
      out.loop_same_song_after_finish = data.loop_same_song_after_finish === true;
      out.hotkeys = (data.hotkeys && typeof data.hotkeys === "object") ? data.hotkeys : {};
      writePracticeModeLocal(out.loop_same_song_after_finish);
    } catch (e) {}
    return out;
  }

  function queueSongPaused(cfg) {
    if (!cfg || !cfg.url) return;
    var player = cfg.player || getGlobalAudioPlayer();
    if (!player) return;
    try { player.pause(); } catch (e) {}
    player.onended = null;
    player.loop = false;
    player.src = cfg.url;
    player.currentTime = 0;
    var songVol = 1;
    if (typeof cfg.getSongVolume === "function") {
      songVol = normalizeVolume(cfg.getSongVolume(cfg.url));
    }
    player.volume = songVol;
    if (typeof cfg.setCurrent === "function") {
      cfg.setCurrent(cfg.url);
    }
  }

  window.PageShared = {
    api: api,
    isTypingTarget: isTypingTarget,
    normalizeVolume: normalizeVolume,
    getGlobalAudioPlayer: getGlobalAudioPlayer,
    readPracticeModeLocal: readPracticeModeLocal,
    writePracticeModeLocal: writePracticeModeLocal,
    fetchAppSettings: fetchAppSettings,
    queueSongPaused: queueSongPaused,
  };
})();
