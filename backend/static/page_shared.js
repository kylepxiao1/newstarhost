(function () {
  "use strict";

  function goToLogin() {
    try { (window.top || window).location.href = "/login"; } catch (e) { window.location.href = "/login"; }
  }

  function api(path, opts) {
    const o = opts || {};
    return fetch(path, {
      method: o.method || "GET",
      headers: {"Content-Type": "application/json"},
      body: o.body ? JSON.stringify(o.body) : undefined,
    }).then(function (resp) {
      // Session expired or logged out elsewhere: send the whole app to the login page.
      if (resp.status === 401) goToLogin();
      return resp;
    });
  }

  // The logged-in dance group ({display_name, tiktok_handle, overlay_url, ...}), fetched once.
  var groupPromise = null;
  function getGroup() {
    if (!groupPromise) {
      groupPromise = fetch("/auth/me")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (g) { window.NSH_GROUP = g; return g; })
        .catch(function () { return null; });
    }
    return groupPromise;
  }
  getGroup();

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

  // Song audio on the Supabase Storage CDN is content-addressed: a URL never changes what
  // it points to, so a cached copy is always valid and can be played without the network.
  var CDN_AUDIO_PATH = "/storage/v1/object/public/song-audio/";

  function isCacheableAudioUrl(absUrl) {
    if (!absUrl || typeof caches === "undefined") return false;
    if (!/^https?:/i.test(absUrl)) return false;
    try {
      return new URL(absUrl).pathname.indexOf(CDN_AUDIO_PATH) === 0;
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

  function forgetAudioBlobUrl(absUrl) {
    var blobUrl = _audioBlobUrlBySource[absUrl];
    if (!blobUrl) return;
    delete _audioBlobUrlBySource[absUrl];
    _audioBlobUrlOrder = _audioBlobUrlOrder.filter(function (key) { return key !== absUrl; });
    if (typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function") {
      try { URL.revokeObjectURL(blobUrl); } catch (e) {}
    }
  }

  // Cache first: a cached song plays instantly with no network round trip. An uncached song
  // streams straight from the CDN (no waiting for a full download) and is saved for next time.
  async function resolveAudioUrlFromCache(url) {
    var absUrl = toAbsoluteUrl(url);
    if (!isCacheableAudioUrl(absUrl)) return String(url || "");
    if (_audioBlobUrlBySource[absUrl]) return _audioBlobUrlBySource[absUrl];
    try {
      var cache = await caches.open(AUDIO_CACHE_NAME);
      var resp = await cache.match(absUrl);
      if (resp && resp.ok) {
        var blob = await resp.blob();
        if (blob && blob.size) {
          var blobUrl = URL.createObjectURL(blob);
          rememberAudioBlobUrl(absUrl, blobUrl);
          return blobUrl;
        }
      }
    } catch (e) {}
    primeAudioCache(absUrl);
    return absUrl;
  }

  var _audioCacheInFlight = Object.create(null);

  async function cacheAudioUrl(url) {
    var absUrl = toAbsoluteUrl(url);
    if (!isCacheableAudioUrl(absUrl)) return false;
    if (_audioCacheInFlight[absUrl]) return _audioCacheInFlight[absUrl];
    var job = (async function () {
      try {
        var cache = await caches.open(AUDIO_CACHE_NAME);
        var hit = await cache.match(absUrl);
        if (hit && hit.ok) return true;
        var resp = await fetch(absUrl, { mode: "cors", credentials: "omit" });
        if (!resp || !resp.ok || resp.status !== 200) return false;
        await cache.put(absUrl, resp);
        return true;
      } catch (e) {
        return false;
      } finally {
        delete _audioCacheInFlight[absUrl];
      }
    })();
    _audioCacheInFlight[absUrl] = job;
    return job;
  }

  function primeAudioCache(url) {
    cacheAudioUrl(url).catch(function () {});
  }

  // Download the group's whole library in the background (once per app window) so every
  // song plays from local storage even if the connection drops. Removed songs are evicted.
  // It must never compete with page loads or playback: it starts after the app has finished
  // loading and the browser is idle, fetches one file at a time at low priority, and pauses
  // while a song is streaming from the network or the device is offline.
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function whenAppLoaded(host) {
    return new Promise(function (resolve) {
      var go = function () {
        var idle = host.requestIdleCallback || window.requestIdleCallback;
        setTimeout(function () { idle ? idle(function () { resolve(); }, { timeout: 15000 }) : resolve(); }, 8000);
      };
      try {
        if (host.document.readyState === "complete") go();
        else host.addEventListener("load", go, { once: true });
      } catch (e) { go(); }
    });
  }

  function playbackIsStreaming() {
    try {
      var player = getGlobalAudioPlayer();
      if (!player || player.paused) return false;
      var src = player.currentSrc || player.src || "";
      return /^https?:/i.test(src);  // blob: = playing from the local cache
    } catch (e) {
      return false;
    }
  }

  async function prefetchOne(absUrl) {
    var cache = await caches.open(AUDIO_CACHE_NAME);
    var hit = await cache.match(absUrl);
    if (hit && hit.ok) return;
    var resp = await fetch(absUrl, { mode: "cors", credentials: "omit", priority: "low" });
    if (resp && resp.ok && resp.status === 200) await cache.put(absUrl, resp);
  }

  function prefetchSongLibrary() {
    var host;
    try { host = window.top || window; host.document; } catch (e) { host = window; }
    if (host.__nshSongPrefetch || typeof caches === "undefined") return;
    host.__nshSongPrefetch = true;
    whenAppLoaded(host).then(async function () {
      try {
        if (navigator.connection && navigator.connection.saveData) return;
        if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(function () {});
        var resp = await fetch("/songs", { credentials: "same-origin", priority: "low" });
        if (!resp.ok) { host.__nshSongPrefetch = false; return; }
        var songs = await resp.json();
        var wanted = Object.create(null);
        Object.values((songs && songs.library) || {}).forEach(function (song) {
          var absUrl = toAbsoluteUrl(song && song.url);
          if (isCacheableAudioUrl(absUrl)) wanted[absUrl] = true;
        });
        var cache = await caches.open(AUDIO_CACHE_NAME);
        var cachedKeys = await cache.keys();
        var have = Object.create(null);
        for (var i = 0; i < cachedKeys.length; i++) {
          var key = cachedKeys[i].url;
          if (wanted[key]) have[key] = true;
          else await cache.delete(cachedKeys[i]);
        }
        var queue = Object.keys(wanted).filter(function (u) { return !have[u]; });
        while (queue.length) {
          if (navigator.onLine === false || playbackIsStreaming() || _audioCacheInFlight[queue[0]]) {
            await sleep(5000);
            continue;
          }
          try { await prefetchOne(queue.shift()); } catch (e) {}
          await sleep(250);
        }
      } catch (e) {
        host.__nshSongPrefetch = false;
      }
    });
  }
  prefetchSongLibrary();

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

  function isRelayLoopActive(opts) {
    var relay = opts && opts.relayLoop;
    if (!relay) return false;
    return typeof relay.isActive === "function" ? !!relay.isActive() : true;
  }

  // Relay loop: restart the track from the top just before it ends, with a short volume
  // dip (fade out over the last fadeSec, fade back in over the first fadeSec) instead of
  // letting it end and continue the sequence. Turning relay off lets the current pass end normally.
  function runRelayLoop(player, loadToken, targetVolume, opts) {
    var fadeSec = Number(opts.relayLoop.fadeSec) > 0 ? Number(opts.relayLoop.fadeSec) : 0.6;
    var endLeadSec = 0.05;
    var looped = false;
    var touched = false;
    var lastTime = 0;
    var timer = setInterval(function () {
      // Any jump back toward the start (incl. the onended fallback) fades back in.
      if (player.currentTime < lastTime - 1) looped = true;
      lastTime = player.currentTime;
      if (player.dataset.audioLoadToken !== loadToken) {
        clearInterval(timer);
        return;
      }
      if (!isRelayLoopActive(opts)) {
        if (touched) player.volume = targetVolume;
        touched = false;
        looped = false;
        return;
      }
      var duration = player.duration;
      if (player.paused || !isFinite(duration) || duration <= fadeSec * 4) return;
      var rate = player.playbackRate || 1;
      var remaining = (duration - player.currentTime) / rate - endLeadSec;
      if (remaining <= 0) {
        looped = true;
        touched = true;
        player.volume = 0;
        player.currentTime = 0;
        lastTime = 0;
        return;
      }
      var gain = Math.min(1, remaining / fadeSec);
      if (looped) gain = Math.min(gain, (player.currentTime / rate) / fadeSec);
      if (gain < 1 || looped) {
        touched = true;
        player.volume = Math.max(0, Math.min(1, targetVolume * gain));
      }
      if (looped && gain >= 1) looped = false;
    }, 20);
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
            if (isRelayLoopActive(opts) && player.dataset.audioLoadToken === loadToken) {
              // Fallback if the relay timer was throttled and missed the end: restart immediately.
              player.currentTime = 0;
              var p0 = player.play();
              if (p0 && typeof p0.catch === "function") p0.catch(function () {});
              return;
            }
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
          if (opts.relayLoop) {
            runRelayLoop(player, loadToken, targetVolume, opts);
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

  function initHotkeys(apiFn) {
    var callApi = apiFn || api;
    var hotkeyMap = {};
    var songsLibrary = {};

    function getRoleUrl(role) {
      var entry = Object.values(songsLibrary || {}).find(function (s) {
        return (s.roles || []).includes(role);
      });
      return entry ? (entry.url || "") : "";
    }

    function playRole(role) {
      var url = getRoleUrl(role);
      if (!url) return;
      if (window.top && window.top !== window) {
        window.top.postMessage({type: "play-audio", url: url}, "*");
      } else {
        var audio = new Audio(url);
        audio.play().catch(function () {});
      }
    }

    function handleHotkey(evt) {
      if (evt.defaultPrevented) return;
      if (evt.metaKey || evt.ctrlKey || evt.altKey) return;
      if (isTypingTarget(evt.target)) return;
      var key = evt.key;
      if (!/^\d$/.test(key)) return;
      var role = hotkeyMap[key];
      if (!role) return;
      evt.preventDefault();
      playRole(role);
    }

    function loadLibrary() {
      return callApi("/songs").then(function (r) { return r.json(); }).then(function (data) {
        songsLibrary = (data && data.library) || {};
      }).catch(function () {});
    }

    function loadHotkeys() {
      return fetchAppSettings(callApi).then(function (data) {
        hotkeyMap = data.hotkeys || {};
      });
    }

    loadLibrary();
    loadHotkeys();
    document.addEventListener("keydown", handleHotkey);
    window.addEventListener("message", function (evt) {
      if (evt && evt.data && evt.data.type === "settings-updated") {
        loadHotkeys();
      }
    });
  }

  window.PageShared = {
    api: api,
    getGroup: getGroup,
    isTypingTarget: isTypingTarget,
    initHotkeys: initHotkeys,
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
