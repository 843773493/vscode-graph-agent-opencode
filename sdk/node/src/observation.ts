export interface RuntimeObservationApplyRequest {
  instance_id: string;
  revision: string;
  telemetry: { enabled: boolean; sample_ratio: number; sample_buffer_limit: number };
  statistics: { enabled: boolean; collection_interval_ms: number };
}
export interface RuntimeObservationStatus extends Omit<RuntimeObservationApplyRequest, "revision"> {
  applied_revision: string;
  requests: { count: number; errors: number; duration_ms: number; proxy_count: number; proxy_errors: number; proxy_duration_ms: number } | null;
  samples: { kind: string; status_code: number; duration_ms: number; failed: boolean }[];
  processes: { service: string; pid: number; rss_bytes: number; cpu_seconds: number; error: string | null }[];
  collection_error: string | null;
  unsupported_metrics: string[];
}
export interface RuntimeObservationResponse {
  statusCode: number;
  once(event: 'finish', listener: () => void): this;
}
export interface RuntimeObservation {
  apply(payload: unknown): RuntimeObservationStatus;
  status(): RuntimeObservationStatus;
  observeHttp(response: RuntimeObservationResponse): void;
  close(): void;
}
import { fromJson, toJson } from "@bufbuild/protobuf";
import { RuntimeObservationApplyRequestSchema } from "./generated/boxteam/gateway/v2/public_pb.js";

/** 仅记录固定 HTTP 指标及本进程资源；关闭时不保留缓冲、计数或定时任务。 */
export function createRuntimeObservation(instanceId: string, service: string, initial: RuntimeObservationApplyRequest): RuntimeObservation {
  let settings: RuntimeObservationApplyRequest;
  let samples: RuntimeObservationStatus["samples"] | null = null;
  let nextSample = 0;
  let requests: RuntimeObservationStatus["requests"] = null;
  let processes: RuntimeObservationStatus["processes"] | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;

  function snapshotSamples() {
    return samples === null ? [] : [...samples.slice(nextSample), ...samples.slice(0, nextSample)];
  }

  function collectResources() {
    const cpu = process.cpuUsage();
    processes = [{ service, pid: process.pid, rss_bytes: process.memoryUsage.rss(),
      cpu_seconds: (cpu.user + cpu.system) / 1_000_000, error: null }];
  }

  function apply(payload: unknown) {
    let desired;
    try {
      desired = toJson(RuntimeObservationApplyRequestSchema,
        fromJson(RuntimeObservationApplyRequestSchema, payload as import("@bufbuild/protobuf").JsonValue), { useProtoFieldName: true, alwaysEmitImplicit: true }) as unknown as RuntimeObservationApplyRequest;
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
    if (!statistics.enabled) { requests = null; processes = null; }
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
    observeHttp(response: RuntimeObservationResponse) {
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
      timer = null; samples = null; requests = null; processes = null;
    },
  });
}
