"use strict";
const $ = (id) => document.getElementById(id),
  N = (v) => (v === null || v === undefined ? null : Number(v));
const fmt = (v, d = 2) =>
  N(v) === null
    ? "缺失"
    : N(v).toLocaleString("en-US", {
        maximumFractionDigits: d,
        minimumFractionDigits: d,
      });
const date = (v) =>
  new Intl.DateTimeFormat("zh-CN", {
    timeZone: "America/Toronto",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(v));
const receipt = (v) =>
  `${date(v)}:${String(new Date(v).getUTCSeconds()).padStart(2, "0")}`;
const esc = (v) =>
  String(v).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  bars: [],
  signals: [],
  health: {},
  selected: null,
  offline: false,
  offlineData: null,
  loading: false,
  dataError: null,
  market: null,
};
let refreshGeneration = 0;
const colors = {
  text: "#92a5ba",
  grid: "#243243",
  up: "#46c6ac",
  down: "#ee7b88",
  spot: "#e6ba6c",
  future: "#7ea8ed",
};
const verdict = {
  pending: "追踪中",
  direction_supported: "方向支持",
  direction_opposed: "方向相反",
  insufficient_data: "数据不足",
  flat: "横盘",
};
const touchLabels = {
  target_first: "已证实：1R 先于失效",
  invalidation_first: "已证实：失效先于1R",
  target_only: "已证实先到1R；目前只观察到目标触及",
  invalidation_only: "已证实先到失效；目前只观察到失效触及",
  ambiguous_same_minute: "同一分钟同时触及，分钟内次序不明",
  neither: "完整分钟路径内均未触及",
  unresolved_missing_prefix: "首次已知触及之前存在缺口，首次次序未确定",
  unresolved_partial_start_minute: "起始分钟内路径不确定",
  unknown_due_to_gaps: "旧版结果：路径有缺口，需重新计算",
};
const delivery = {
  sent: "已发 TG",
  retrospective: "未发送·历史重建",
  disabled: "通知关闭",
  suppressed_cooldown: "冷却期留存",
  missing_fresh_entry: "缺少实时参考价",
  missing_fresh_delivery: "投递前缺少新鲜报价",
  stale_delivery: "通知已过期·未发送",
  invalidated_before_delivery: "通知前已失效",
  uncertain: "投递结果不确定",
  attempted: "已尝试·结果待核实",
  pending: "待发送",
  superseded_strategy: "旧策略已替换·未发送",
};
const chartNav = new HourlyChartController(
  $("chart"),
  draw,
  filtered,
  () => state.selected,
  select,
);
async function get(path, options) {
  const r = await fetch(path, options);
  if (!r.ok) {
    let message = `请求失败 ${r.status}`;
    try {
      const payload = await r.json();
      if (typeof payload.detail === "string") message = payload.detail;
    } catch {}
    const error = Error(message);
    error.status = r.status;
    throw error;
  }
  return r.json();
}
function filtered() {
  return state.signals.filter(
    (s) =>
      ($("direction").value === "ALL" ||
        s.direction === $("direction").value) &&
      ($("tier").value === "ALL" || s.tier === $("tier").value) &&
      ($("origin").value === "ALL" || s.origin === $("origin").value),
  );
}
function summary() {
  const h = state.health;
  const live = !state.offline && state.market;
  $("status").textContent = state.offline
    ? "离线复盘 · 不发通知"
    : h.enabled
      ? h.healthy
        ? "1H 监控运行中"
        : "1H 监控预热 / 数据检查"
      : h.database?.scope === "local_preview"
        ? "本地数据库只读 · 非云端实时连接"
        : "1H 监控未启用";
  if (state.dataError && !state.offline)
    $("status").textContent = state.dataError;
  if (live) {
    const m = live.health;
    $("status").textContent = m.price_fresh
      ? `实时价格已连接 · ${m.provider_fresh && m.metrics_complete ? "聚合指标已更新" : "聚合指标缺口／预热／过期"} · 不发TG`
      : "价格数据已过期／连接中断，非当前实时行情";
    if (state.dataError)
      $("status").textContent =
        "实时读取失败；图中为上次数据：" + state.dataError;
  }
  $("live-price").textContent = live
    ? `${receipt(live.received_at)} · $${fmt(state.bars.at(-1)?.price?.close)}`
    : "非独立实时行情";
  $("source").textContent =
    (h.source || state.bars[0]?.source || "未知") === "coinalyze"
      ? "Coinalyze · 3 现货 / 3 合约"
      : "交易所直连 · 4 现货 / 3 合约";
  $("coverage").textContent =
    `${state.bars.filter((b) => b.complete).length} / ${state.bars.length}`;
  const signals = filtered();
  $("counts").textContent =
    `${signals.length} / ${signals.filter((s) => s.delivery_state === "sent").length}`;
  const evaluable = signals.filter(
    (s) =>
      s.origin === "live" &&
      ["direction_supported", "direction_opposed", "flat"].includes(
        s.outcomes?.windows?.["24"]?.verdict,
      ),
  );
  $("performance").textContent = evaluable.length
    ? `${evaluable.filter((s) => s.outcomes.windows["24"].verdict === "direction_supported").length} / ${evaluable.length} 实时样本`
    : "实时样本尚不足";
  const repaired = state.bars.filter((b) => b.review_recovery).length;
  $("notice").textContent =
    `${state.offline ? "离线文件；无法证明云端当前健康。 " + (state.offlineData?.exported_at ? "快照时间 " + date(state.offlineData.exported_at) + "。 " : "") : ""}1H 收盘后评估；箭头标在触发K线，生成时间见详情。${h.source === "direct" ? "固定 7 源直连" : "Coinalyze 固定 6 源，未包含 OKX 现货"}；费率是未统一周期、非 OI 加权的报告均值。${repaired ? ` ${repaired} 个小时使用当时已有单位证据生成只读复盘修正版，历史决策未改。` : ""}${h.last_evaluation?.blocked ? " 当前阻断：" + h.last_evaluation.blocked : ""}${h.replay?.state === "running" ? " · 历史重建中" : ""}`;
  if (live) {
    const m = live.health;
    $("notice").textContent =
      `本机直连：Bybit价格每30秒更新，Coinalyze固定6源已收盘1H指标每120秒刷新。末根半透明K线未收盘，不用于信号。行情不依赖数据库，信号另读本机档案；不会自动同步Railway或发送TG。现货CVD仅在单位核验后显示，费率是未统一周期的非OI加权均值。`;
    $("notice").textContent +=
      ` 价格接收：${date(live.received_at)}；指标接收：${m.provider_received_at ? date(m.provider_received_at) : "尚未完成"}；完整指标截至：${m.latest_complete_metrics_end ? date(m.latest_complete_metrics_end) : "尚无完整六源小时"}（多伦多时间）。`;
    if (!m.provider_fresh || m.provider_error)
      $("notice").textContent +=
        ` 聚合指标未就绪／过期：${m.provider_error || "正在读取并核验单位"}，旧值不能当作当前数据。`;
    if (m.provider_fresh && !m.metrics_complete) {
      const unknown = (m.selected_feeds || [])
        .filter((f) => f.volume_unit !== "base")
        .map((f) => f.feed);
      $("notice").textContent +=
        " 聚合指标尚未完整；单位待核验：" +
        (unknown.join("、") || "请检查最近收盘小时的数据覆盖") +
        "。";
    }
    if (m.storage_error)
      $("notice").textContent +=
        " 本地原始数据留存失败，但不影响实时行情显示。";
    if (live.signal_warning)
      $("notice").textContent += " " + live.signal_warning;
  } else if (!state.offline && h.database?.scope === "local_preview")
    $("notice").textContent =
      "本机数据库，不会自动同步 Railway；打开界面不启动采集或TG。 " +
      $("notice").textContent;
  const lastBar = state.bars.at(-1);
  if (lastBar && !live) {
    $("notice").textContent += ` 数据截至 ${date(lastBar.end)}（多伦多时间）。`;
    if (Date.now() - Date.parse(lastBar.end) > 2 * 3600000)
      $("notice").textContent += " 数据已过期，仅供历史复盘，非当前实时行情。";
  }
  if (state.offlineData?.local_verification && state.offline)
    $("notice").textContent +=
      ` 本地新算法验证，尚未部署；${state.offlineData.verified_signal_ids?.length || 0} 条信号使用导出的原始分钟数据复算，其余仍为原导出结果。`;
  if (state.offlineData?.sweep_research && state.offline)
    $("notice").textContent =
      "扫点背离历史验证 · 不发TG · 事后补取的固定六源数据，不证明当时已可用。箭头标在触发K线，详情列出理论评估时刻；方向分与先触及失效分别复盘，非可执行收益。";
  $("online").hidden = !state.offline;
  $("replay").disabled =
    Boolean(live) ||
    state.offline ||
    Boolean(state.dataError) ||
    h.database?.connected === false ||
    h.replay?.state === "running";
}
function cvdSeries(bars, market) {
  let sum = 0,
    prior = null;
  return bars.map((b) => {
    const flow = b.aggregated[market];
    if (!(flow.complete ?? b.complete) || N(flow.delta_btc) === null) {
      sum = 0;
      prior = null;
      return null;
    }
    if (prior !== b.start) sum = 0;
    sum += N(flow.delta_btc);
    prior = b.end;
    return sum;
  });
}
function draw() {
  const allBars = state.bars;
  chartNav.sync(
    allBars,
    state.offline
      ? `offline:${state.offlineData?.exported_at}`
      : state.market
        ? "live"
        : "archive",
  );
  const [visibleStart, visibleEnd] = chartNav.view.slice();
  const bars = allBars.slice(visibleStart, visibleEnd);
  const visibleSignals = filtered().filter((s) =>
    bars.some((b) => Date.parse(b.start) === Date.parse(s.bar_start)),
  );
  chartNav.status(visibleSignals.length);
  if (!bars.length) {
    $("chart").innerHTML =
      '<div class="chart-empty">尚无 1H 特征记录。<br>启用监控后自动生成，或点击“重建历史（不发 TG）”。<br>不会用模拟数据填充真实界面。</div>';
    return;
  }
  const W = 1100,
    left = 12,
    right = 110,
    plot = W - left - right,
    H = 790;
  const indices = new Map(allBars.map((b, i) => [Date.parse(b.start), i]));
  const x = (t) => {
    const time = Date.parse(t),
      floor = Math.floor(time / 3600000) * 3600000;
    let i = indices.get(floor);
    if (i === undefined) {
      i = allBars.findIndex((b) => Date.parse(b.start) >= floor);
      if (i < 0) i = allBars.length;
    }
    return (
      left +
      ((i + (time - floor) / 3600000 - chartNav.view.left) /
        chartNav.view.count) *
        plot
    );
  };
  let svg = `<svg viewBox="0 0 ${W} ${H}" aria-label="BTC 1H 价格及市场指标"><defs><clipPath id="hourly-plot-clip"><rect x="${left}" y="0" width="${plot}" height="${H}"/></clipPath></defs>`;
  const text = (a, b, s, c = colors.text, size = 11) =>
    `<text x="${a}" y="${b}" fill="${c}" font-size="${size}" font-family="system-ui">${esc(s)}</text>`;
  const panels = [
    ["price", "价格 · Bybit BTCUSDT / USDT", 25, 245],
    ["volume", "合约成交量 · BTC", 283, 70],
    ["spot", "现货 CVD · BTC · 连续段归零", 391, 78],
    ["linear", "合约 CVD · BTC · 连续段归零", 507, 78],
    ["oi", "同源 OI · BTC · 计数口径未核验", 623, 55],
    ["funding", "报告费率均值 · % · 非 OI 加权", 716, 45],
  ];
  const cumulative = { spot: [], linear: [] };
  for (const market of ["spot", "linear"])
    cumulative[market] = cvdSeries(allBars, market).slice(
      visibleStart,
      visibleEnd,
    );
  const selected = visibleSignals.find((s) => s.id === state.selected);
  const series = (key) =>
    bars.map((b, i) =>
      key === "volume"
        ? N(b.aggregated.linear.volume_btc)
        : key === "oi"
          ? N(b.oi_btc)
          : key === "funding"
            ? N(b.funding_fraction_mean) === null
              ? null
              : N(b.funding_fraction_mean) * 100
            : cumulative[key]?.[i],
    );
  for (const [key, label, top, height] of panels) {
    let values =
      key === "price"
        ? bars.flatMap((b) =>
            b.price ? [N(b.price.low), N(b.price.high)] : [],
          )
        : series(key).filter((v) => v !== null);
    if (key === "price" && selected)
      values.push(N(selected.entry), N(selected.invalidation));
    svg += text(left, top - 8, label);
    if (!values.length) {
      svg += text(left, top + height / 2, "缺少完整数据，未补零");
      continue;
    }
    let min = Math.min(...values),
      max = Math.max(...values);
    if (["volume", "funding"].includes(key)) {
      min = Math.min(min, 0);
      max = Math.max(max, 0);
    }
    const span = max - min || Math.max(Math.abs(max) * 0.01, 1);
    min = key === "volume" ? 0 : min - span * (key === "price" ? 0.15 : 0.06);
    max += span * (key === "price" ? 0.15 : 0.06);
    const y = (v) => top + height - ((v - min) / (max - min)) * height;
    for (let j = 0; j < 3; j++) {
      const val = min + ((max - min) * j) / 2;
      svg +=
        `<path d="M${left} ${y(val)}H${W - right + 3}" stroke="${colors.grid}"/>` +
        text(W - right + 10, y(val) + 4, fmt(val, key === "funding" ? 4 : 0));
    }
    if (key === "price") {
      svg += '<g clip-path="url(#hourly-plot-clip)">';
      bars.forEach((b) => {
        if (!b.price) return;
        const p = b.price,
          up = N(p.close) >= N(p.open),
          c = up ? colors.up : colors.down,
          xx = x(new Date(Date.parse(b.start) + 1800000).toISOString()),
          bw = Math.max(0.5, Math.min(18, (plot / chartNav.view.count) * 0.55));
        svg += `<path d="M${xx} ${y(N(p.high))}V${y(N(p.low))}" stroke="${c}"/><rect x="${xx - bw / 2}" y="${Math.min(y(N(p.open)), y(N(p.close)))}" width="${bw}" height="${Math.max(1, Math.abs(y(N(p.open)) - y(N(p.close))))}" fill="${c}" opacity="${b.closed === undefined ? (b.complete ? 1 : 0.35) : b.closed ? 1 : 0.45}"/>`;
      });
      if (selected) {
        for (const [v, title, c, offset] of [
          [N(selected.entry), "参考价", colors.future, 0],
          [N(selected.invalidation), "失效参考", colors.down, 220],
        ])
          svg +=
            `<path d="M${left} ${y(v)}H${W - right}" stroke="${c}" stroke-dasharray="5 4"/>` +
            text(left + 5 + offset, top + 14, `${title} ${fmt(v)}`, c);
        const ref = selected.snapshot?.candidate?.sweep_diagnostics;
        if (ref && N(ref.level) !== null) {
          const from = Math.max(left, x(ref.pivot_start)),
            to = x(
              new Date(Date.parse(selected.bar_start) + 1800000).toISOString(),
            );
          svg +=
            `<path data-sweep-level="true" d="M${from} ${y(N(ref.level))}H${to}" stroke="${colors.spot}" stroke-dasharray="3 3"/>` +
            text(
              left + 445,
              top + 14,
              `被扫结构 ${fmt(ref.level)} · 锚点 ${date(ref.pivot_start)}`,
              colors.spot,
            );
          if (
            bars.some(
              (b) => Date.parse(b.start) === Date.parse(ref.pivot_start),
            )
          )
            svg += `<circle data-sweep-anchor="true" cx="${x(new Date(Date.parse(ref.pivot_start) + 1800000).toISOString())}" cy="${y(N(ref.level))}" r="4" fill="${colors.spot}"/>`;
        }
      }
      const stacks = new Map();
      for (const s of visibleSignals) {
        const xx = x(new Date(Date.parse(s.bar_start) + 1800000).toISOString()),
          b = bars.find((b) => Date.parse(b.start) === Date.parse(s.bar_start));
        if (!b?.price) continue;
        const stackKey = s.bar_start + ":" + s.direction,
          stack = stacks.get(stackKey) || 0;
        stacks.set(stackKey, stack + 1);
        const c = s.direction === "SHORT" ? colors.down : colors.up,
          yy =
            s.direction === "SHORT"
              ? y(N(b.price.high)) - 20 - stack * 22
              : y(N(b.price.low)) + 20 + stack * 22;
        svg += `<g data-signal="${esc(s.id)}" tabindex="0" role="button" aria-label="${esc(s.direction + " " + s.tier + " " + date(s.generated_at))}" cursor="pointer"><title>${esc(s.direction + " · 触发K线 " + date(s.bar_start) + " · 生成 " + date(s.generated_at) + " · " + s.origin)}</title><rect x="${xx - 12}" y="${yy - 12}" width="24" height="28" rx="4" fill="${s.id === state.selected ? "#26384b" : "transparent"}"/><path d="${s.direction === "SHORT" ? `M ${xx} ${yy + 8} l -7 -9 h 4 v -7 h 6 v 7 h 4 Z` : `M ${xx} ${yy - 8} l -7 9 h 4 v 7 h 6 v -7 h 4 Z`}" stroke="${c}" stroke-width="${s.id === state.selected ? 2 : 1}" fill="${s.tier === "CONFIRMED" ? c : "none"}"/></g>`;
      }
      svg += "</g>";
    } else {
      svg += '<g clip-path="url(#hourly-plot-clip)">';
      const vals = series(key),
        c = key === "spot" ? colors.spot : colors.future;
      let path = "";
      vals.forEach((v, i) => {
        if (v === null) {
          if (["spot", "linear"].includes(key)) {
            const xx = x(bars[i].start),
              width = plot / chartNav.view.count;
            const label =
              bars[i].closed === false
                ? "当前小时尚未收盘"
                : "缺口 / 单位未确认";
            svg += `<g><title>${esc(date(bars[i].start) + " " + label)}</title><rect x="${xx}" y="${top}" width="${width}" height="${height}" fill="#e6ba6c" opacity=".09"/></g>`;
            if (i === 0 || vals[i - 1] !== null)
              svg += text(
                xx + 3,
                top + 12,
                bars[i].closed === false ? "未收盘" : "缺口",
                colors.spot,
                10,
              );
          }
          path += " ";
          return;
        }
        const xx = x(
          new Date(Date.parse(bars[i].start) + 1800000).toISOString(),
        );
        if (["volume", "funding"].includes(key)) {
          const w = Math.max(
            0.5,
            Math.min(18, (plot / chartNav.view.count) * 0.6),
          );
          svg += `<rect x="${xx - w / 2}" y="${Math.min(y(v), y(0))}" width="${w}" height="${Math.max(1, Math.abs(y(v) - y(0)))}" fill="${v >= 0 ? colors.up : colors.down}" opacity=".7"/>`;
        } else {
          const contiguous =
            i > 0 && vals[i - 1] !== null && bars[i - 1].end === bars[i].start;
          if (i > 0 && !contiguous)
            svg += text(xx + 3, top + 12, "重新累计", colors.spot, 10);
          path += `${contiguous ? "L" : "M"}${xx} ${y(v)} `;
        }
      });
      svg += `<path d="${path}" stroke="${c}" stroke-width="1.7" fill="none"/>`;
      svg += "</g>";
    }
  }
  for (let i = 0; i < bars.length; i += Math.max(1, Math.ceil(bars.length / 8)))
    svg += text(
      x(bars[i].start),
      785,
      date(bars[i].start).replace(" ", " / "),
      colors.text,
      10,
    );
  svg +=
    '<path id="crosshair" stroke="#92a5ba" stroke-dasharray="3 4" opacity=".45"/></svg>';
  $("chart").innerHTML = svg;
  // Real gaps remain explicit; navigation compresses only the missing time,
  // never manufactures a candle or joins a CVD path over it.
  for (let i = 1; i < bars.length; i++) {
    if (Date.parse(bars[i - 1].end) !== Date.parse(bars[i].start)) {
      const gap = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "path",
      );
      gap.setAttribute("d", `M${x(bars[i].start)} 15V765`);
      gap.setAttribute("stroke", colors.spot);
      gap.setAttribute("stroke-dasharray", "2 5");
      gap.setAttribute("pointer-events", "none");
      $("chart").querySelector("svg").append(gap);
    }
  }
  $("chart")
    .querySelectorAll("[data-signal]")
    .forEach((el) => {
      el.addEventListener("click", () => select(el.dataset.signal));
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          select(el.dataset.signal);
        }
      });
    });
  const node = $("chart").querySelector("svg");
  node.addEventListener("mousemove", (e) => {
    if (chartNav.drag) return;
    const rect = node.getBoundingClientRect(),
      xx = ((e.clientX - rect.left) / rect.width) * W,
      i = Math.floor(
        chartNav.view.left + ((xx - left) / plot) * chartNav.view.count,
      ),
      b = allBars[i];
    if (!b || xx < left || xx > W - right) return;
    const p = b.price;
    $("hover").textContent =
      `${date(b.start)} ${p ? "O " + fmt(p.open) + " H " + fmt(p.high) + " L " + fmt(p.low) + " C " + fmt(p.close) : "价格缺失"} | 现货 Δ ${fmt(b.aggregated.spot.delta_btc)} BTC | 合约 Δ ${fmt(b.aggregated.linear.delta_btc)} BTC | ${b.review_recovery ? "只读修正版·历史决策未改" : b.complete ? "完整" : "缺口"}${Object.entries(
        b.flows || {},
      )
        .filter(([, v]) => !v.complete)
        .map(
          ([k, v]) =>
            ` · ${k} ${v.observed_bars}/${v.expected_bars} ${v.quality_reasons?.join(",") || "单位未确认/数据不完整"}`,
        )
        .join("")}`;
    node
      .querySelector("#crosshair")
      .setAttribute(
        "d",
        `M${xx} 12V765 M${left} ${((e.clientY - rect.top) / rect.height) * H}H${W - right}`,
      );
  });
}
function outcomeReturn(w) {
  if (!w || w.status !== "complete") return "尚未到期";
  if (
    (w.endpoint_exact === undefined &&
      w.coverage !== "complete_minute_interior") ||
    w.endpoint_exact === false ||
    w.verdict === "insufficient_data"
  )
    return "数据不足，未判定";
  return `方向变动 ${fmt(w.signed_return_pct)}%`;
}
function ledger() {
  const node = $("signals");
  node.replaceChildren();
  for (const s of filtered()) {
    const row = document.createElement("button");
    row.className = "signal-row" + (s.id === state.selected ? " selected" : "");
    const research24 = s.research_review?.["24"];
    const researchOutcome = research24
      ? `收盘参考情景<small>24H方向分 ${fmt(research24.direction_score, 1)}${research24.complete ? "" : " · 未到期/缺口"}</small><small>结构分 ${fmt(research24.structure_score, 0)}（非交易收益）</small>`
      : null;
    const parts = [
      `${date(s.generated_at)}<small>${s.origin === "live" ? "实时记录" : "历史重建 · 非实时通知"}</small>`,
      `<span class="${s.direction === "SHORT" ? "short" : "long"}">${s.direction} · ${s.tier === "WATCH" ? "观察" : "确认"}</span><small>${esc(s.kind)} · 证据分 ${fmt(s.confidence?.score ?? s.outcomes?.confidence?.score, 0)}</small>`,
      esc(s.reasons.join(" / ")),
      `$${fmt(s.entry)}<small>${esc(delivery[s.delivery_state] || s.delivery_state)}</small>`,
      researchOutcome ||
        `${esc(verdict[s.outcomes?.windows?.["24"]?.verdict] || "追踪中")}<small>24H ${outcomeReturn(s.outcomes?.windows?.["24"])}</small><small>结构分 ${s.outcomes?.windows?.["24"]?.scores?.structure_score == null ? "未确定" : fmt(s.outcomes.windows["24"].scores.structure_score, 0)}</small>`,
    ];
    for (const p of parts) {
      const span = document.createElement("span");
      span.innerHTML = p;
      row.append(span);
    }
    row.addEventListener("click", () => select(s.id));
    node.append(row);
  }
  if (!filtered().length) {
    const p = document.createElement("p");
    p.className = "signal-empty";
    p.textContent =
      state.market && !state.offline
        ? "实时模式仅显示行情，未启动本地信号计算。本机档案没有该范围信号；云端信号不会自动同步到这里。"
        : "该范围没有满足条件的信号。数据不足时不会强行产生提醒。";
    node.append(p);
  }
}
async function select(id, focus = true) {
  state.selected = id;
  let s = state.signals.find((v) => v.id === id);
  if (!s) return;
  if (focus) chartNav.focusIfOutside(s);
  if (!s.snapshot && !state.offline) {
    try {
      s = await get("/hourly/signals/" + id);
      Object.assign(
        state.signals.find((v) => v.id === id),
        s,
      );
    } catch (e) {
      $("status").textContent = e.message;
      return;
    }
  }
  const d = s.snapshot?.diagnostics,
    c = s.snapshot?.candidate,
    node = $("detail");
  node.replaceChildren();
  const title = document.createElement("h2");
  title.textContent = `${s.direction} · ${s.tier === "WATCH" ? "背离观察" : "价格确认"}`;
  node.append(title);
  function row(k, v) {
    const el = document.createElement("div");
    el.className = "detail-row";
    const a = document.createElement("span"),
      b = document.createElement("b");
    a.textContent = k;
    b.textContent = v;
    el.append(a, b);
    node.append(el);
  }
  row("生成时间", date(s.generated_at));
  row("触发小时", date(s.bar_start));
  row("记录来源", s.origin === "live" ? "实时" : "历史重建（非通知）");
  row("投递状态", delivery[s.delivery_state] || s.delivery_state);
  row("参考价格", "$" + fmt(s.entry));
  row("结构失效", "$" + fmt(s.invalidation));
  const confidence =
    s.confidence || s.snapshot?.confidence || s.outcomes?.confidence;
  if (confidence) {
    row("发出时证据置信分（非胜率）", `${fmt(confidence.score, 0)} / 100`);
    row(
      "评分来源",
      (s.confidence_origin || s.outcomes?.confidence_origin) === "at_generation"
        ? "发出时保存"
        : "按原始快照事后补算，未使用后续价格",
    );
    const parts = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "查看置信分拆解 · " + confidence.version;
    parts.append(summary);
    for (const component of confidence.components || []) {
      const p = document.createElement("p");
      p.textContent = `${component.label || component.key}：${component.points} / ${component.max}`;
      parts.append(p);
    }
    node.append(parts);
  } else row("发出时证据置信分", "旧文件未含评分，请导出新版复盘文件");
  const review24 = s.outcomes?.windows?.["24"];
  if (review24?.scores) {
    row(
      "24H复盘分（方向 / 结构）",
      `${fmt(review24.scores.direction_score, 1)} / ${review24.scores.structure_score == null ? "未确定" : fmt(review24.scores.structure_score, 0)}${review24.scores.provisional ? " · 进行中" : ""}`,
    );
    row(
      "24H完整分钟",
      `${review24.observed_minutes} / ${review24.expected_minutes}`,
    );
    row(
      "24H结构结果",
      touchLabels[review24.touch_order] || review24.touch_order,
    );
  }
  if (d) {
    const flowWindow = `${s.snapshot.parameters.flow_hours}H`;
    row(`现货 ${flowWindow} ΔCVD`, fmt(d.spot_delta_btc) + " BTC");
    row("现货 normalizer", fmt(d.spot_normalizer_btc) + " BTC");
    row("现货主动压力", fmt(N(d.spot_pressure) * 100) + "%");
    row(`合约 ${flowWindow} ΔCVD`, fmt(d.futures_delta_btc) + " BTC");
    row("合约主动压力", fmt(N(d.futures_pressure) * 100) + "%");
    row("价格变化 / ATR", fmt(d.price_change_atr));
    row("OI 1H", fmt(d.oi_change_pct) + "%");
    row(
      "报告费率均值",
      N(d.funding_fraction_mean) === null
        ? "缺失"
        : fmt(N(d.funding_fraction_mean) * 100, 4) + "%",
    );
    row("合约量比", fmt(d.volume_ratio) + "x");
    row("ATR 14H", fmt(d.atr));
  }
  if (c?.sweep_diagnostics) {
    const r = c.sweep_diagnostics;
    row("扫点锚点小时", date(r.pivot_start));
    row("锚点已确认", date(r.pivot_confirmed_at));
    row("被扫结构", "$" + fmt(r.level));
    row("锚点CVD比较长度", r.compared_hours + "H（同一价格锚点收盘）");
    for (const [market, label] of [
      ["spot", "现货"],
      ["linear", "合约"],
    ]) {
      row(
        label + " 锚点→触发 ΔCVD",
        fmt(r.flows[market].pivot_to_now_delta_btc) + " BTC",
      );
      row(
        label + " 锚点 normalizer",
        fmt(r.flows[market].normalizer_btc) + " BTC",
      );
    }
  }
  if (s.research_review) {
    row("复盘口径", "事后补取数据；收盘参考价情景，非16:03实际成交或TG");
    for (const [h, r] of Object.entries(s.research_review)) {
      row(
        h + "H 方向分 / 结构分",
        `${fmt(r.direction_score, 1)} / ${fmt(r.structure_score, 0)}`,
      );
      row(
        h + "H 触及顺序",
        {
          target_first: "先1R",
          invalidation_first: "先触及失效",
          same_hour_ambiguous: "同小时先后不明",
          neither: "均未触及",
          pending_or_gap: "未到期或缺口",
          unknown_due_to_gaps: "缺口前后顺序不明",
        }[r.touch_order] || r.touch_order,
      );
    }
  }
  const ul = document.createElement("ul");
  for (const reason of s.reasons) {
    const li = document.createElement("li");
    li.textContent = reason;
    ul.append(li);
  }
  node.append(ul);
  const heading = document.createElement("h3");
  heading.textContent = "后续价格路径 · 非实际交易收益";
  node.append(heading);
  for (const hours of s.research_review ? [] : ["1", "4", "12", "24"]) {
    const w = s.outcomes?.windows?.[hours],
      box = document.createElement("div");
    box.className = "outcome";
    box.textContent = `${hours}H · ${verdict[w?.verdict] || "追踪中"} · ${outcomeReturn(w)}`;
    node.append(box);
    if (w) {
      row(
        "覆盖",
        `${w.observed_minutes}/${w.expected_minutes} 分钟 · ${w.coverage}`,
      );
      row(
        "完整分钟覆盖分",
        w.data_quality_score == null
          ? "旧版未计算"
          : `${fmt(w.data_quality_score)} / 100`,
      );
      row(
        "终点方向分 / 结构结果分",
        w.scores
          ? `${w.scores.direction_score == null ? "未确定" : fmt(w.scores.direction_score, 1)} / ${w.scores.structure_score == null ? "未确定" : fmt(w.scores.structure_score, 0)}（各满分100${w.scores.provisional ? "，进行中" : ""}）`
          : "旧版未计算",
      );
      if (w.scores && hours === "1") {
        const explanation = document.createElement("p");
        explanation.className = "muted";
        explanation.textContent = w.scores.notice;
        node.append(explanation);
      }
      if (w.endpoint_exact !== undefined)
        row(
          "终点价格时间",
          w.endpoint_exact
            ? `${date(w.endpoint_at)} · 截至该完整分钟收盘${w.endpoint_boundary_seconds ? `（距窗口边界 ${fmt(w.endpoint_boundary_seconds, 1)} 秒）` : ""}`
            : "缺少窗口终点分钟，方向分暂不计算",
        );
      row(
        "有利/不利极值下界",
        `${fmt(w.mfe_usdt_lower_bound)} / ${fmt(w.mae_usdt_lower_bound)}`,
      );
      row("1R / 失效首次次序", touchLabels[w.touch_order] || w.touch_order);
      if (w.observed_touch_order && !w.touch_order_proven)
        row(
          "仅按已有分钟观察（非完整证明）",
          (
            touchLabels[w.observed_touch_order] || w.observed_touch_order
          ).replace("已证实", "已观察"),
        );
      for (const [name, hit] of Object.entries(w.first_touches || {}))
        row(
          `已知 ${name === "invalidation" ? "失效" : name} 触及`,
          `${date(hit.from)}–${date(hit.to)}`,
        );
      if (w.partial_start_minute)
        row(
          "起始分钟",
          w.start_boundary_clear
            ? "整分钟高低价证明该分钟未触及1R/失效"
            : "只使用生成后完整分钟，起始分钟先后关系未确定",
        );
      if (w.missing_minutes?.length) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = `缺失/不合格分钟 ${w.missing_minutes.length} 个（多伦多时间）`;
        details.append(summary);
        for (const missing of w.missing_minutes) {
          const line = document.createElement("div");
          line.textContent = `${date(missing.start)} · ${missing.reason}`;
          details.append(line);
        }
        node.append(details);
      }
      if (w.price_repair_ids?.length)
        row(
          "历史价格补采",
          `${w.price_repair_ids.length} 分钟 · 仅修复复盘，非当时实时数据`,
        );
    }
  }
  if (s.outcomes?.repair)
    row(
      "最近价格补采",
      `${s.outcomes.repair.state} · ${date(s.outcomes.repair.attempted_at)}${s.outcomes.repair.http_status ? ` · HTTP ${s.outcomes.repair.http_status}` : ""}`,
    );
  const disclaimer = document.createElement("p");
  disclaimer.className = "muted";
  disclaimer.textContent =
    c?.warnings?.join("；") ||
    "完整分钟之外的路径未知；不把分钟内目标/止损同时触及算作确定获利。";
  node.append(disclaimer);
  const download = document.createElement("button");
  download.textContent = state.offline
    ? "下载已导入的信号快照"
    : "下载完整快照 + 原始输入";
  download.addEventListener("click", async () => {
    try {
      const data = state.offline
        ? s
        : await get(`/hourly/signals/${id}/export`);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(
        new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
      );
      a.download = `h1-${id}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) {
      $("status").textContent = e.message;
    }
  });
  node.append(download);
  if (s.snapshot) {
    const details = document.createElement("details"),
      summary = document.createElement("summary"),
      pre = document.createElement("pre");
    summary.textContent = "完整阈值、诊断与输入血缘";
    pre.textContent = JSON.stringify(s.snapshot, null, 2);
    details.append(summary, pre);
    node.append(details);
  }
  draw();
  ledger();
}
async function refresh() {
  if (state.offline) return;
  const request = ++refreshGeneration;
  state.loading = true;
  try {
    const h = $("range").value;
    if ($("datasource").value === "live") {
      try {
        const market = await get(`/hourly/market?hours=${h}`, {
          signal: AbortSignal.timeout(25000),
        });
        if (state.offline || request !== refreshGeneration) return;
        Object.assign(state, { market, bars: market.bars, dataError: null });
        render();
        // Signals/DB health must never gate drawing a valid public market response.
        const results = await Promise.allSettled([
          get(`/hourly/signals?hours=${h}&limit=500`, {
            signal: AbortSignal.timeout(4000),
          }),
          get("/hourly/health", { signal: AbortSignal.timeout(4000) }),
        ]);
        if (state.offline || request !== refreshGeneration) return;
        if (results[0].status === "fulfilled") state.signals = results[0].value;
        else
          market.signal_warning =
            "信号档案暂不可用；实时行情仍正常。保留的旧信号不代表当前正在发出新信号。";
        if (results[1].status === "fulfilled") state.health = results[1].value;
        render();
        return;
      } catch (error) {
        if (error.status !== 404) throw error;
        if (request !== refreshGeneration) return;
        $("datasource").value = "archive";
      }
    }
    const [bars, signals, health] = await Promise.all([
      get(`/hourly/bars?hours=${h}`, { signal: AbortSignal.timeout(20000) }),
      get(`/hourly/signals?hours=${h}&limit=500`, {
        signal: AbortSignal.timeout(20000),
      }),
      get("/hourly/health", { signal: AbortSignal.timeout(5000) }),
    ]);
    if (state.offline || request !== refreshGeneration) return;
    Object.assign(state, {
      bars,
      signals,
      health,
      dataError: null,
      market: null,
    });
    render();
  } catch (e) {
    if (!state.offline && request === refreshGeneration) {
      state.dataError = e.message;
      $("status").textContent = e.message;
      $("notice").textContent =
        "读取失败；页面中若有旧数据，仅为上次成功加载的结果，非实时更新。可以读取本地快照，或启动 Docker 后重试。";
      $("replay").disabled = true;
      if (state.market) state.market.health.price_fresh = false;
    }
  } finally {
    if (request === refreshGeneration) state.loading = false;
  }
}
function render() {
  if (state.selected && !filtered().some((s) => s.id === state.selected)) {
    state.selected = null;
    $("detail").innerHTML =
      '<h2>信号证据</h2><p class="muted">点击图表标记或下方信号，查看完整诊断、关键位和后续结果。</p>';
  }
  summary();
  draw();
  ledger();
  if (state.selected) select(state.selected, false);
}
function applyOfflineRange() {
  const data = state.offlineData;
  if (!data) return;
  const end = Math.max(...data.bars.map((b) => Date.parse(b.end)));
  const start = end - N($("range").value) * 3600000;
  state.bars = data.bars.filter((b) => Date.parse(b.start) >= start);
  state.signals = data.signals.filter(
    (s) => Date.parse(s.bar_start) >= start && Date.parse(s.bar_start) < end,
  );
  render();
}
for (const id of ["direction", "tier", "origin"])
  $(id).addEventListener("change", render);
$("range").addEventListener("change", () =>
  state.offline ? applyOfflineRange() : refresh(),
);
$("datasource").addEventListener("change", () => {
  state.offline = false;
  state.market = null;
  state.selected = null;
  state.signals = [];
  refresh();
});
$("refresh").addEventListener("click", () => refresh());
$("replay").addEventListener("click", async () => {
  try {
    const hours = Math.max(30, N($("range").value));
    await get(`/hourly/replay?hours=${hours}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    $("status").textContent = "历史重建已排队，不会发送 TG";
    await refresh();
  } catch (e) {
    $("status").textContent = e.message;
  }
});
async function readReviewFile(file) {
  const limit = 50 * 1024 * 1024;
  if (file.size > limit) throw Error("文件超过 50 MiB");
  let stream = file.stream();
  if (file.name.toLowerCase().endsWith(".gz")) {
    if (typeof DecompressionStream === "undefined")
      throw Error("此浏览器不支持 gzip，请导入 JSON 文件");
    stream = stream.pipeThrough(new DecompressionStream("gzip"));
  }
  const reader = stream.getReader(),
    chunks = [];
  let bytes = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    bytes += value.byteLength;
    if (bytes > limit) {
      await reader.cancel();
      throw Error("解压后超过 50 MiB");
    }
    chunks.push(value);
  }
  return JSON.parse(await new Blob(chunks).text());
}
function applyOffline(v) {
  if (
    v.schema !== "btc-hourly-review-v1" ||
    !Array.isArray(v.bars) ||
    !Array.isArray(v.signals) ||
    v.bars.length > 168 ||
    v.signals.length > 500
  )
    throw Error("不是有效的 BTC 1H 复盘文件");
  Object.assign(state, {
    offlineData: v,
    health: v.health || {},
    offline: true,
    selected: null,
    dataError: null,
    market: null,
  });
  chartNav.mode = null;
  refreshGeneration++;
  state.loading = false;
  applyOfflineRange();
  if (v.sweep_research)
    $("notice").textContent =
      "扫点背离历史验证 · 不发TG · 事后补取的固定六源数据，不证明当时已可用。箭头标在触发K线，详情列出理论评估时刻；方向分与先触及失效分别复盘，非可执行收益。";
}
$("sweep-research").addEventListener("click", async () => {
  try {
    $("sweep-research").disabled = true;
    const v = await get("/hourly/sweep-research");
    $("range").value = "168";
    $("origin").value = "ALL";
    $("direction").value = "ALL";
    $("tier").value = "ALL";
    applyOffline(v);
  } catch (e) {
    $("status").textContent = e.message;
  } finally {
    $("sweep-research").disabled = false;
  }
});
$("local-snapshot").addEventListener("click", async () => {
  try {
    const response = await fetch("/hourly/local-snapshot");
    if (!response.ok) throw Error("无法读取本地快照，请导入已有离线复盘文件");
    const blob = await response.blob();
    const v = await readReviewFile({
      name: "snapshot.json.gz",
      size: blob.size,
      stream: () => blob.stream(),
    });
    applyOffline(v);
    // A snapshot contains both live and reconstructed records; start with all records visible.
    $("origin").value = "ALL";
    render();
  } catch (error) {
    $("status").textContent = error.message;
  }
});
$("import").addEventListener("change", async (e) => {
  try {
    const f = e.target.files[0];
    if (!f) return;
    const v = await readReviewFile(f);
    applyOffline(v);
  } catch (error) {
    $("status").textContent = error.message;
  }
});
$("online").addEventListener("click", () => {
  state.offline = false;
  state.selected = null;
  refresh();
});
refresh();
setInterval(() => {
  if (!state.loading) refresh();
}, 30000);
if (typeof document.addEventListener === "function") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !state.offline) refresh();
  });
}
