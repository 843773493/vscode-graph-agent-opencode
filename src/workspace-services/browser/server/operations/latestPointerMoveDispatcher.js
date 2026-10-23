export class LatestPointerMoveDispatcher {
  constructor(dispatchPointerMove) {
    this.dispatchPointerMove = dispatchPointerMove;
    // 仅保留尚未交给回调的最新移动，不包含正在执行的事件。
    this.pendingPointerMove = null;
    /** @type {Promise<void>|null} 整个排空循环的完成句柄，不是浏览器或指针活动状态。 */
    this.drainPromise = null;
    this.closed = false;
    // 只统计待处理事件被后来的移动替换，不统计已交给回调的事件。
    this.supersededMoveCount = 0;
  }

  offer(pointerMoveMessage) {
    if (this.closed) {
      return;
    }
    if (this.pendingPointerMove !== null) {
      this.supersededMoveCount += 1;
    }
    this.pendingPointerMove = pointerMoveMessage;
    this.start();
  }

  async flush() {
    while (this.drainPromise !== null) {
      await this.drainPromise;
    }
  }

  close() {
    this.closed = true;
    this.pendingPointerMove = null;
  }

  start() {
    if (this.drainPromise !== null || this.closed) {
      return;
    }
    const drainExecution = this.drain();
    this.drainPromise = drainExecution.finally(() => {
      this.drainPromise = null;
      if (this.pendingPointerMove !== null && !this.closed) {
        this.start();
      }
    });
  }

  async drain() {
    while (this.pendingPointerMove !== null && !this.closed) {
      const pointerMoveMessage = this.pendingPointerMove;
      this.pendingPointerMove = null;
      await this.dispatchPointerMove(pointerMoveMessage);
    }
  }
}
