// Pure JS unit tests, not browser automation. Run: node --test tests/test_frontend.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { gzipSync } from "node:zlib";

const source = readFileSync(
  new URL("../src/btc_monitor/hourly/web/app.js", import.meta.url),
  "utf8",
);
function sandbox() {
  const elements = new Map();
  const context = vm.createContext({
    Blob,
    DecompressionStream,
    Uint8Array,
    document: {
      getElementById(id) {
        if (!elements.has(id))
          elements.set(id, {
            value: id === "range" ? "24" : "ALL",
            addEventListener() {},
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
