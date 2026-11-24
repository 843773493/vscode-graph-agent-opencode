import { SseProtocolError } from "./sse.js";

export class RealtimeDisconnectedError extends TypeError {}
export class RealtimeTransportError extends Error {
  constructor(status, message, requestId, context = {}) {
    super(message, { cause: context.cause });
    this.status = status;
    this.requestId = requestId;
    this.detail = context.detail;
    this.subscriptionId = context.subscriptionId;
    this.path = context.path;
  }
}
const channels = new Map();
let nextId = 0;
/** 每个入口 Gateway 共用一个连接；业务模块不直接持有 WebSocket。 */
class RealtimeChannel {
  url;
  credential;
  socket;
  pending = new Map();
  ready;
  resolveReady;
  rejectReady;
  closed = false;
  constructor(url, credential) {
    this.url = url;
    this.credential = credential;
    this.ready = new Promise((resolve, reject) => {
      this.resolveReady = resolve;
      this.rejectReady = reject;
    });
    // 失败只交付给已登记的订阅，避免无消费者的 ready Promise 产生额外未处理异常。
    void this.ready.catch(() => {});
    void this.connect().catch((error) => this.fail(error));
  }
  async connect() {
    const token = await this.credential;
    if (this.closed) return;
    const socket = (this.socket = new WebSocket(this.url));
    socket.binaryType = "arraybuffer";
    socket.onopen = () => this.send({ type: "auth", token, headers: {} });
    socket.onmessage = (event) => {
      try {
        this.receive(event.data);
      } catch (error) {
        this.fail(error);
      }
    };
    socket.onerror = () =>
      this.fail(new RealtimeDisconnectedError("Gateway 实时连接失败"));
    socket.onclose = (event) =>
      this.fail(
        new RealtimeDisconnectedError(`Gateway 实时连接已断开 (${event.code})`),
      );
  }
  send(frame) {
    if (this.socket?.readyState === WebSocket.OPEN)
      this.socket.send(JSON.stringify(frame));
  }
  receive(raw) {
    if (raw instanceof ArrayBuffer) {
      if (raw.byteLength <= 4)
        throw new SseProtocolError("实时数据帧缺少订阅 ID 或正文");
      const identity = new DataView(raw).getUint32(0);
      const pending = this.pending.get(identity);
      // unsubscribe 与已在网络中的数据交错时，原订阅已经取消，不交付迟到数据。
      if (!pending) return;
      const bytes = new Uint8Array(raw, 4);
      pending.unacknowledged += bytes.byteLength;
      pending.controller.enqueue(bytes);
      pending.pullDone?.();
      pending.pullDone = undefined;
      return;
    }
    if (typeof raw !== "string")
      throw new SseProtocolError("实时控制帧必须为 JSON");
    const frame = JSON.parse(raw);
    if (!frame || typeof frame.type !== "string")
      throw new SseProtocolError("实时控制帧缺少类型");
    if (frame.type === "ready") {
      this.resolveReady();
      return;
    }
    if (frame.subscription_id === undefined) {
      if (frame.type === "error")
        throw new RealtimeTransportError(
          frame.status ?? 500,
          frame.message ?? "实时连接失败",
          frame.request_id ?? null,
          { detail: frame.detail },
        );
      throw new SseProtocolError("实时控制帧缺少订阅 ID");
    }
    if (
      !Number.isInteger(frame.subscription_id) ||
      frame.subscription_id < 1 ||
      !["headers", "end", "error"].includes(frame.type)
    )
      throw new SseProtocolError("实时控制帧类型或订阅 ID 无效");
    const pending = this.pending.get(frame.subscription_id);
    if (!pending) return;
    if (frame.type === "headers") {
      if (!Number.isInteger(frame.status) || !frame.headers)
        throw new SseProtocolError("实时订阅响应头无效");
      clearTimeout(pending.timer);
      const body = pending.body;
      if (!body) throw new SseProtocolError("实时订阅重复返回响应头");
      pending.body = undefined;
      pending.resolve(
        new Response(body, { status: frame.status, headers: frame.headers }),
      );
    } else if (frame.type === "end") {
      pending.controller.close();
      this.release(frame.subscription_id);
    } else if (frame.type === "error") {
      const error = new RealtimeTransportError(
        frame.status ?? 500,
        frame.message ?? "实时订阅失败",
        frame.request_id ?? null,
        { detail: frame.detail, subscriptionId: frame.subscription_id, path: pending.path },
      );
      pending.reject(error);
      pending.controller.error(error);
      this.release(frame.subscription_id);
    } else throw new SseProtocolError(`未知实时控制帧: ${frame.type}`);
  }
  open(request, signal, headerTimeoutMs) {
    const identity = ++nextId;
    return new Promise((resolve, reject) => {
      const cancel = (readerCancelled = false) => {
        const pending = this.pending.get(identity);
        if (!pending) return;
        pending.reject(
          signal?.reason ?? new DOMException("实时订阅已取消", "AbortError"),
        );
        if (!readerCancelled) pending.controller.close();
        this.send({
          type: "unsubscribe",
          subscription_id: identity,
          headers: {},
        });
        this.release(identity);
      };
      let controller;
      const body = new ReadableStream(
        {
          start(value) {
            controller = value;
          },
          pull: () => {
            const pending = this.pending.get(identity);
            if (!pending) return;
            // 高水位为 0，只有原 reader 消费完已缓冲字节才确认，后端据此反压单个订阅。
            if (pending.unacknowledged) {
              this.send({
                type: "ack",
                subscription_id: identity,
                consumed_bytes: pending.unacknowledged,
                headers: {},
              });
              pending.unacknowledged = 0;
            }
            return new Promise((done) => {
              pending.pullDone = done;
            });
          },
          cancel: () => cancel(true),
        },
        { highWaterMark: 0 },
      );
      const abort = () => cancel();
      const pending = {
        path: request.path,
        resolve,
        reject,
        controller,
        body,
        cancel: () => signal?.removeEventListener("abort", abort),
        unacknowledged: 0,
        timer: setTimeout(() => {
          const error = new Error(
            `请求超时: ${request.path}；等待实时响应头超过 ${headerTimeoutMs}ms`,
          );
          reject(error);
          controller.error(error);
          this.send({
            type: "unsubscribe",
            subscription_id: identity,
            headers: {},
          });
          this.release(identity);
        }, headerTimeoutMs),
      };
      this.pending.set(identity, pending);
      signal?.addEventListener("abort", abort, { once: true });
      void this.ready.then(
        () => {
          if (this.pending.has(identity))
            this.send({
              type: "subscribe",
              subscription_id: identity,
              ...request,
              headers: request.headers ?? {},
              method: request.method ?? "GET",
            });
        },
        (error) => {
          reject(error);
        },
      );
    });
  }
  release(identity) {
    const pending = this.pending.get(identity);
    if (!pending) return;
    clearTimeout(pending.timer);
    pending.cancel();
    pending.pullDone?.();
    pending.body = undefined;
    this.pending.delete(identity);
    if (!this.pending.size) this.close();
  }
  close() {
    this.closed = true;
    if (channels.get(this.url) === this) channels.delete(this.url);
    this.socket?.close();
  }
  fail(error) {
    if (this.closed) return;
    this.rejectReady(error);
    for (const [identity, pending] of this.pending) {
      // 共享连接失败沿各自发起路径交付，不能改写同一个异常的订阅归属。
      const failure = error instanceof RealtimeTransportError
        ? new RealtimeTransportError(error.status, error.message, error.requestId,
          { detail: error.detail, path: pending.path, subscriptionId: identity, cause: error })
        : error;
      pending.reject(failure);
      pending.controller.error(failure);
      this.release(identity);
    }
    this.close();
  }
}

/** 页面内按入口 URL 共享连接，具体 owner 保留自己的路由与游标。 */
export function openRealtimeResponse(
  url,
  credential,
  request,
  signal,
  headerTimeoutMs = 90000,
) {
  if (signal?.aborted) return Promise.reject(signal.reason);
  let channel = channels.get(url);
  if (!channel) {
    channel = new RealtimeChannel(url, Promise.resolve(credential));
    channels.set(url, channel);
  }
  return channel.open(request, signal, headerTimeoutMs);
}

export function invalidateRealtimeConnection(url) {
  channels
    .get(url)
    ?.fail(
      new RealtimeDisconnectedError("Gateway 凭据已改变，重新认证实时连接"),
    );
}
