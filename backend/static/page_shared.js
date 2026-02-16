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

  function startAudio(cfg) {
    if (!cfg || !cfg.url) return null;
    var player = cfg.player || getGlobalAudioPlayer();
    if (!player) return null;
    var url = cfg.url;
    var opts = cfg.opts || {};
    var getRoleUrlFn = cfg.getRoleUrl;
    var getSongVolumeFn = cfg.getSongVolume;
    var bellUrl = (typeof getRoleUrlFn === "function") ? getRoleUrlFn("bell") : "";
    var applauseUrl = (typeof getRoleUrlFn === "function") ? getRoleUrlFn("applause") : "";
    var isBellOrApplause = !!url && (url === bellUrl || url === applauseUrl);
    var rawVolume = (typeof opts.volume === "number")
      ? opts.volume
      : ((typeof getSongVolumeFn === "function") ? getSongVolumeFn(url) : 1);
    var targetVolume = normalizeVolume(rawVolume);
    var fadeIn = isBellOrApplause ? false : (opts.fade ?? cfg.settingsFadeIn ?? true);
    var fadeOut = isBellOrApplause ? false : (opts.fadeOut ?? cfg.settingsFadeOut ?? true);

    var doPlay = function () {
      try { player.pause(); } catch (e) {}
      player.loop = !!opts.loop;
      player.onended = function () {
        if (typeof opts.onEnd === "function") {
          opts.onEnd();
          return;
        }
        if (typeof cfg.onEndedDefault === "function") {
          cfg.onEndedDefault({player: player, url: url, opts: opts});
        }
      };
      player.src = url;
      if (!opts.isRole && typeof cfg.onTrackPlay === "function") {
        cfg.onTrackPlay({player: player, url: url, opts: opts});
      }
      if (fadeIn) {
        var vol = 0.0;
        player.volume = 0;
        var p1 = player.play();
        if (p1 && typeof p1.catch === "function") p1.catch(function () {});
        var steps = 10;
        var stepMs = 50;
        var inc = targetVolume / steps;
        var count = 0;
        var intv = setInterval(function () {
          count += 1;
          vol = Math.min(targetVolume, vol + inc);
          player.volume = vol;
          if (count >= steps) clearInterval(intv);
        }, stepMs);
      } else {
        player.volume = targetVolume;
        var p2 = player.play();
        if (p2 && typeof p2.catch === "function") p2.catch(function () {});
      }
    };

    if (fadeOut && !opts.skipFadeOut && !player.paused && player.src) {
      var volOut = player.volume;
      var stepsOut = 10;
      var stepMsOut = 50;
      var dec = volOut / stepsOut;
      var countOut = 0;
      var outv = setInterval(function () {
        countOut += 1;
        volOut = Math.max(0, volOut - dec);
        player.volume = volOut;
        if (countOut >= stepsOut) {
          clearInterval(outv);
          try { player.pause(); } catch (e) {}
          doPlay();
        }
      }, stepMsOut);
    } else {
      doPlay();
    }

    if (typeof cfg.onAfterStart === "function") {
      cfg.onAfterStart({player: player, url: url, opts: opts});
    }

    return player;
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
    startAudio: startAudio,
  };
})();
