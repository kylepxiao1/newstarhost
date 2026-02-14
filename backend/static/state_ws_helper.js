(function () {
  function defaultWsUrl() {
    return (location.origin.replace(/^http/, "ws")) + "/ws/state";
  }

  function connectStateSocket(options) {
    const cfg = options || {};
    const wsUrl = cfg.wsUrl || defaultWsUrl();
    const staleMs = Number(cfg.staleMs || 45000);
    const checkIntervalMs = Number(cfg.checkIntervalMs || 5000);
    const minDelayMs = Number(cfg.minDelayMs || 500);
    const maxDelayMs = Number(cfg.maxDelayMs || 30000);
    const jitterMs = Number(cfg.jitterMs || 400);

    let ws = null;
    let reconnectTimer = null;
    let staleTimer = null;
    let attempt = 0;
    let lastMsgAt = 0;
    let closedByPage = false;

    function call(fn, arg1, arg2) {
      if (typeof fn !== "function") return;
      try { fn(arg1, arg2); } catch (e) {}
    }

    function clearTimers() {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (staleTimer) {
        clearInterval(staleTimer);
        staleTimer = null;
      }
    }

    function scheduleReconnect() {
      if (closedByPage || reconnectTimer) return;
      call(cfg.onDisconnected);
      const delay = Math.min(maxDelayMs, minDelayMs * (2 ** Math.min(attempt, 8))) + Math.floor(Math.random() * jitterMs);
      attempt += 1;
      reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        connect();
      }, delay);
    }

    function armStaleCheck() {
      if (staleTimer) clearInterval(staleTimer);
      staleTimer = setInterval(function () {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        if (Date.now() - lastMsgAt > staleMs) {
          try { ws.close(); } catch (e) {}
        }
      }, checkIntervalMs);
    }

    function connect() {
      if (closedByPage) return;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
      try {
        ws = new WebSocket(wsUrl);
      } catch (e) {
        scheduleReconnect();
        return;
      }
      ws.onopen = function () {
        attempt = 0;
        lastMsgAt = Date.now();
        call(cfg.onConnected, ws);
        call(cfg.onOpen, ws);
        armStaleCheck();
      };
      ws.onmessage = function (evt) {
        lastMsgAt = Date.now();
        let data = null;
        try {
          data = JSON.parse(evt.data);
        } catch (e) {
          call(cfg.onParseError, e);
          return;
        }
        call(cfg.onMessage, data, evt);
        if (data && data.type === "state") {
          call(cfg.onState, data.payload, data);
        }
      };
      ws.onerror = function (err) {
        call(cfg.onError, err);
      };
      ws.onclose = function (evt) {
        call(cfg.onClose, evt);
        scheduleReconnect();
      };
    }

    function nudgeReconnect() {
      if (closedByPage) return;
      if (ws && ws.readyState === WebSocket.OPEN) {
        call(cfg.onWake, ws);
        return;
      }
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      connect();
    }

    function onVisibilityChange() {
      if (!document.hidden) nudgeReconnect();
    }

    function onOnline() {
      nudgeReconnect();
    }

    function onPageShow() {
      nudgeReconnect();
    }

    function onBeforeUnload() {
      close();
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("online", onOnline);
    window.addEventListener("pageshow", onPageShow);
    window.addEventListener("beforeunload", onBeforeUnload);

    function close() {
      if (closedByPage) return;
      closedByPage = true;
      clearTimers();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("pageshow", onPageShow);
      window.removeEventListener("beforeunload", onBeforeUnload);
      try { if (ws) ws.close(); } catch (e) {}
    }

    connect();

    return {
      close: close,
      nudge: nudgeReconnect,
      socket: function () { return ws; },
    };
  }

  window.StateWsHelper = {
    connect: connectStateSocket,
  };
})();
