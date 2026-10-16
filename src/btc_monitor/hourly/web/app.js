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
};
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
};
async function get(path, options) {
  const r = await fetch(path, options);
  if (!r.ok) throw Error(`请求失败 ${r.status}`);
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
  $("status").textContent = state.offline
    ? "离线复盘 · 不发通知"
    : h.enabled
      ? h.healthy
        ? "1H 监控运行中"
        : "1H 监控预热 / 数据检查"
      : "1H 监控未启用";
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
    `${state.offline ? "离线文件；无法证明云端当前健康。 " + (state.offlineData?.exported_at ? "快照时间 " + date(state.offlineData.exported_at) + "。 " : "") : ""}1H 收盘后评估；生成箭头≠触发收盘点。${h.source === "direct" ? "固定 7 源直连" : "Coinalyze 固定 6 源，未包含 OKX 现货"}；费率是未统一周期、非 OI 加权的报告均值。${repaired ? ` ${repaired} 个小时使用当时已有单位证据生成只读复盘修正版，历史决策未改。` : ""}${h.last_evaluation?.blocked ? " 当前阻断：" + h.last_evaluation.blocked : ""}${h.replay?.state === "running" ? " · 历史重建中" : ""}`;
  if (state.offlineData?.local_verification && state.offline)
    $("notice").textContent +=
      ` 本地新算法验证，尚未部署；${state.offlineData.verified_signal_ids?.length || 0} 条信号使用导出的原始分钟数据复算，其余仍为原导出结果。`;
  $("online").hidden = !state.offline;
  $("replay").disabled = state.offline || h.replay?.state === "running";
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
  const bars = state.bars;
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
  const t0 = Date.parse(bars[0].start),
    t1 = Date.parse(bars.at(-1).end) + 3600000;
  const x = (t) => left + ((Date.parse(t) - t0) / (t1 - t0)) * plot;
  let svg = `<svg viewBox="0 0 ${W} ${H}" aria-label="BTC 1H 价格及市场指标">`;
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
    cumulative[market] = cvdSeries(bars, market);
  const selected = state.signals.find((s) => s.id === state.selected);
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
    min = key === "volume" ? 0 : min - span * 0.06;
    max += span * 0.06;
    const y = (v) => top + height - ((v - min) / (max - min)) * height;
    for (let j = 0; j < 3; j++) {
      const val = min + ((max - min) * j) / 2;
      svg +=
        `<path d="M${left} ${y(val)}H${W - right + 3}" stroke="${colors.grid}"/>` +
        text(W - right + 10, y(val) + 4, fmt(val, key === "funding" ? 4 : 0));
    }
    if (key === "price") {
      bars.forEach((b) => {
        if (!b.price) return;
        const p = b.price,
          up = N(p.close) >= N(p.open),
          c = up ? colors.up : colors.down,
          xx = x(new Date(Date.parse(b.start) + 1800000).toISOString()),
          bw = Math.max(2, (plot / bars.length) * 0.55);
        svg += `<path d="M${xx} ${y(N(p.high))}V${y(N(p.low))}" stroke="${c}"/><rect x="${xx - bw / 2}" y="${Math.min(y(N(p.open)), y(N(p.close)))}" width="${bw}" height="${Math.max(1, Math.abs(y(N(p.open)) - y(N(p.close))))}" fill="${c}" opacity="${b.complete ? 1 : 0.35}"/>`;
      });
      if (selected) {
        for (const [v, title, c] of [
          [N(selected.entry), "参考价", colors.future],
          [N(selected.invalidation), "失效参考", colors.down],
        ])
          svg +=
            `<path d="M${left} ${y(v)}H${W - right}" stroke="${c}" stroke-dasharray="5 4"/>` +
            text(left + 5, y(v) - 4, `${title} ${fmt(v)}`, c);
      }
      for (const s of filtered()) {
        const xx = x(s.generated_at),
          b = bars.find((b) => b.start === s.bar_start);
        if (!b?.price) continue;
        const c = s.direction === "SHORT" ? colors.down : colors.up,
          yy =
            s.direction === "SHORT"
              ? y(N(b.price.high)) - 16
              : y(N(b.price.low)) + 16;
        svg += `<g data-signal="${esc(s.id)}" tabindex="0" role="button" aria-label="${esc(s.direction + " " + s.tier + " " + date(s.generated_at))}" cursor="pointer"><title>${esc(s.direction + " " + s.tier + " " + date(s.generated_at) + " · " + s.origin)}</title><rect x="${xx - 10}" y="${yy - 12}" width="20" height="24" fill="transparent"/><path d="${s.direction === "SHORT" ? `M${xx - 5} ${yy - 6}H${xx + 5}L${xx} ${yy + 3}Z` : `M${xx - 5} ${yy + 6}H${xx + 5}L${xx} ${yy - 3}Z`}" stroke="${c}" stroke-width="${s.id === state.selected ? 3 : 1.5}" fill="${s.tier === "CONFIRMED" ? c : "none"}"/></g><circle cx="${x(b.end)}" cy="${y(N(b.price.close))}" r="2" fill="${c}" pointer-events="none"/>`;
      }
    } else {
      const vals = series(key),
        c = key === "spot" ? colors.spot : colors.future;
      let path = "";
      vals.forEach((v, i) => {
        if (v === null) {
          if (["spot", "linear"].includes(key)) {
            const xx = x(bars[i].start),
              width = plot / bars.length;
            const label = "缺口 / 单位未确认";
            svg += `<g><title>${esc(date(bars[i].start) + " " + label)}</title><rect x="${xx}" y="${top}" width="${width}" height="${height}" fill="#e6ba6c" opacity=".09"/></g>`;
            if (i === 0 || vals[i - 1] !== null)
              svg += text(xx + 3, top + 12, "缺口", colors.spot, 10);
          }
          path += " ";
          return;
        }
        const xx = x(
          new Date(Date.parse(bars[i].start) + 1800000).toISOString(),
        );
        if (["volume", "funding"].includes(key)) {
          const w = Math.max(2, (plot / bars.length) * 0.6);
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
  $("chart")
    .querySelectorAll("[data-signal]")
    .forEach((el) => {
      el.addEventListener("click", () => select(el.dataset.signal));
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter") select(el.dataset.signal);
      });
    });
  const node = $("chart").querySelector("svg");
  node.addEventListener("mousemove", (e) => {
    const rect = node.getBoundingClientRect(),
      xx = ((e.clientX - rect.left) / rect.width) * W,
      t = t0 + ((xx - left) / plot) * (t1 - t0),
      i = Math.floor((t - t0) / 3600000),
      b = bars.find((v) => Date.parse(v.start) === t0 + i * 3600000);
    if (!b) return;
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
    node.querySelector("#crosshair").setAttribute("d", `M${xx} 12V765`);
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
    const parts = [
      `${date(s.generated_at)}<small>${s.origin === "live" ? "实时记录" : "历史重建 · 非实时通知"}</small>`,
      `<span class="${s.direction === "SHORT" ? "short" : "long"}">${s.direction} · ${s.tier === "WATCH" ? "观察" : "确认"}</span><small>${esc(s.kind)} · 证据分 ${fmt(s.confidence?.score ?? s.outcomes?.confidence?.score, 0)}</small>`,
      esc(s.reasons.join(" / ")),
      `$${fmt(s.entry)}<small>${esc(delivery[s.delivery_state] || s.delivery_state)}</small>`,
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
    p.textContent = "该范围没有满足条件的信号。数据不足时不会强行产生提醒。";
    node.append(p);
  }
}
async function select(id) {
  state.selected = id;
  let s = state.signals.find((v) => v.id === id);
  if (!s) return;
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
      p.textContent = `${component.label}：${component.points} / ${component.max}`;
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
  for (const hours of ["1", "4", "12", "24"]) {
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
  try {
    const h = $("range").value;
    const [bars, signals, health] = await Promise.all([
      get(`/hourly/bars?hours=${h}`),
      get(`/hourly/signals?hours=${h}&limit=500`),
      get("/hourly/health"),
    ]);
    if (state.offline) return;
    Object.assign(state, { bars, signals, health });
    render();
  } catch (e) {
    if (!state.offline) $("status").textContent = e.message;
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
  if (state.selected) select(state.selected);
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
$("refresh").addEventListener("click", refresh);
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
$("import").addEventListener("change", async (e) => {
  try {
    const f = e.target.files[0];
    if (!f) return;
    const v = await readReviewFile(f);
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
    });
    applyOfflineRange();
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
setInterval(refresh, 30000);
