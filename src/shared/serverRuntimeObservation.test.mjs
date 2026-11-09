import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import { createRuntimeObservation } from "./serverRuntimeObservation.mjs";

const instanceId = "plugin-observation-test";

function config({
  revision = "test",
  telemetryEnabled = false,
  sampleRatio = 1,
  sampleBufferLimit = 256,
  statisticsEnabled = false,
  collectionIntervalMs = 5000,
  targetInstanceId = instanceId,
} = {}) {
  return {
    instance_id: targetInstanceId,
    revision,
    telemetry: {
      enabled: telemetryEnabled,
      sample_ratio: sampleRatio,
      sample_buffer_limit: sampleBufferLimit,
    },
    statistics: {
      enabled: statisticsEnabled,
      collection_interval_ms: collectionIntervalMs,
    },
  };
}

function completeRequest(observation, statusCode) {
  const response = new EventEmitter();
  response.statusCode = statusCode;
  observation.observeHttp(response);
  response.emit("finish");
  return response;
}

function captureIntervals(run) {
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const scheduled = [];
  const cleared = [];
  globalThis.setInterval = (callback, delay) => {
    const timer = { callback, delay, unref() {} };
    scheduled.push(timer);
    return timer;
  };
  globalThis.clearInterval = (timer) => cleared.push(timer);
  try {
    run({ scheduled, cleared });
  } finally {
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
  }
}

test("插件观察默认关闭且遥测与统计独立开关", () => {
  captureIntervals(({ scheduled }) => {
    const observation = createRuntimeObservation(instanceId, "terminal", config());
    assert.equal(scheduled.length, 0);
    assert.equal(observation.status().requests, null);
    assert.deepEqual(observation.status().samples, []);
    assert.deepEqual(observation.status().processes, []);
    completeRequest(observation, 200);
    assert.equal(observation.status().requests, null);
    assert.deepEqual(observation.status().samples, []);

    observation.apply(config({ revision: "telemetry-only", telemetryEnabled: true, sampleBufferLimit: 2 }));
    for (const status of [200, 201, 500]) {
      completeRequest(observation, status);
    }
    let state = observation.status();
    assert.deepEqual(state.samples.map((sample) => sample.status_code), [201, 500]);
    assert.deepEqual(Object.keys(state.samples[0]).sort(), ["duration_ms", "failed", "kind", "status_code"]);
    assert.equal(state.requests, null);
    assert.deepEqual(state.processes, []);
    assert.equal(scheduled.length, 0);

    // 关闭遥测后只计数，不保留样本；统计仅持有本 Node 插件进程。
    observation.apply(config({ revision: "statistics-only", statisticsEnabled: true }));
    assert.equal(scheduled.length, 1);
    assert.equal(scheduled[0].delay, 5000);
    state = observation.status();
    assert.equal(state.requests.count, 0);
    assert.deepEqual(state.samples, []);
    assert.equal(state.processes.length, 1);
    assert.equal(state.processes[0].service, "terminal");
    assert.equal(state.processes[0].pid, process.pid);
    completeRequest(observation, 503);
    state = observation.status();
    assert.deepEqual([state.requests.count, state.requests.errors], [1, 1]);
    assert.deepEqual(state.samples, []);
    assert.equal(state.applied_revision, "statistics-only");
    observation.close();
  });
});

test("零采样率不记样本，环形缓冲保留最近记录及顺序", () => {
  const observation = createRuntimeObservation(instanceId, "terminal", config({
    telemetryEnabled: true,
    sampleRatio: 0,
    sampleBufferLimit: 3,
  }));
  completeRequest(observation, 200);
  assert.deepEqual(observation.status().samples, []);

  observation.apply(config({ revision: "sampled", telemetryEnabled: true, sampleBufferLimit: 3 }));
  for (const status of [201, 202, 203, 204]) completeRequest(observation, status);
  assert.deepEqual(observation.status().samples.map((sample) => sample.status_code), [202, 203, 204]);
  observation.apply(config({ revision: "resized", telemetryEnabled: true, sampleBufferLimit: 2 }));
  assert.deepEqual(observation.status().samples.map((sample) => sample.status_code), [203, 204]);
  observation.close();
});

test("热切换关闭采集、拒绝无效设置且关闭释放 timer 和缓存", () => {
  captureIntervals(({ scheduled, cleared }) => {
    const observation = createRuntimeObservation(instanceId, "terminal", config({
      telemetryEnabled: true,
      statisticsEnabled: true,
      collectionIntervalMs: 1000,
    }));
    const initial = observation.status();
    assert.equal(scheduled.length, 1);
    assert.equal(scheduled[0].delay, 1000);
    completeRequest(observation, 200);

    for (const invalid of [
      config({ revision: "bad-ratio", telemetryEnabled: true, sampleRatio: 2 }),
      config({ revision: "bad-buffer", telemetryEnabled: true, sampleBufferLimit: 4097 }),
      config({ revision: "bad-interval", statisticsEnabled: true, collectionIntervalMs: 999 }),
      { ...config({ revision: "bad-type" }), telemetry: { ...config().telemetry, sample_ratio: "bad" } },
      config({ revision: "wrong-instance", targetInstanceId: "another-plugin" }),
    ]) {
      const beforeInvalidApply = observation.status();
      assert.throws(() => observation.apply(invalid), (error) => error.statusCode === (
        invalid.instance_id === "another-plugin" ? 409 : 422
      ));
      assert.deepEqual(observation.status(), beforeInvalidApply);
      assert.equal(scheduled.length, 1);
      assert.equal(cleared.length, 0);
    }
    assert.notDeepEqual(observation.status(), initial);

    observation.apply(config({ revision: "interval-updated", statisticsEnabled: true, collectionIntervalMs: 2000 }));
    assert.equal(cleared.length, 1);
    assert.equal(cleared[0], scheduled[0]);
    assert.equal(scheduled.length, 2);
    assert.equal(scheduled[1].delay, 2000);

    observation.apply(config({ revision: "off" }));
    assert.equal(cleared.length, 2);
    const off = observation.status();
    assert.equal(off.applied_revision, "off");
    assert.equal(off.requests, null);
    assert.deepEqual(off.samples, []);
    assert.deepEqual(off.processes, []);
    const response = completeRequest(observation, 500);
    assert.equal(response.listenerCount("finish"), 0);

    observation.close();
    assert.equal(cleared.length, 2);
    assert.equal(observation.status().requests, null);
    assert.deepEqual(observation.status().samples, []);
    assert.deepEqual(observation.status().processes, []);
  });
});
