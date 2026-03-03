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

  var AUDIO_CACHE_NAME = "newstarhost-song-cache-v1";
  var AUDIO_BLOB_URL_LIMIT = 80;
  var _audioBlobUrlBySource = Object.create(null);
  var _audioBlobUrlOrder = [];

  function toAbsoluteUrl(url) {
    try {
      return new URL(String(url || ""), window.location.href).toString();
    } catch (e) {
      return String(url || "");
    }
  }

  function isCacheableAudioUrl(absUrl) {
    if (!absUrl || typeof caches === "undefined") return false;
    if (!/^https?:/i.test(absUrl)) return false;
    try {
      var parsed = new URL(absUrl);
      return parsed.origin === window.location.origin;
    } catch (e) {
      return false;
    }
  }

  function rememberAudioBlobUrl(absUrl, blobUrl) {
    if (!absUrl || !blobUrl) return;
    if (_audioBlobUrlBySource[absUrl]) return;
    _audioBlobUrlBySource[absUrl] = blobUrl;
    _audioBlobUrlOrder.push(absUrl);
    if (_audioBlobUrlOrder.length <= AUDIO_BLOB_URL_LIMIT) return;
    var evictKey = _audioBlobUrlOrder.shift();
    if (!evictKey) return;
    var evictUrl = _audioBlobUrlBySource[evictKey];
    delete _audioBlobUrlBySource[evictKey];
    if (evictUrl && typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
      try { URL.revokeObjectURL(evictUrl); } catch (e) {}
    }
  }

  async function resolveAudioUrlFromCache(url) {
    var absUrl = toAbsoluteUrl(url);
    if (!isCacheableAudioUrl(absUrl)) return String(url || "");
    var remembered = _audioBlobUrlBySource[absUrl];
    if (remembered) return remembered;
    try {
      var cache = await caches.open(AUDIO_CACHE_NAME);
      var resp = await cache.match(absUrl);
      if (!resp || !resp.ok) return absUrl;
      var blob = await resp.blob();
      if (!blob || !blob.size) return absUrl;
      var blobUrl = URL.createObjectURL(blob);
      rememberAudioBlobUrl(absUrl, blobUrl);
      return blobUrl;
    } catch (e) {
      return absUrl;
    }
  }

  async function cacheAudioUrl(url) {
    var absUrl = toAbsoluteUrl(url);
    if (!isCacheableAudioUrl(absUrl)) return false;
    try {
      var cache = await caches.open(AUDIO_CACHE_NAME);
      var hit = await cache.match(absUrl);
      if (hit && hit.ok) return true;
      var resp = await fetch(absUrl, { credentials: "same-origin" });
      if (!resp || !resp.ok) return false;
      await cache.put(absUrl, resp.clone());
      return true;
    } catch (e) {
      return false;
    }
  }

  function primeAudioCache(url) {
    cacheAudioUrl(url).catch(function () {});
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
      display_background_timer: false,
      enable_duo_dance: false,
      hotkeys: {},
    };
    try {
      var res = await callApi("/settings/data");
      var data = await res.json();
      out.fade_in = data.fade_in !== false;
      out.fade_out = data.fade_out !== false;
      out.loop_same_song_after_finish = data.loop_same_song_after_finish === true;
      out.display_background_timer = data.display_background_timer === true;
      out.enable_duo_dance = data.enable_duo_dance === true;
      out.hotkeys = (data.hotkeys && typeof data.hotkeys === "object") ? data.hotkeys : {};
      writePracticeModeLocal(out.loop_same_song_after_finish);
    } catch (e) {}
    return out;
  }

  function stripPracticeModeSuffix(text) {
    return String(text || "").replace(/\s*- Practice Mode\s*$/i, "").trim();
  }

  function setPracticeModeHeader(enabled, options) {
    var cfg = options || {};
    var selector = cfg.headerSelector || "header h1";
    var headerTitle = document.querySelector(selector);
    if (!headerTitle) return;
    var base = headerTitle.getAttribute("data-base-title");
    if (!base) {
      base = stripPracticeModeSuffix(headerTitle.textContent);
      headerTitle.setAttribute("data-base-title", base);
    }
    var suffix = cfg.practiceSuffix || " - Practice Mode";
    headerTitle.textContent = enabled ? (base + suffix) : base;
  }

  async function refreshPracticeModeHeader(options) {
    var cfg = options || {};
    var localEnabled = readPracticeModeLocal();
    setPracticeModeHeader(localEnabled, cfg);
    var callApi = cfg.apiFn || api;
    try {
      var res = await callApi("/settings/data");
      var data = await res.json();
      var enabled = data.loop_same_song_after_finish === true;
      writePracticeModeLocal(enabled);
      setPracticeModeHeader(enabled, cfg);
      return enabled;
    } catch (e) {
      return localEnabled;
    }
  }

  function initPracticeModeHeader(options) {
    var cfg = options || {};
    if (window.__practiceModeHeaderInit) return window.__practiceModeHeaderInit;
    var refresh = function () {
      refreshPracticeModeHeader(cfg);
    };
    refresh();
    var onMessage = function (evt) {
      if (evt && evt.data && evt.data.type === "settings-updated") {
        refresh();
      }
    };
    var onStorage = function (evt) {
      if (!evt || evt.key !== "loop_same_song_after_finish") return;
      setPracticeModeHeader(readPracticeModeLocal(), cfg);
    };
    window.addEventListener("message", onMessage);
    window.addEventListener("storage", onStorage);
    window.__practiceModeHeaderInit = {
      refresh: refresh,
    };
    return window.__practiceModeHeaderInit;
  }

  function setWsStatusUI(isConnected, cfg) {
    var options = cfg || {};
    var dotId = options.dotId || "wsDot";
    var textId = options.textId === undefined ? "statusText" : options.textId;
    var connectedColor = options.connectedColor || "#22c55e";
    var disconnectedColor = options.disconnectedColor || "#ef4444";
    var connectedShadow = options.connectedShadow || "0 0 8px " + connectedColor;
    var disconnectedShadow = options.disconnectedShadow || "0 0 8px " + disconnectedColor;
    var connectedText = options.connectedText || "Live";
    var disconnectedText = options.disconnectedText || "Disconnected";
    var dot = dotId ? document.getElementById(dotId) : null;
    if (dot) {
      dot.style.background = isConnected ? connectedColor : disconnectedColor;
      dot.style.boxShadow = isConnected ? connectedShadow : disconnectedShadow;
    }
    if (options.updateText === false) return;
    var label = textId ? document.getElementById(textId) : null;
    if (label) {
      label.textContent = isConnected ? connectedText : disconnectedText;
    }
  }

  function connectWsStatus(options) {
    var cfg = options || {};
    if (!window.StateWsHelper || typeof window.StateWsHelper.connect !== "function") {
      return null;
    }
    var isConnected = false;
    var dotId = cfg.dotId || "wsDot";
    var dot = dotId ? document.getElementById(dotId) : null;
    var conn = window.StateWsHelper.connect({
      wsUrl: cfg.wsUrl,
      staleMs: cfg.staleMs,
      checkIntervalMs: cfg.checkIntervalMs,
      minDelayMs: cfg.minDelayMs,
      maxDelayMs: cfg.maxDelayMs,
      jitterMs: cfg.jitterMs,
      onConnected: function (ws) {
        isConnected = true;
        setWsStatusUI(true, cfg);
        if (typeof cfg.onConnected === "function") cfg.onConnected(ws);
      },
      onDisconnected: function () {
        isConnected = false;
        setWsStatusUI(false, cfg);
        if (typeof cfg.onDisconnected === "function") cfg.onDisconnected();
      },
      onOpen: cfg.onOpen,
      onWake: cfg.onWake,
      onState: cfg.onState,
      onMessage: cfg.onMessage,
      onParseError: cfg.onParseError,
      onError: cfg.onError,
      onClose: cfg.onClose,
    });
    if (dot) {
      dot.style.cursor = "pointer";
      dot.title = "Tap to reconnect if disconnected";
      dot.onclick = function (evt) {
        if (evt) {
          evt.preventDefault();
          evt.stopPropagation();
        }
        if (isConnected) return;
        if (conn && typeof conn.nudge === "function") {
          conn.nudge();
        }
      };
    }
    return conn;
  }

  function queueSongPaused(cfg) {
    if (!cfg || !cfg.url) return;
    var player = cfg.player || getGlobalAudioPlayer();
    if (!player) return;
    var originalUrl = String(cfg.url || "");
    var loadToken = String(Date.now()) + ":" + String(Math.random());
    try { player.dataset.audioLoadToken = loadToken; } catch (e) {}
    resolveAudioUrlFromCache(originalUrl)
      .then(function (playUrl) {
        try {
          if (player.dataset.audioLoadToken !== loadToken) return;
          try { player.pause(); } catch (e) {}
          player.onended = null;
          player.loop = false;
          player.src = playUrl || originalUrl;
          try { player.dataset.originalSrc = originalUrl; } catch (e) {}
          player.currentTime = 0;
          var songVol = 1;
          if (typeof cfg.getSongVolume === "function") {
            songVol = normalizeVolume(cfg.getSongVolume(originalUrl));
          }
          player.volume = songVol;
          if (typeof cfg.setCurrent === "function") {
            cfg.setCurrent(originalUrl);
          }
        } catch (e) {}
      })
      .catch(function () {})
      .finally(function () {
        primeAudioCache(originalUrl);
      });
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
      var originalUrl = String(url || "");
      var loadToken = String(Date.now()) + ":" + String(Math.random());
      try { player.dataset.audioLoadToken = loadToken; } catch (e) {}
      resolveAudioUrlFromCache(originalUrl)
        .then(function (playUrl) {
          if (player.dataset.audioLoadToken !== loadToken) return;
          try { player.pause(); } catch (e) {}
          player.loop = !!opts.loop;
          player.onended = function () {
            if (typeof opts.onEnd === "function") {
              opts.onEnd();
              return;
            }
            if (typeof cfg.onEndedDefault === "function") {
              cfg.onEndedDefault({player: player, url: originalUrl, opts: opts});
            }
          };
          player.src = playUrl || originalUrl;
          try { player.dataset.originalSrc = originalUrl; } catch (e) {}
          if (!opts.isRole && typeof cfg.onTrackPlay === "function") {
            cfg.onTrackPlay({player: player, url: originalUrl, opts: opts});
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
        })
        .catch(function () {})
        .finally(function () {
          primeAudioCache(originalUrl);
        });
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
    setPracticeModeHeader: setPracticeModeHeader,
    refreshPracticeModeHeader: refreshPracticeModeHeader,
    initPracticeModeHeader: initPracticeModeHeader,
    connectWsStatus: connectWsStatus,
    queueSongPaused: queueSongPaused,
    startAudio: startAudio,
    resolveAudioUrlFromCache: resolveAudioUrlFromCache,
    primeAudioCache: primeAudioCache,
  };
})();
