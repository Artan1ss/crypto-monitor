/* Shared by the 1H multi-panel chart and 4H/1D reversal chart. */
class CandleViewport {
  constructor(total = 0) {
    this.reset(total);
  }
  reset(total) {
    this.total = total;
    this.count = Math.min(100, total);
    this.left = Math.max(0, total - this.count);
  }
  clamp() {
    this.count = Math.max(
      Math.min(12, this.total),
      Math.min(this.total, this.count),
    );
    this.left = Math.max(0, Math.min(this.total - this.count, this.left));
  }
  pan(delta) {
    this.left += delta;
    this.clamp();
  }
  zoom(factor, anchor = 0.5) {
    const at = this.left + this.count * anchor;
    this.count *= factor;
    this.clamp();
    this.left = at - this.count * anchor;
    this.clamp();
  }
  focus(index) {
    this.count = Math.min(100, this.total);
    this.left = index - this.count * 0.65;
    this.clamp();
  }
  slice() {
    return [
      Math.floor(this.left),
      Math.min(this.total, Math.ceil(this.left + this.count)),
    ];
  }
}
