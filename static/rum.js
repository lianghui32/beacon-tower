/**
 * rum.js — 自研前端性能监控 SDK（零依赖）
 *
 * 采集能力（对标腾讯云前端性能监控 RUM）：
 *  - 页面访问 PV：首次加载 + SPA 路由变化（history hook）
 *  - 页面性能：TTFB / DOMReady / Load / FP / FCP / LCP（PerformanceObserver）
 *  - JS 异常：window.onerror + unhandledrejection（含堆栈）
 *  - API 监控：自动包装 XMLHttpRequest 与 fetch，记录 URL / 状态码 / 耗时
 *  - 静态资源：Resource Timing 中耗时 > 100ms 的条目
 *  - 自定义上报：window.obsRum('事件名', {任意 JSON})
 *
 * 使用：页面里先配置 window._OBS_RUM = { endpoint: '/rum/beacon/', app: 'forum' }
 * 再引入本文件即可。数据批量上报（5 秒 / 页面离开时），sendBeacon 优先。
 */
(function () {
  'use strict';
  var CFG = window._OBS_RUM || {};
  var ENDPOINT = CFG.endpoint || '/rum/beacon/';
  var APP = CFG.app || 'web';
  var queue = [];
  var flushTimer = null;

  // ---------- 基础信息 ----------
  var sessionId = (function () {
    try {
      var sid = sessionStorage.getItem('obs_sid');
      if (!sid) {
        sid = Math.random().toString(36).slice(2) + Date.now().toString(36);
        sessionStorage.setItem('obs_sid', sid);
      }
      return sid;
    } catch (e) { return 'anon-' + Date.now().toString(36); }
  })();

  function detectDevice() {
    var ua = navigator.userAgent;
    var browser = '未知浏览器';
    if (/Edg\//.test(ua)) browser = 'Edge';
    else if (/Chrome\//.test(ua)) browser = 'Chrome';
    else if (/Firefox\//.test(ua)) browser = 'Firefox';
    else if (/Safari\//.test(ua)) browser = 'Safari';
    var os = /Windows/.test(ua) ? 'Windows'
      : /Mac OS/.test(ua) ? 'macOS'
      : /Android/.test(ua) ? 'Android'
      : /iPhone|iPad/.test(ua) ? 'iOS' : 'Other';
    return browser + '/' + os;
  }

  var DEVICE = detectDevice();
  var SCREEN = (screen && screen.width ? screen.width + 'x' + screen.height : '');

  // ---------- 上报队列 ----------
  function enqueue(ev) {
    ev.app = APP;
    ev.session_id = sessionId;
    ev.device = DEVICE;
    ev.screen = SCREEN;
    ev.ts = Date.now();
    if (ev.page_url === undefined) ev.page_url = location.href.slice(0, 256);
    queue.push(ev);
    if (queue.length >= 10) flush();
    else scheduleFlush();
  }

  function scheduleFlush() {
    if (flushTimer) return;
    flushTimer = setTimeout(function () { flush(); }, 5000);
  }

  function flush() {
    if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
    if (!queue.length) return;
    var batch = queue.splice(0, queue.length);
    var body = JSON.stringify(batch);
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: 'application/json' }));
        return;
      }
    } catch (e) { /* fallthrough */ }
    try {
      fetch(ENDPOINT, {
        method: 'POST', body: body, keepalive: true,
        headers: { 'Content-Type': 'application/json' },
      }).catch(function () {});
    } catch (e) { /* 上报失败静默 */ }
  }

  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') flush();
  });

  // ---------- PV（含 SPA 路由） ----------
  function trackPv() {
    // 同一 URL 的连续路由调用（如 replaceState 高频触发）只计一次，避免 PV 虚高
    var last = trackPv._lastUrl;
    var url = location.href;
    if (url === last) return;
    trackPv._lastUrl = url;
    enqueue({ type: 'pv', referrer: (document.referrer || '').slice(0, 256) });
  }
  (function hookHistory() {
    function wrap(type) {
      var orig = history[type];
      history[type] = function () {
        var ret = orig.apply(this, arguments);
        setTimeout(trackPv, 0);
        return ret;
      };
    }
    wrap('pushState');
    wrap('replaceState');
    window.addEventListener('popstate', trackPv);
  })();

  // ---------- 页面性能 ----------
  function reportPerf() {
    var nav = performance.getEntriesByType &&
      performance.getEntriesByType('navigation')[0];
    var ev = { type: 'perf' };
    if (nav) {
      ev.ttfb_ms = Math.round(nav.responseStart - nav.requestStart) || 0;
      ev.dom_ready_ms = Math.round(nav.domContentLoadedEventEnd - nav.startTime) || 0;
      ev.load_ms = Math.round(nav.loadEventEnd - nav.startTime) || 0;
    }
    var paints = performance.getEntriesByType ? performance.getEntriesByType('paint') : [];
    (paints || []).forEach(function (p) {
      if (p.name === 'first-paint') ev.fp_ms = Math.round(p.startTime);
      if (p.name === 'first-contentful-paint') ev.fcp_ms = Math.round(p.startTime);
    });
    // LCP：仅首屏一次；回调晚于入队时写回同一对象（flush 前生效即随本批上报）
    try {
      var po = new PerformanceObserver(function (list) {
        var entries = list.getEntries();
        if (entries.length) {
          ev.lcp_ms = Math.round(entries[entries.length - 1].startTime);
          // 若 ev 已随更早的 flush 发走（罕见），把迟到值作为一条补录上报
          if (ev.__sent) {
            enqueue({ type: 'perf', lcp_ms: ev.lcp_ms, page_url: ev.page_url });
          }
          scheduleFlush();
        }
      });
      po.observe({ type: 'largest-contentful-paint', buffered: true });
    } catch (e) { /* 浏览器不支持则略过 */ }
    // load 事件后延迟上报，尽量拿到 Load 完成值与 LCP
    setTimeout(function () {
      if (nav && nav.loadEventEnd === 0) ev.load_ms = Math.round(performance.now());
      ev.__sent = true;
      enqueue(ev);
      reportResources();
    }, 800);
  }

  // ---------- 静态资源 ----------
  function reportResources() {
    try {
      var entries = performance.getEntriesByType('resource') || [];
      var reported = 0;
      for (var i = 0; i < entries.length && reported < 20; i++) {
        var r = entries[i];
        var dur = Math.round(r.duration);
        if (dur < 100) continue;
        var type = 'other';
        if (/\.js($|\?)/.test(r.name) || r.initiatorType === 'script') type = 'script';
        else if (/\.css($|\?)/.test(r.name) || r.initiatorType === 'css' || r.initiatorType === 'link') type = 'css';
        else if (/\.(png|jpe?g|gif|webp|svg|ico)($|\?)/.test(r.name) || r.initiatorType === 'img') type = 'img';
        else if (r.initiatorType === 'xmlhttprequest' || r.initiatorType === 'fetch') continue; // API 已单独上报
        enqueue({
          type: 'resource',
          page_url: location.href.slice(0, 256),
          r_type: type,
          r_url: r.name.slice(0, 256),
          r_duration_ms: dur,
          r_size_kb: r.transferSize ? Math.round(r.transferSize / 102.4) / 10 : 0,
        });
        reported++;
      }
    } catch (e) { /* 忽略 */ }
  }

  // ---------- JS 异常 ----------
  window.addEventListener('error', function (e) {
    // 过滤资源加载错误（img/script 404 等）：其 target 非 window，
    // 误报成 "Script error" 会刷爆异常分析页；资源耗时已由 Resource Timing 覆盖
    if (e.target && e.target !== window && !(e instanceof ErrorEvent)) return;
    enqueue({
      type: 'error',
      err_message: (e.message || 'Script error').slice(0, 280),
      err_stack: (e.error && e.error.stack ? String(e.error.stack).slice(0, 2000) : ''),
    });
  }, true);
  window.addEventListener('unhandledrejection', function (e) {
    var reason = e.reason;
    enqueue({
      type: 'error',
      err_message: ('UnhandledRejection: ' + (reason && reason.message ? reason.message : reason)).slice(0, 280),
      err_stack: (reason && reason.stack ? String(reason.stack).slice(0, 2000) : ''),
    });
  });

  // ---------- API 监控（XHR + fetch 自动包装） ----------
  function wrapXhr() {
    if (!window.XMLHttpRequest) return;
    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__obs = { method: method, url: String(url).slice(0, 250), start: 0 };
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      var meta = this.__obs;
      if (meta && !isRumUrl(meta.url)) {
        meta.start = Date.now();
        this.addEventListener('loadend', function () {
          enqueue({
            type: 'api',
            api_url: meta.url,
            api_method: meta.method,
            api_status: this.status,
            api_duration_ms: Date.now() - meta.start,
            api_ok: this.status >= 200 && this.status < 400,
          });
        });
      }
      return origSend.apply(this, arguments);
    };
  }

  function wrapFetch() {
    if (!window.fetch) return;
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var method = (init && init.method) || (input && input.method) || 'GET';
      if (isRumUrl(url)) return origFetch.apply(this, arguments);
      var start = Date.now();
      return origFetch.apply(this, arguments).then(function (resp) {
        enqueue({
          type: 'api',
          api_url: String(url).slice(0, 250),
          api_method: String(method),
          api_status: resp.status,
          api_duration_ms: Date.now() - start,
          api_ok: resp.status >= 200 && resp.status < 400,
        });
        return resp;
      }, function (err) {
        enqueue({
          type: 'api',
          api_url: String(url).slice(0, 250),
          api_method: String(method),
          api_status: 0,
          api_duration_ms: Date.now() - start,
          api_ok: false,
        });
        throw err;
      });
    };
  }

  function isRumUrl(url) {
    // 只排除自身上报端点与 /metrics 端点（精确匹配，避免误伤 /api/metrics/xxx 等业务路径）
    if (url.indexOf(ENDPOINT) !== -1) return true;
    try {
      var p = new URL(url, location.href);
      return p.origin === location.origin && (p.pathname === '/metrics' || p.pathname === ENDPOINT);
    } catch (e) {
      return false;
    }
  }

  // ---------- 自定义上报 ----------
  window.obsRum = function (eventName, data) {
    enqueue({
      type: 'custom',
      event_name: String(eventName).slice(0, 60),
      payload: JSON.stringify(data || {}),
      page_url: location.href.slice(0, 256),
    });
  };

  // ---------- 启动 ----------
  function start() {
    wrapXhr();
    wrapFetch();
    trackPv();
    if (document.readyState === 'complete') reportPerf();
    else window.addEventListener('load', reportPerf);
  }
  start();
})();
