export interface RuntimeObservationApplyRequest {
  instance_id: string;
  revision: string;
  telemetry: { enabled: boolean; sample_ratio: number; sample_buffer_limit: number };
  statistics: { enabled: boolean; collection_interval_ms: number };
}
export interface RuntimeObservationStatus extends RuntimeObservationApplyRequest {
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
  apply(payload: RuntimeObservationApplyRequest): RuntimeObservationStatus;
  status(): RuntimeObservationStatus;
  observeHttp(response: RuntimeObservationResponse): void;
  close(): void;
}

export function createRuntimeObservation(
  instanceId: string,
  service: string,
  initial: RuntimeObservationApplyRequest,
): RuntimeObservation;
