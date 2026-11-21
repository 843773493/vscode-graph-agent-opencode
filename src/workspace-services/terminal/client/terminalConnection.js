import { encodeTerminalClientMessage } from "./protocol/messages.js";

/** 只管理终端字节连接；资源归属、授权取得和页面展示由调用方持有。 */
export class TerminalConnection {
  constructor({ terminalId, requestGrant, socketUrl, isVisible, canReconnect, getSize,
    onMessage, onMode, onStatus, onRevoked }) {
    Object.assign(this, { terminalId, requestGrant, socketUrl, isVisible, canReconnect, getSize,
      onMessage, onMode, onStatus, onRevoked });
    this.socket = null;
    this.attached = false;
    this.desiredAttached = false;
    this.grantLoading = false;
    this.reconnectTimer = null;
    this.reconnectAttempts = 0;
    this.lastSentCols = null;
    this.lastSentRows = null;
    // 页面内断线沿本游标续读；重载页面由owner完整快照重建屏幕和游标。
    this.lastSequence = 0;
  }

  rememberSequence(sequence) {
    if (!Number.isInteger(sequence) || sequence < this.lastSequence) return;
    this.lastSequence = sequence;
  }

  acknowledge(sequence) {
    this.rememberSequence(sequence);
    if (this.socket?.readyState === WebSocket.OPEN) this.send({ type: "ack", sequence });
  }

  send(message) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) throw new Error("WebSocket 尚未连接");
    this.socket.send(encodeTerminalClientMessage(message));
  }

  resize(cols, rows) {
    if (!this.attached || this.socket?.readyState !== WebSocket.OPEN
      || (cols === this.lastSentCols && rows === this.lastSentRows)) return;
    this.send({ type: "resize", cols, rows });
    this.lastSentCols = cols;
    this.lastSentRows = rows;
  }

  disconnect() {
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    const socket = this.socket;
    this.socket = null;
    this.attached = false;
    this.lastSentCols = null;
    this.lastSentRows = null;
    this.onMode("detached");
    socket?.close();
  }

  detach({ preserveIntent = false } = {}) {
    if (!preserveIntent) this.desiredAttached = false;
    if (this.socket?.readyState === WebSocket.OPEN) this.send({ type: "detach", terminalId: this.terminalId });
    this.disconnect();
    this.onStatus("已 detach，终端仍在后台运行");
  }

  async attach() {
    if (!this.isVisible()) return;
    if (!this.terminalId) { this.onStatus("URL 缺少 terminalId 参数", true); return; }
    if (this.attached) return;
    if (this.socket && this.socket.readyState !== WebSocket.CLOSED) {
      this.onStatus("正在连接终端...");
      return;
    }
    this.desiredAttached = true;
    if (this.grantLoading) return;
    this.grantLoading = true;
    let grant;
    try { grant = await this.requestGrant(); }
    finally { this.grantLoading = false; }
    if (!grant) {
      this.desiredAttached = false;
      this.onMode("detached");
      return;
    }
    if (!this.desiredAttached || !this.isVisible()) return;
    const currentSocket = new WebSocket(this.socketUrl(grant.service));
    this.socket = currentSocket;
    this.onMode("attaching");
    this.onStatus("正在连接终端...");

    currentSocket.addEventListener("open", () => {
      if (this.socket !== currentSocket || !this.isVisible()) { currentSocket.close(); return; }
      const { cols, rows } = this.getSize();
      this.send({ type: "attach", terminalId: this.terminalId, grantToken: grant.grant_token,
        cols, rows, afterSequence: this.lastSequence });
      this.lastSentCols = cols;
      this.lastSentRows = rows;
    });
    currentSocket.addEventListener("message", event => {
      if (this.socket !== currentSocket || !this.isVisible()) return;
      const message = JSON.parse(event.data);
      if (message.type === "attached") {
        this.attached = true;
        this.reconnectAttempts = 0;
        this.onMode("attached");
      } else if (message.type === "detached") {
        this.attached = false;
        this.onMode("detached");
      } else if (message.type === "exit") this.desiredAttached = false;
      this.onMessage(message);
      if (message.type === "attached" && !this.canReconnect()) {
        this.attached = false;
        currentSocket.close();
        this.onMode("detached");
      }
    });
    currentSocket.addEventListener("close", event => {
      if (this.socket !== currentSocket) return;
      this.socket = null;
      this.attached = false;
      this.lastSentCols = null;
      this.lastSentRows = null;
      this.onMode("detached");
      if (event.code === 4009) {
        this.desiredAttached = false;
        this.onRevoked();
      }
      if (this.desiredAttached && this.canReconnect()) {
        this.reconnectAttempts += 1;
        const delay = Math.min(500 * 2 ** (this.reconnectAttempts - 1), 5000);
        this.onStatus(`连接已断开，${delay}ms 后自动重连...`);
        this.reconnectTimer = window.setTimeout(() => {
          this.reconnectTimer = null;
          void this.attach();
        }, delay);
      }
    });
    currentSocket.addEventListener("error", () => {
      if (this.socket === currentSocket && this.isVisible()) this.onStatus("WebSocket 连接失败", true);
    });
  }
}
