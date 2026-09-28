/**
 * obs/common.js — 平台页面公共工具
 * 侧边栏高亮 / JSON 拉取 / ECharts 快捷构建 / 时间范围选择 / 自动刷新
 */

// ---------- 侧边栏高亮：按前缀最长匹配 ----------
(function () {
  var path = location.pathname;
  var links = document.querySelectorAll('.sidebar a');
  var best = null, bestLen = -1;
  links.forEach(function (a) {
    var href = a.getAttribute('href');
    if (href === '/') {
      if (path === '/' && bestLen < 1) { best = a; bestLen = 1; }
      return;
    }
    if (path.indexOf(href) === 0 && href.length > bestLen) { best = a; bestLen = href.length; }
  });
  if (best) best.classList.add('active');
})();

// ---------- fetch JSON（失败自动重试一次，避免首屏偶发抖动） ----------
function obsGet(url, retried) {
  return fetch(url).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).catch(function (e) {
    if (retried) throw e;
    return new Promise(function (resolve) { setTimeout(resolve, 1500); })
      .then(function () { return obsGet(url, true); });
  });
}
function obsPost(url, data) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }).then(function (r) { return r.json(); });
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function getParam(name, def) {
  var m = new URLSearchParams(location.search).get(name);
  return m === null ? def : m;
}

// ---------- ECharts 快捷构建 ----------
function mkChart(id) {
  var el = document.getElementById(id);
  if (!el) return null;
  return echarts.init(el);
}

function lineOption(series, opts) {
  opts = opts || {};
  return {
    tooltip: { trigger: 'axis' },
    legend: series.length > 1 ? { data: series.map(function (s) { return s.name; }) } : undefined,
    grid: { left: 55, right: opts.right || 30, top: series.length > 1 ? 40 : 30, bottom: 32 },
    xAxis: { type: 'category', data: series[0].x, axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', name: opts.unit || '', scale: opts.scale !== false },
    series: series.map(function (s, i) {
      var colors = ['#2563eb', '#dc2626', '#16a34a', '#7c3aed', '#ea580c', '#0891b2'];
      return {
        name: s.name, type: opts.type || 'line', smooth: true, data: s.y,
        areaStyle: (s.area || (opts.area && i === 0)) ? { opacity: 0.14 } : undefined,
        itemStyle: { color: s.color || colors[i % colors.length] },
        markLine: s.markLine ? {
          silent: true, symbol: 'none',
          data: [{ yAxis: s.markLine, name: '阈值' }],
          lineStyle: { color: '#dc2626', type: 'dashed' },
          label: { formatter: '阈值 {c}', fontSize: 10 },
        } : undefined,
        markPoint: s.markPoints ? {
          data: s.markPoints.map(function (p) {
            return { coord: [p.x, p.y], value: p.z || '异', symbolSize: 34,
                     itemStyle: { color: '#dc2626' }, label: { fontSize: 9, color: '#fff' } };
          }),
        } : undefined,
      };
    }),
  };
}

function barOption(categories, values, opts) {
  opts = opts || {};
  return {
    // 自定义 formatter 并转义：类目名（如自定义事件名）可能来自外部上报，默认 HTML 渲染存在 XSS 风险
    tooltip: { trigger: 'axis', formatter: function (params) {
      var list = Array.isArray(params) ? params : [params];
      return list.map(function (p) {
        return esc(p.axisValueLabel != null ? p.axisValueLabel : p.name)
          + '<br>' + esc(p.seriesName) + ': <b>' + esc(p.value) + '</b>';
      }).join('<br>');
    } },
    grid: { left: 55, right: 20, top: 30, bottom: opts.bottom || 70 },
    xAxis: { type: 'category', data: categories, axisLabel: { rotate: opts.rotate != null ? opts.rotate : 25, fontSize: 11 } },
    yAxis: { type: 'value', name: opts.unit || '' },
    series: [{ type: 'bar', data: values, itemStyle: { color: opts.color || '#2563eb' }, barMaxWidth: 36 }],
  };
}

function pieOption(data, opts) {
  opts = opts || {};
  return {
    // 同上：name 转义后再拼 HTML
    tooltip: { trigger: 'item', formatter: function (p) {
      return esc(p.name) + ': ' + esc(p.value) + ' (' + p.percent + '%)';
    } },
    legend: { bottom: 0, type: 'scroll', textStyle: { fontSize: 11 } },
    series: [{
      type: 'pie', radius: opts.radius || ['38%', '66%'], center: ['50%', '46%'],
      data: data, label: { fontSize: 11 },
    }],
  };
}

// ---------- 时间范围选择 ----------
var RANGES = [
  [15, '近 15 分钟'], [60, '近 1 小时'], [360, '近 6 小时'],
  [1440, '近 24 小时'], [4320, '近 3 天'],
];
function rangeSelect(current, onChange) {
  var sel = document.createElement('select');
  RANGES.forEach(function (r) {
    var o = document.createElement('option');
    o.value = r[0]; o.textContent = r[1];
    if (Number(current) === r[0]) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = function () {
    if (onChange) onChange(sel.value);
    else {
      var usp = new URLSearchParams(location.search);
      usp.set('minutes', sel.value);
      location.search = usp.toString();
    }
  };
  return sel;
}
// 自动把 class=range-slot 的占位填充为时间范围选择器
document.querySelectorAll('.range-slot').forEach(function (el) {
  el.appendChild(rangeSelect(el.getAttribute('data-minutes') || 60));
});

// ---------- 自动刷新 ----------
function poll(fn, ms) {
  fn();
  var timer = null;
  function start() { if (timer === null) timer = setInterval(fn, ms || 10000); }
  function stop() { if (timer !== null) { clearInterval(timer); timer = null; } }
  // 页面不可见时暂停轮询：省资源，也避免后台标签页堆积过期数据
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { fn(); start(); }
    else stop();
  });
  start();
  return timer;
}
window.addEventListener('resize', function () {
  document.querySelectorAll('.chart').forEach(function (el) {
    var inst = echarts.getInstanceByDom(el);
    if (inst) inst.resize();
  });
});
