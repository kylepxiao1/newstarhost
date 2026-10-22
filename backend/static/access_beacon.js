// Reports basic browser details once per page load to /access-log/client (see backend/access_log.py).
(function () {
  if (window.__nshAccessBeaconSent) return;
  window.__nshAccessBeaconSent = true;

  function sessionId() {
    try {
      var id = sessionStorage.getItem("nsh_session_id");
      if (!id) {
        id = Math.random().toString(36).slice(2) + Date.now().toString(36);
        sessionStorage.setItem("nsh_session_id", id);
      }
      return id;
    } catch (e) {
      return "";
    }
  }

  function collect() {
    var nav = window.navigator || {};
    var conn = nav.connection || {};
    var uad = nav.userAgentData || null;
    var timing = (performance.getEntriesByType && performance.getEntriesByType("navigation")[0]) || null;
    var tz = "";
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
    return {
      session_id: sessionId(),
      page: location.pathname + location.search,
      referrer: document.referrer || "",
      in_iframe: window.top !== window,
      title: document.title || "",
      timezone: tz,
      tz_offset_min: new Date().getTimezoneOffset(),
      languages: nav.languages ? Array.prototype.slice.call(nav.languages, 0, 5) : [nav.language],
      platform: (uad && uad.platform) || nav.platform || "",
      mobile: uad ? !!uad.mobile : /Mobi|Android/i.test(nav.userAgent || ""),
      browser_brands: uad && uad.brands ? uad.brands.map(function (b) { return b.brand + " " + b.version; }) : [],
      screen: { w: screen.width, h: screen.height, avail_w: screen.availWidth, avail_h: screen.availHeight, color_depth: screen.colorDepth },
      viewport: { w: window.innerWidth, h: window.innerHeight },
      pixel_ratio: window.devicePixelRatio || 1,
      touch_points: nav.maxTouchPoints || 0,
      cpu_cores: nav.hardwareConcurrency || null,
      memory_gb: nav.deviceMemory || null,
      connection: conn.effectiveType ? { type: conn.effectiveType, downlink_mbps: conn.downlink, rtt_ms: conn.rtt } : null,
      dark_mode: !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches),
      do_not_track: nav.doNotTrack || null,
      load_ms: timing ? Math.round(timing.loadEventEnd || timing.domContentLoadedEventEnd || 0) : null,
    };
  }

  function send() {
    try {
      var body = JSON.stringify(collect());
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/access-log/client", new Blob([body], { type: "application/json" }));
      } else {
        fetch("/access-log/client", { method: "POST", body: body, keepalive: true, headers: { "Content-Type": "application/json" } });
      }
    } catch (e) {}
  }

  if (document.readyState === "complete") setTimeout(send, 0);
  else window.addEventListener("load", function () { setTimeout(send, 0); });
})();
