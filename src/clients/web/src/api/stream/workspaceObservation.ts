import type { WorkspaceObservationSubscription } from "../../types/backend";
import {
  consumeSseResponse,
  decodeJsonSseData,
  defineSseEvent,
  parseSseFrameBlock,
} from "../../sse/sseClient";
import type { ConsumeSseResponseOptions } from "../../sse/sseClient";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import { validateWorkspaceObservationFrame } from "../../sse/sseRuntimeSchemas";
import { HttpRequestError, requestGatewayResponse } from "../http";

const STREAM_PATH = "/api/v1/workspace/observations/stream";
type Subscription = Omit<WorkspaceObservationSubscription, "subscription_id">;
type Options = ConsumeSseResponseOptions & {
  onConnected?: (routeRevision: string | null, cursor: string | undefined) => void;
};
interface Subscriber {
  id: string;
  headers: Record<string, string>;
  subscription: () => Subscription;
  options: Options;
  resolve: () => void;
  reject: (error: unknown) => void;
  timer: ReturnType<typeof setTimeout> | undefined;
  abort: () => void;
}

const transports = new Map<string, WorkspaceObservationTransport>();
let nextSubscriptionId = 0;

class WorkspaceObservationTransport {
  private readonly subscribers = new Map<string, Subscriber>();
  private readonly targetHeaders = new Headers();
  private controller: AbortController | null = null;
  private running: Promise<void> | null = null;
  private connectionDone: Promise<void> | null = null;
  private revision = 0;

  constructor(private readonly port: number, private readonly key: string) {}

  subscribe(
    headers: Record<string, string>,
    subscription: () => Subscription,
    options: Options,
  ): Promise<void> {
    if (options.signal?.aborted) return Promise.resolve();
    // 同一个 Gateway target 的显式后端身份必须一致，不能因共享连接丢失核验。
    const incoming = new Headers(headers);
    for (const [name, value] of this.targetHeaders) {
      const other = incoming.get(name);
      if (other !== null && other !== value) {
        return Promise.reject(new Error(`Workspace 观察者目标身份冲突: ${name}`));
      }
    }
    for (const [name, value] of incoming) this.targetHeaders.set(name, value);
    return new Promise<void>((resolve, reject) => {
      const subscriber: Subscriber = {
        id: `observation-${++nextSubscriptionId}`,
        headers, subscription, options, resolve, reject, timer: undefined,
        abort: () => this.settle(subscriber, undefined, true),
      };
      this.subscribers.set(subscriber.id, subscriber);
      options.signal?.addEventListener("abort", subscriber.abort, { once: true });
      this.reconfigure();
    });
  }

  private settle(subscriber: Subscriber, error?: unknown, reconfigure = false): void {
    if (!this.subscribers.delete(subscriber.id)) return;
    clearTimeout(subscriber.timer);
    subscriber.options.signal?.removeEventListener("abort", subscriber.abort);
    const releaseConnection = reconfigure || this.subscribers.size === 0;
    const completed = releaseConnection ? this.connectionDone : null;
    if (releaseConnection) this.reconfigure();
    const finish = () => {
      if (error === undefined) subscriber.resolve();
      else subscriber.reject(error);
    };
    if (completed) void completed.then(finish);
    else finish();
  }

  private activity(subscriber: Subscriber): void {
    clearTimeout(subscriber.timer);
    const timeoutMs = subscriber.options.idleTimeoutMs ?? SSE_IDLE_TIMEOUT_MS;
    if (timeoutMs <= 0) throw new Error(`SSE 空闲超时必须大于 0: ${timeoutMs}`);
    subscriber.timer = setTimeout(() => this.settle(
      subscriber,
      subscriber.options.idleTimeoutError?.(timeoutMs)
        ?? new Error(`SSE 超过 ${timeoutMs}ms 未收到任何数据`),
      true,
    ), timeoutMs);
  }

  private reconfigure(): void {
    this.revision += 1;
    this.controller?.abort();
    if (this.running) return;
    // 同一轮登记的逻辑观察者合并发送；重建串行等待旧 reader 完成取消。
    this.running = Promise.resolve().then(() => this.run()).finally(() => {
      this.running = null;
      if (this.subscribers.size) this.reconfigure();
      else if (transports.get(this.key) === this) transports.delete(this.key);
    });
  }

  private async run(): Promise<void> {
    while (this.subscribers.size) {
      const revision = this.revision;
      const subscribers = [...this.subscribers.values()];
      const controller = new AbortController();
      this.controller = controller;
      let release!: () => void;
      const completed = new Promise<void>((resolve) => { release = resolve; });
      this.connectionDone = completed;
      try {
        const response = await requestGatewayResponse(this.port, STREAM_PATH, {
          method: "POST",
          signal: controller.signal,
          skipGatewayUserSession: true,
          headers: { ...Object.fromEntries(this.targetHeaders), accept: "text/event-stream", "Content-Type": "application/json" },
          body: JSON.stringify({ subscriptions: subscribers.map((subscriber) => ({
            ...subscriber.subscription(), subscription_id: subscriber.id,
          })) }),
        });
        if (controller.signal.aborted) {
          await response.body?.cancel();
          continue;
        }
        const routeRevision = response.headers.get("X-BoxTeam-Route-Revision");
        const ids = new Set(subscribers.map((subscriber) => subscriber.id));
        const connected = new Set<string>();
        for (const subscriber of subscribers) this.activity(subscriber);
        await consumeSseResponse(response, {
          signal: controller.signal,
          idleTimeoutMs: SSE_IDLE_TIMEOUT_MS,
          events: { observation: defineSseEvent(
            (data, frame) => validateWorkspaceObservationFrame(decodeJsonSseData(data, frame)),
            (envelope) => {
              if (revision !== this.revision) return;
              if (!ids.has(envelope.subscription_id)) {
                throw new Error(`Workspace SSE 返回未知观察者: ${envelope.subscription_id}`);
              }
              const subscriber = this.subscribers.get(envelope.subscription_id);
              if (!subscriber) return;
              try {
                if (envelope.state === "error") {
                  throw new HttpRequestError(envelope.status!, "Observation failed",
                    JSON.parse(envelope.detail_json!), STREAM_PATH);
                }
                if (envelope.state === "closed") {
                  this.settle(subscriber);
                  return;
                }
                this.activity(subscriber);
                if (envelope.state === "connected") {
                  connected.add(subscriber.id);
                  subscriber.options.onConnected?.(routeRevision, envelope.cursor);
                  return;
                }
                if (!connected.has(subscriber.id)) {
                  throw new Error(`Workspace 观察者尚未连接就收到事件: ${subscriber.id}`);
                }
                subscriber.options.onActivity?.();
                const frame = parseSseFrameBlock(envelope.frame!);
                if (!frame) return;
                const events = subscriber.options.events;
                const definition = Object.prototype.hasOwnProperty.call(events, frame.event)
                  ? events[frame.event] : events["*"];
                if (!definition) throw new Error(`未注册的 SSE 事件类型: ${frame.event}`);
                definition.handle(definition.decode(frame.data, frame), frame);
              } catch (error) {
                this.settle(subscriber, error, envelope.state !== "error");
              }
            },
          ) },
        });
        if (!controller.signal.aborted) {
          for (const subscriber of subscribers) this.settle(subscriber);
        }
      } catch (error) {
        if (revision === this.revision && !controller.signal.aborted) {
          for (const subscriber of subscribers) this.settle(subscriber, error);
        }
      } finally {
        if (this.controller === controller) this.controller = null;
        if (this.connectionDone === completed) this.connectionDone = null;
        release();
      }
    }
  }
}

export function consumeWorkspaceObservation(
  port: number,
  headers: Record<string, string>,
  subscription: () => Subscription,
  options: Options,
): Promise<void> {
  if (options.signal?.aborted) return Promise.resolve();
  const workspaceId = new Headers(headers).get("X-BoxTeam-Workspace-Id");
  const key = JSON.stringify([port, workspaceId]);
  let transport = transports.get(key);
  if (!transport) {
    transport = new WorkspaceObservationTransport(port, key);
    transports.set(key, transport);
  }
  return transport.subscribe(headers, subscription, options);
}
