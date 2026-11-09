import { fromJson, toJson } from "@bufbuild/protobuf";
import { RuntimeObservationApplyRequestSchema } from "../workspace-services/protocol/generated/boxteam/gateway/v2/public_pb.js";

/** 仅记录固定 HTTP 指标及本进程资源；关闭时不保留缓冲、计数或定时任务。 */
export function createRuntimeObservation(instanceId, service, initial) {
  let settings;
  let samples = null, nextSample = 0, requests = null, processes = null, timer = null;

  function snapshotSamples() {
    return samples === null ? [] : [...samples.slice(nextSample), ...samples.slice(0, nextSample)];
  }

  function collectResources() {
    const cpu = process.cpuUsage();
    processes = [{ service, pid: process.pid, rss_bytes: process.memoryUsage.rss(),
      cpu_seconds: (cpu.user + cpu.system) / 1_000_000, error: null }];
  }

  function apply(payload) {
    let desired;
    try {
      desired = toJson(RuntimeObservationApplyRequestSchema,
        fromJson(RuntimeObservationApplyRequestSchema, payload), { useProtoFieldName: true, alwaysEmitImplicit: true });
    } catch (cause) {
      throw Object.assign(new TypeError("诊断配置不符合生成的运行时观察合同", { cause }), { statusCode: 422 });
    }
    if (desired.instance_id !== instanceId) {
      throw Object.assign(new Error("诊断配置不属于原插件实例"), { statusCode: 409 });
    }
    const { telemetry, statistics } = desired;
    if (!desired.revision?.trim() || !telemetry || !statistics
        || !Number.isFinite(telemetry.sample_ratio) || telemetry.sample_ratio < 0 || telemetry.sample_ratio > 1
        || !Number.isInteger(telemetry.sample_buffer_limit) || telemetry.sample_buffer_limit < 1 || telemetry.sample_buffer_limit > 4096
        || !Number.isInteger(statistics.collection_interval_ms) || statistics.collection_interval_ms < 1000
        || statistics.collection_interval_ms > 60000) {
      throw Object.assign(new TypeError("诊断配置缺少 revision 或采样／缓冲／间隔超出合法范围"), { statusCode: 422 });
    }
    if (timer !== null && (!statistics.enabled
        || statistics.collection_interval_ms !== settings.statistics.collection_interval_ms)) {
      clearInterval(timer);
      timer = null;
    }
    if (!telemetry.enabled) samples = null;
    else if (samples === null || telemetry.sample_buffer_limit !== settings.telemetry.sample_buffer_limit) {
      samples = snapshotSamples().slice(-telemetry.sample_buffer_limit);
      nextSample = 0;
    }
    if (!statistics.enabled) requests = processes = null;
    else if (requests === null) requests = { count: 0, errors: 0, duration_ms: 0,
      proxy_count: 0, proxy_errors: 0, proxy_duration_ms: 0 };
    settings = desired;
    if (statistics.enabled && timer === null) {
      collectResources();
      timer = setInterval(collectResources, statistics.collection_interval_ms);
      timer.unref();
    }
    return status();
  }

  function status() {
    return { instance_id: instanceId, applied_revision: settings.revision,
      telemetry: { ...settings.telemetry }, statistics: { ...settings.statistics },
      requests: requests === null ? null : { ...requests }, samples: snapshotSamples(),
      processes: processes === null ? [] : processes.map(item => ({ ...item })),
      collection_error: null, unsupported_metrics: ["plugin_internal_stages"] };
  }

  apply(initial);
  return Object.freeze({
    apply, status,
    observeHttp(response) {
      if (requests === null && samples === null) return;
      const started = performance.now();
      response.once("finish", () => {
        const duration_ms = performance.now() - started, failed = response.statusCode >= 400;
        if (requests !== null) {
          requests.count += 1;
          requests.errors += Number(failed);
          requests.duration_ms += duration_ms;
        }
        if (samples !== null && settings.telemetry.sample_ratio > 0
            && (settings.telemetry.sample_ratio === 1 || Math.random() < settings.telemetry.sample_ratio)) {
          const sample = { kind: "http", status_code: response.statusCode, duration_ms, failed };
          if (samples.length < settings.telemetry.sample_buffer_limit) samples.push(sample);
          else {
            samples[nextSample] = sample;
            nextSample = (nextSample + 1) % samples.length;
          }
        }
      });
    },
    close() {
      if (timer !== null) clearInterval(timer);
      timer = samples = requests = processes = null;
    },
  });
}
