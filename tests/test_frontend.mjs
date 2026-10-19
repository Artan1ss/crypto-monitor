// Pure JS unit tests, not browser automation. Run: node --test tests/test_frontend.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { gzipSync } from "node:zlib";

const source =
  ["viewport.js", "chart.js"]
    .map((name) =>
      readFileSync(
        new URL("../src/btc_monitor/hourly/web/" + name, import.meta.url),
        "utf8",
      ),
    )
    .join("\n") +
  "\n" +
  readFileSync(
    new URL("../src/btc_monitor/hourly/web/app.js", import.meta.url),
    "utf8",
  );
function sandbox() {
  const elements = new Map();
  const context = vm.createContext({
    Blob,
    DecompressionStream,
    Uint8Array,
    AbortSignal,
    document: {
      getElementById(id) {
        if (!elements.has(id))
          elements.set(id, {
            value: id === "range" ? "24" : "ALL",
            listeners: {},
            addEventListener(name, listener) {
              this.listeners[name] = listener;
            },
            getBoundingClientRect() {
              return { left: 0, top: 0, width: 1100, height: 790 };
            },
            focus() {},
            setPointerCapture() {},
          });
        return elements.get(id);
      },
    },
    fetch: () => new Promise(() => {}),
    setInterval: () => {},
  });
  vm.runInContext(source, context);
  return context;
}

test("API errors show safe actionable details and failed refresh preserves old data", async () => {
  const context = sandbox();
  vm.runInContext(
    `fetch=async()=>({ok:false,status:503,json:async()=>({detail:'数据库暂不可用，请启动Docker'})});`,
    context,
  );
  await assert.rejects(
    vm.runInContext("get('/hourly/bars')", context),
    /数据库暂不可用/,
  );
  await vm.runInContext(
    `state.loading=false;state.bars=[{start:'saved'}];refresh()`,
    context,
  );
  assert.equal(vm.runInContext("state.bars[0].start", context), "saved");
  assert.match(vm.runInContext("$('status').textContent", context), /Docker/);
  assert.equal(vm.runInContext("$('replay').disabled", context), true);
  assert.equal(vm.runInContext("state.loading", context), false);
});

test("live market renders even when signal/health database fails", async () => {
  const c = sandbox();
  vm.runInContext(
    `
    $('datasource').value='live'; render=()=>{};
    fetch=async path=>path.startsWith('/hourly/market')?{ok:true,json:async()=>({bars:[{price:{close:123}}],health:{price_fresh:true}})}:{ok:false,status:503,json:async()=>({detail:'database down'})};
  `,
    c,
  );
  await vm.runInContext("refresh()", c);
  assert.equal(vm.runInContext("state.bars[0].price.close", c), 123);
  assert.match(
    vm.runInContext("state.market.signal_warning", c),
    /信号档案暂不可用/,
  );
  assert.equal(vm.runInContext("state.dataError", c), null);
});

test("late archive requests cannot overwrite a newer live request", async () => {
  const c = sandbox();
  vm.runInContext(
    `
    render=()=>{}; const blocked=[];
    fetch=path=>path.startsWith('/hourly/market')?Promise.resolve({ok:true,json:async()=>({bars:[{price:{close:456}}],health:{price_fresh:true}})}):new Promise(resolve=>blocked.push(resolve));
    $('datasource').value='archive'; const older=refresh();
    $('datasource').value='live'; const newer=refresh();
  `,
    c,
  );
  // Resolve all auxiliary reads after the live market has had a chance to publish.
  await new Promise((resolve) => setTimeout(resolve, 0));
  vm.runInContext(
    `for(const done of blocked)done({ok:true,json:async()=>[]});`,
    c,
  );
  await vm.runInContext("Promise.all([older,newer])", c);
  assert.equal(vm.runInContext("state.bars[0].price.close", c), 456);
});

test("local snapshot uses offline mode, not a healthy live monitor", () => {
  const context = sandbox();
  vm.runInContext(
    `render=()=>{}; applyOffline({schema:'btc-hourly-review-v1',exported_at:'2026-10-16T00:00:00Z',bars:[{start:'2026-10-15T23:00:00Z',end:'2026-10-16T00:00:00Z'}],signals:[]});`,
    context,
  );
  assert.equal(vm.runInContext("state.offline", context), true);
  assert.equal(vm.runInContext("state.bars.length", context), 1);
  assert.equal(vm.runInContext("state.health.healthy", context), undefined);
});

test("plain and gzip review imports preserve all snapshot fields", async () => {
  const context = sandbox(),
    read = vm.runInContext("readReviewFile", context);
  const payload = {
    schema: "btc-hourly-review-v1",
    bars: [],
    signals: [{ id: "a", snapshot: { reason: "背离", input_ids: [1, 2] } }],
  };
  const encoded = JSON.stringify(payload);
  for (const [name, bytes] of [
    ["review.json", encoded],
    ["review.json.gz", gzipSync(encoded)],
  ]) {
    const blob = new Blob([bytes]);
    assert.equal(
      JSON.stringify(
        await read({ name, size: blob.size, stream: () => blob.stream() }),
      ),
      JSON.stringify(payload),
    );
  }
});

test("gzip expansion cannot bypass decoded size bound", async () => {
  const context = sandbox(),
    read = vm.runInContext("readReviewFile", context);
  const blob = new Blob([gzipSync(Buffer.alloc(50 * 1024 * 1024 + 1, 32))]);
  await assert.rejects(
    read({
      name: "oversized.gz",
      size: blob.size,
      stream: () => blob.stream(),
    }),
    /50 MiB/,
  );
});

test("offline time range slices bars and signals without network requests", () => {
  const context = sandbox();
  vm.runInContext(
    `render=()=>{}; state.offlineData={bars:Array.from({length:72},(_,i)=>({start:new Date(i*3600000).toISOString(),end:new Date((i+1)*3600000).toISOString()})),signals:[{bar_start:new Date(0).toISOString()},{bar_start:new Date(70*3600000).toISOString()}]};applyOfflineRange();`,
    context,
  );
  assert.equal(vm.runInContext("state.bars.length", context), 24);
  assert.equal(vm.runInContext("state.signals.length", context), 1);
});

test("partial paths are never labelled as finished horizon returns", () => {
  const format = vm.runInContext("outcomeReturn", sandbox());
  assert.equal(
    format({ status: "pending", signed_return_pct: "2.39" }),
    "尚未到期",
  );
  assert.equal(
    format({ status: "complete", coverage: "incomplete" }),
    "数据不足，未判定",
  );
  assert.match(
    format({
      status: "complete",
      coverage: "complete_minute_interior",
      signed_return_pct: "1",
    }),
    /1\.00%/,
  );
  assert.match(
    format({
      status: "complete",
      coverage: "incomplete",
      endpoint_exact: true,
      verdict: "direction_supported",
      signed_return_pct: "1",
    }),
    /1\.00%/,
  );
  assert.equal(
    format({ status: "complete", endpoint_exact: false }),
    "数据不足，未判定",
  );
});

test("spot gaps cannot interrupt valid futures and gaps reset only their own series", () => {
  const calculate = vm.runInContext("cvdSeries", sandbox());
  const bars = [0, 1, 2].map((i) => ({
    start: String(i),
    end: String(i + 1),
    complete: i !== 1,
    aggregated: {
      spot: { complete: i !== 1, delta_btc: i === 1 ? null : 2 },
      linear: { complete: true, delta_btc: 3 },
    },
  }));
  assert.equal(JSON.stringify(calculate(bars, "spot")), "[2,null,2]");
  assert.equal(JSON.stringify(calculate(bars, "linear")), "[3,6,9]");
});

test("1H chart shares viewport; pan, zoom, refresh and source switching preserve correct anchors", () => {
  const c = sandbox();
  vm.runInContext(
    `
    draw=()=>{};chartNav.redraw=()=>{};
    const history=Array.from({length:170},(_,i)=>({start:new Date(i*3600000).toISOString(),end:new Date((i+1)*3600000).toISOString()}));
    chartNav.sync(history.slice(0,168),'live');
    chartNav.view.count=40; chartNav.view.left=30;
    const first=chartNav.bars[30].start;
    chartNav.sync(history.slice(1,169),'live');
    if(chartNav.bars[Math.floor(chartNav.view.left)].start!==first)throw Error('historical anchor moved');
    $('chart').listeners.pointerdown({button:0,target:{closest:()=>null},clientX:500,pointerId:1});
    $('chart').listeners.pointermove({clientX:600});
    if(chartNav.view.left>=29)throw Error('drag must reveal older candles');
    $('chart').listeners.pointerup();
    const count=chartNav.view.count;
    $('chart').listeners.wheel({preventDefault(){},clientX:500,deltaY:-120});
    if(chartNav.view.count>=count)throw Error('wheel did not zoom');
    chartNav.latest(); chartNav.sync(history.slice(2,170),'live');
    if(chartNav.view.left+chartNav.view.count!==168)throw Error('live edge not followed');
    chartNav.sync(history.slice(0,72),'research');
    if(chartNav.view.count!==72||chartNav.view.left!==0)throw Error('mode switch failed');
  `,
    c,
  );
});

test("1H draw uses full-series CVD while markers stay on trigger candle and support clicks", async () => {
  const c = sandbox();
  vm.runInContext(
    `
    const svg={listeners:{},addEventListener(k,v){this.listeners[k]=v},getBoundingClientRect(){return {left:0,top:0,width:1100,height:790}},querySelector(){return {setAttribute(){}}},append(){}};
    $('chart').querySelector=()=>svg;
    const marker={dataset:{signal:'long'},listeners:{},addEventListener(k,v){this.listeners[k]=v}};
    $('chart').querySelectorAll=()=>[marker];
    state.bars=Array.from({length:60},(_,i)=>({start:new Date(i*3600000).toISOString(),end:new Date((i+1)*3600000).toISOString(),complete:true,price:{open:100,high:110,low:90,close:102},aggregated:{spot:{delta_btc:2,volume_btc:10},linear:{delta_btc:3,volume_btc:20}},oi_btc:100,funding_fraction_mean:0.0001}));
    state.signals=[{id:'long',direction:'LONG',tier:'CONFIRMED',bar_start:state.bars[20].start,generated_at:new Date(21*3600000+180000).toISOString(),entry:102,invalidation:89,origin:'retrospective',snapshot:{candidate:{sweep_diagnostics:{pivot_start:state.bars[5].start,level:92}}}},{id:'short',direction:'SHORT',tier:'CONFIRMED',bar_start:state.bars[40].start,generated_at:state.bars[41].end,entry:100,invalidation:112}];
    state.selected='long';state.market={};
    const original=JSON.stringify(state.bars);
    draw();
    if(JSON.stringify(state.bars)!==original)throw Error('chart mutated inputs');
  `,
    c,
  );
  const svg = vm.runInContext("$('chart').innerHTML", c);
  assert.match(svg, /data-sweep-anchor/);
  assert.match(svg, /data-sweep-level/);
  const x = Number(
    svg.match(/data-signal="long"[\s\S]*?<path d="M ([\d.]+)/)[1],
  );
  assert.ok(
    Math.abs(x - (12 + (20.5 / 60) * 978)) < 0.01,
    "arrow must use bar_start, not generated_at",
  );
  assert.match(svg, /fill="#46c6ac"/);
  assert.match(svg, /fill="#ee7b88"/);
  await vm.runInContext(
    "select=async id=>{state.selected=id}; marker.listeners.click()",
    c,
  );
  assert.equal(vm.runInContext("state.selected", c), "long");
  vm.runInContext(
    `
    const calculate=cvdSeries;const calls=[];
    cvdSeries=(bars,market)=>{calls.push(bars.length);return calculate(bars,market)};
    chartNav.view.count=12;chartNav.view.left=10;draw();
    if(calls.some(n=>n!==60))throw Error('viewport reset CVD baseline');
  `,
    c,
  );
});

test("1H signal navigation and UTC date jump are bounded to loaded data", () => {
  const c = sandbox();
  vm.runInContext(
    `
    draw=()=>{};chartNav.redraw=()=>{};select=async id=>{state.selected=id};
    const history=Array.from({length:168},(_,i)=>({start:new Date(Date.UTC(2026,9,12)+i*3600000).toISOString(),end:new Date(Date.UTC(2026,9,12)+(i+1)*3600000).toISOString()}));
    chartNav.sync(history,'live');
    state.signals=[{id:'new',bar_start:history[150].start},{id:'old',bar_start:history[20].start}];
    chartNav.navigate(1);
    if(state.selected!=='old')throw Error('signals not chronological');
    chartNav.navigate(1);
    if(state.selected!=='new')throw Error('next navigation failed');
    chartNav.jump('2026-10-14');
    if(!(chartNav.view.left<=48 && chartNav.view.left+chartNav.view.count>48))throw Error('date not visible');
    const left=chartNav.view.left;chartNav.jump('2026-09-01');
    if(chartNav.view.left!==left)throw Error('invalid date moved viewport');
    if(!$('chart-range').textContent.includes('不在已加载范围'))throw Error('missing date warning');
  `,
    c,
  );
});

test("background refresh does not refocus an offscreen selected signal", () => {
  const c = sandbox();
  vm.runInContext(
    `
    summary=()=>{};draw=()=>{};ledger=()=>{};
    const selectionCalls=[];
    select=(id,focus)=>selectionCalls.push({id,focus});
    state.signals=[{id:'selected',direction:'LONG',tier:'CONFIRMED',origin:'live'}];
    state.selected='selected';render();
    if(selectionCalls.length!==1||selectionCalls[0].focus!==false)throw Error('refresh moved viewport back to signal');
  `,
    c,
  );
});
