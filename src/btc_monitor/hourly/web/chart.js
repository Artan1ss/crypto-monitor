/* Navigation only. Never modifies bars, CVD, signal evidence, or delivery. */
class HourlyChartController {
  constructor(node, redraw, getSignals, getSelected, selectSignal) {
    this.node = node;
    this.redraw = redraw;
    this.signals = getSignals;
    this.selected = getSelected;
    this.selectSignal = selectSignal;
    this.view = new CandleViewport();
    this.bars = [];
    this.mode = null;
    this.drag = null;
    node.addEventListener(
      "wheel",
      (e) => {
        if (!this.bars.length) return;
        e.preventDefault();
        this.view.zoom(
          Math.exp(Math.max(-0.3, Math.min(0.3, e.deltaY * 0.002))),
          this.anchor(e.clientX),
        );
        this.redraw();
      },
      { passive: false },
    );
    node.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest("[data-signal]")) return;
      node.focus();
      this.drag = { x: e.clientX, left: this.view.left };
      node.setPointerCapture(e.pointerId);
    });
    node.addEventListener("pointermove", (e) => {
      if (!this.drag) return;
      const width = (node.getBoundingClientRect().width * 978) / 1100;
      this.view.left =
        this.drag.left - ((e.clientX - this.drag.x) / width) * this.view.count;
      this.view.clamp();
      this.redraw();
    });
    for (const event of ["pointerup", "pointercancel", "lostpointercapture"])
      node.addEventListener(event, () => {
        this.drag = null;
      });
    node.addEventListener("dblclick", (e) => {
      if (!e.target.closest("[data-signal]")) this.latest();
    });
    node.addEventListener("keydown", (e) => {
      if (e.target.closest("[data-signal]")) return;
      const actions = {
        ArrowLeft: () => this.view.pan(-this.view.count * 0.2),
        ArrowRight: () => this.view.pan(this.view.count * 0.2),
        "+": () => this.view.zoom(0.8),
        "=": () => this.view.zoom(0.8),
        "-": () => this.view.zoom(1.25),
        Home: () => {
          this.view.left = 0;
        },
        End: () => {
          this.view.left = this.view.total - this.view.count;
        },
      };
      if (actions[e.key]) {
        e.preventDefault();
        actions[e.key]();
        this.redraw();
      }
    });
    const button = (id, action) =>
      document.getElementById(id).addEventListener("click", action);
    button("chart-zoom-in", () => {
      this.view.zoom(0.8);
      this.redraw();
    });
    button("chart-zoom-out", () => {
      this.view.zoom(1.25);
      this.redraw();
    });
    button("chart-all", () => {
      this.view.left = 0;
      this.view.count = this.view.total;
      this.redraw();
    });
    button("chart-latest", () => this.latest());
    button("chart-focus", () =>
      this.focus(this.signals().find((s) => s.id === this.selected())),
    );
    button("chart-prev", () => this.navigate(-1));
    button("chart-next", () => this.navigate(1));
    button("chart-jump", () =>
      this.jump(document.getElementById("chart-date").value),
    );
    document.getElementById("chart-scroll").addEventListener("input", (e) => {
      this.view.left =
        (Number(e.target.value) / 1000) *
        Math.max(0, this.view.total - this.view.count);
      this.redraw();
    });
  }
  anchor(clientX) {
    const r = this.node.getBoundingClientRect();
    return Math.max(
      0,
      Math.min(1, (((clientX - r.left) / r.width) * 1100 - 12) / 978),
    );
  }
  sync(bars, mode) {
    const old = this.bars;
    if (mode !== this.mode || !old.length) this.view.reset(bars.length);
    else if (
      bars.length !== old.length ||
      bars[0]?.start !== old[0]?.start ||
      bars.at(-1)?.start !== old.at(-1)?.start
    ) {
      const atEnd = this.view.left + this.view.count >= this.view.total - 0.5;
      const first = old[Math.floor(this.view.left)]?.start;
      const fraction = this.view.left % 1;
      this.view.total = bars.length;
      if (atEnd) this.view.left = bars.length - this.view.count;
      else {
        const i = bars.findIndex(
          (b) => Date.parse(b.start) >= Date.parse(first),
        );
        this.view.left = i < 0 ? bars.length - this.view.count : i + fraction;
      }
      this.view.clamp();
    }
    this.bars = bars;
    this.mode = mode;
    this.index = new Map(bars.map((b, i) => [Date.parse(b.start), i]));
  }
  latest() {
    this.view.count = Math.min(100, this.view.total);
    this.view.left = Math.max(0, this.view.total - this.view.count);
    this.redraw();
  }
  focus(signal) {
    if (!signal) return;
    const i = this.index.get(Date.parse(signal.bar_start));
    if (i === undefined) return;
    this.view.focus(i);
    const anchor = signal.snapshot?.candidate?.sweep_diagnostics?.pivot_start;
    const a = this.index.get(Date.parse(anchor));
    if (a !== undefined) {
      this.view.count = Math.min(this.view.total, Math.max(48, i - a + 24));
      this.view.left = a - 6;
      this.view.clamp();
    }
    this.redraw();
  }
  focusIfOutside(signal) {
    const i = this.index?.get(Date.parse(signal.bar_start));
    if (
      i !== undefined &&
      (i < this.view.left || i >= this.view.left + this.view.count)
    )
      this.focus(signal);
  }
  navigate(direction) {
    const signals = [...this.signals()].sort(
      (a, b) => Date.parse(a.bar_start) - Date.parse(b.bar_start),
    );
    if (!signals.length) return;
    const current = signals.findIndex((s) => s.id === this.selected());
    const i =
      current < 0
        ? direction < 0
          ? signals.length - 1
          : 0
        : Math.max(0, Math.min(signals.length - 1, current + direction));
    this.focus(signals[i]);
    this.selectSignal(signals[i].id);
  }
  jump(value) {
    if (!value) return;
    // Date picker is explicitly UTC; chart labels remain America/Toronto.
    const t = Date.parse(value + "T00:00:00Z");
    const i = this.bars.findIndex((b) => Date.parse(b.start) >= t);
    if (
      i < 0 ||
      t < Date.parse((this.bars[0]?.start || "").slice(0, 10) + "T00:00:00Z")
    ) {
      document.getElementById("chart-range").textContent =
        "该日期不在已加载范围内；请先扩大数据范围或导入历史文件。";
      return;
    }
    this.view.focus(i);
    this.redraw();
  }
  status(visible) {
    const [start, end] = this.view.slice();
    const date = (t) =>
      new Intl.DateTimeFormat("zh-CN", {
        timeZone: "America/Toronto",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(t));
    document.getElementById("chart-range").textContent = this.bars.length
      ? `${date(this.bars[start].start)} — ${date(this.bars[end - 1].start)} · 第 ${start + 1}–${end} / ${this.bars.length} 根已加载K线 · ${visible}个可见信号 · 多伦多时间；时间缺口以黄虚线标识（不补K线）`
      : "尚无已加载数据";
    document.getElementById("chart-scroll").value = String(
      (this.view.left / Math.max(1, this.view.total - this.view.count)) * 1000,
    );
    if (this.bars.length) {
      document.getElementById("chart-date").min = this.bars[0].start.slice(
        0,
        10,
      );
      document.getElementById("chart-date").max = this.bars
        .at(-1)
        .start.slice(0, 10);
    }
  }
}
