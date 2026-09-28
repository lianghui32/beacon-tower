/**
 * scripts/shoot_docs.mjs — 从线上实例重做 README 的那套截图
 *
 *   node scripts/shoot_docs.mjs                    全量重拍 docs/screenshots/
 *   node scripts/shoot_docs.mjs only dashboard geo  只重拍指定几张
 *
 * 零依赖：Node >= 22 自带 fetch/WebSocket，直连无头 Chrome 的 CDP。
 * 登录凭据取自线上登录页（只读演示账号本来就对访客公开），不落盘、不打印。
 * 浏览器路径用 CHROME_PATH 覆盖。
 *
 * 为什么每张图都带 ?minutes=：默认"近 1 小时"里演示站几乎没有流量，
 * 曲线全是平线、异常检测也检不出点，截图要选到能看到真实形态的窗口。
 */
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';

const CHROME = process.env.CHROME_PATH || {
  win32: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  darwin: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  linux: '/usr/bin/google-chrome',
}[process.platform];

const BASE = process.env.OBS_SHOOT_BASE || 'https://beacon.lianghui.vip';
const OUT = path.resolve('docs/screenshots');
const PORT = Number(process.env.OBS_SHOOT_PORT || 9345);
const W = 1484, H = 845;

// [文件名, 路由, 进页面后要执行的 JS]
const PAGES = [
  ['dashboard', '/?minutes=360'],
  ['host', '/hosts/?minutes=360'],
  ['logs', '/logs/'],
  ['geo', '/analytics/geo/'],
  ['dashboard_custom', '/monitor/dashboard/'],
  ['apm', '/monitor/apm/?minutes=360'],
  ['database', '/monitor/apm/database/?minutes=360'],
  ['trace', 'TRACE'],
  ['rum', '/rum/?minutes=1440'],
  ['alerts', '/alerts/events/'],
  ['ops_slo', '/ops/slo/'],
  ['ops_inspection', '/ops/inspection/'],
  ['analytics_report', '/analytics/report/'],
  ['analytics_anomaly', '/analytics/?minutes=360', 'runAnomaly()'],
  ['analytics_corr', '/analytics/?minutes=360', "showTab('corr'); runCorr()"],
  ['diagnose', '/monitor/diagnose/'],
];

const ONLY = process.argv[2] === 'only' ? new Set(process.argv.slice(3)) : null;
const PROFILE = path.join(os.tmpdir(), 'obs-shoot-chrome');
let child = null;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitDebugger() {
  for (let i = 0; i < 80; i++) {
    try {
      const j = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
    } catch { /* Chrome 还没起监听 */ }
    await sleep(250);
  }
  throw new Error('CDP 未就绪：Chrome 没起来或端口被占');
}

class CDP {
  constructor(url) { this.id = 0; this.pending = new Map(); this.handlers = []; this.url = url; }
  async connect() {
    this.ws = new WebSocket(this.url);
    await new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = rej; });
    this.ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const p = this.pending.get(m.id); this.pending.delete(m.id);
        m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
      } else if (m.method) this.handlers.forEach(h => h(m));
    };
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    const pr = new Promise((res, rej) => this.pending.set(id, { res, rej }));
    this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    return pr;
  }
  waitEvent(method, timeout = 8000) {
    return new Promise((res, rej) => {
      const h = m => {
        if (m.method === method) { clearTimeout(t); this.handlers = this.handlers.filter(x => x !== h); res(m); }
      };
      const t = setTimeout(() => { this.handlers = this.handlers.filter(x => x !== h); rej(new Error('timeout')); }, timeout);
      this.handlers.push(h);
    });
  }
  async ev(sid, expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sid);
    if (r.exceptionDetails) throw new Error('页面 JS 异常: ' + JSON.stringify(r.exceptionDetails).slice(0, 200));
    return r.result.value;
  }
}

async function goto(cdp, sid, url) {
  // load 事件可能因页内轮询迟迟不来：等不到就退化成轮询 readyState，别把整轮卡死
  const loaded = cdp.waitEvent('Page.loadEventFired').catch(() => null);
  await cdp.send('Page.navigate', { url }, sid);
  if (await loaded) { await sleep(400); return 'load'; }
  for (let i = 0; i < 40; i++) {
    if (await cdp.ev(sid, 'document.readyState') === 'complete') return 'readyState';
    await sleep(200);
  }
  return 'slow';
}

const CHART_STATE = `(function(){
  var hosts=[].slice.call(document.querySelectorAll('[_echarts_instance_]'));
  if(!hosts.length) return 'none';
  var filled=0;
  hosts.forEach(function(el){
    try{ var i=echarts.getInstanceByDom(el); if(!i) return;
      var s=(i.getOption().series||[]);
      if(s.some(function(x){return (x.data||[]).length;})) filled++;
    }catch(e){}
  });
  return filled+'/'+hosts.length;
})()`;

// 顺手复核侧栏：条目顺序、同名重复、当前页高亮——改版后这三项最容易被碰坏
const NAV_STATE = `(function(){
  var secs=[].slice.call(document.querySelectorAll('.sidebar .nav-sec'));
  var order=secs.map(function(s){
    var a=s.querySelector(':scope > a'), g=s.querySelector('.nav-group');
    return (a?a.textContent.trim():g.textContent.replace(/[▾▸]/g,'').trim());
  });
  var dup=0, seen={};
  secs.forEach(function(s){
    [].slice.call(s.querySelectorAll('a, .nav-group')).forEach(function(e){
      var k=e.textContent.replace(/[▾▸]/g,'').trim(); if(seen[k]) dup++; seen[k]=1;
    });
  });
  var act=document.querySelector('.sidebar a.active');
  return JSON.stringify({order:order, dup:dup, active:act?act.textContent.trim():null});
})()`;

async function main() {
  if (!fs.existsSync(CHROME)) throw new Error('找不到 Chrome，用 CHROME_PATH 指定：' + CHROME);
  fs.mkdirSync(OUT, { recursive: true });
  fs.rmSync(PROFILE, { recursive: true, force: true });
  child = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    `--window-size=${W},${H}`, '--no-first-run', '--no-default-browser-check',
    '--remote-allow-origins=*',
    // 橙云会掐 HeadlessChrome UA：同一 URL curl 200、无头里却拿不到登录框
    '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      + '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    '--disable-gpu', 'about:blank',
  ], { stdio: 'ignore' });

  const cdp = new CDP(await waitDebugger());
  await cdp.connect();
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  await cdp.send('Page.enable', {}, sessionId);
  await cdp.send('Runtime.enable', {}, sessionId);
  await cdp.send('Emulation.setDeviceMetricsOverride',
    { width: W, height: H, deviceScaleFactor: 1, mobile: false }, sessionId);

  let logged = '';
  for (let i = 0; i < 4 && !/^200/.test(logged); i++) {
    const how = await goto(cdp, sessionId, BASE + '/accounts/login/');
    await sleep(700);
    logged = await cdp.ev(sessionId, `(async function(){
      var box=document.querySelector('.demo-box'); if(!box) return 'no-box[${how}]';
      var t=box.innerText.replace(/\\s+/g,' ');
      var f=document.querySelector('form');
      f.querySelector('[name=username]').value=(t.match(/用户名\\s*([\\w.@-]+)/)||[])[1];
      f.querySelector('[name=password]').value=(t.match(/密码\\s*([\\w.-]+)/)||[])[1];
      var r=await fetch('/accounts/login/',{method:'POST',body:new FormData(f),redirect:'follow'});
      return r.status;
    })()`);
    console.log('LOGIN 尝试', i + 1, '→', logged);
  }
  if (!/^200/.test(logged)) throw new Error('登录失败: ' + logged);

  await goto(cdp, sessionId, BASE + '/monitor/apm/');
  const traceId = await cdp.ev(sessionId, `(async function(){
    var r=await fetch('/monitor/apm/api/traces/?minutes=1440'); var j=await r.json();
    var rows=j.rows||[];
    var pick=rows.find(function(x){return (x.sql_count||0)>=3;})||rows[0];
    return (pick&&pick.trace_id)||'';
  })()`);
  console.log('TRACE_ID', traceId || '(未取到，trace 那张会拍成缺失页)');

  for (const [name, route, action] of PAGES) {
    if (ONLY && !ONLY.has(name)) continue;
    const url = route === 'TRACE' ? `${BASE}/monitor/apm/trace/${traceId}/` : BASE + route;
    const mode = await goto(cdp, sessionId, url);
    if (action) {
      await cdp.ev(sessionId, `(function(){ try { ${action}; return 1 } catch(e){ return 0 } })()`);
      await sleep(1500);
    }
    let state = 'pending';
    for (let i = 0; i < 30; i++) {
      state = await cdp.ev(sessionId, CHART_STATE);
      if (state === 'none' || (state.includes('/') && Number(state.split('/')[0]) > 0)) break;
      await sleep(300);
    }
    await sleep(1200);   // 让 ECharts 把最后一批数据真的画到 canvas 上
    const nv = JSON.parse(await cdp.ev(sessionId, NAV_STATE));
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' }, sessionId);
    fs.writeFileSync(path.join(OUT, `${name}.png`), Buffer.from(shot.data, 'base64'));
    console.log(`${name.padEnd(18)} ${mode.padEnd(10)} charts=${state.padEnd(6)} 高亮=${nv.active} 同名重复=${nv.dup}`);
    if (name === 'dashboard') console.log('  侧栏顺序:', nv.order.join(' | '));
  }
  cdp.ws.close();
}

main().then(() => finish(0)).catch(e => { console.error('FAIL', e.message); finish(1); });

function finish(code) {
  try {
    if (child) {
      process.platform === 'win32'
        ? execFileSync('taskkill', ['//PID', String(child.pid), '//T', '//F'], { stdio: 'ignore' })
        : child.kill('SIGKILL');
    }
  } catch { /* 已经退了 */ }
  process.exit(code);
}
