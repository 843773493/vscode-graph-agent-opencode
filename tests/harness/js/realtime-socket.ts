import { afterEach, beforeEach } from 'bun:test';

/** 测试中的 Gateway WS：将 subscribe 交给测试自己的 HTTP owner fake，产品没有 HTTP 回退。 */
export class RealtimeTestSocket {
  static readonly OPEN = 1;
  static sockets: RealtimeTestSocket[] = [];
  readyState = 1;
  binaryType = 'arraybuffer';
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string | ArrayBuffer }) => void) | null = null;
  readonly commands: Record<string, unknown>[] = [];
  private token = '';
  private readers = new Map<number, ReadableStreamDefaultReader<Uint8Array>>();
  private controllers = new Map<number, AbortController>();
  constructor(readonly url: string | URL) {
    RealtimeTestSocket.sockets.push(this);
    queueMicrotask(() => this.onopen?.());
  }
  control(frame: object): void { this.onmessage?.({ data: JSON.stringify(frame) }); }
  bytes(id: number, bytes: Uint8Array): void {
    const buffer = new ArrayBuffer(4 + bytes.length);
    new DataView(buffer).setUint32(0, id);
    new Uint8Array(buffer, 4).set(bytes);
    this.onmessage?.({ data: buffer });
  }
  send(raw: string): void {
    const command = JSON.parse(raw);
    this.commands.push(command);
    if (command.type === 'auth') { this.token = command.token; this.control({ type: 'ready' }); }
    if (command.type === 'subscribe') void this.open(command);
    if (command.type === 'unsubscribe') {
      this.controllers.get(command.subscription_id)?.abort();
      void this.readers.get(command.subscription_id)?.cancel();
    }
  }
  private async open(command: { subscription_id: number; path: string; method: string; headers: Record<string, string>; body?: string }): Promise<void> {
    const id = command.subscription_id;
    const controller = new AbortController();
    this.controllers.set(id, controller);
    try {
      const response = await fetch(new URL(command.path, String(this.url).replace(/^ws/, 'http')), {
        method: command.method, headers: { ...command.headers, 'X-Local-Token': this.token },
        body: command.body, signal: controller.signal,
      });
      const headers = Object.fromEntries(response.headers);
      this.control({ type: 'headers', subscription_id: id, status: response.status, headers });
      const reader = response.body?.getReader();
      if (reader) {
        this.readers.set(id, reader);
        while (!controller.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          if (value.length) this.bytes(id, value);
        }
        this.readers.delete(id);
        reader.releaseLock();
      }
      this.control({ type: 'end', subscription_id: id });
    } catch (error) {
      if (!controller.signal.aborted) this.control({ type: 'error', subscription_id: id, message: String(error) });
    } finally { this.readers.delete(id); this.controllers.delete(id); }
  }
  close(): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    for (const controller of this.controllers.values()) controller.abort();
    for (const reader of this.readers.values()) void reader.cancel();
    this.onclose?.({ code: 1000 });
  }
}

export function useRealtimeSocket(): void {
  const original = globalThis.WebSocket;
  beforeEach(() => {
    RealtimeTestSocket.sockets = [];
    globalThis.WebSocket = RealtimeTestSocket as unknown as typeof WebSocket;
  });
  afterEach(() => {
    for (const socket of RealtimeTestSocket.sockets) socket.close();
    globalThis.WebSocket = original;
  });
}
