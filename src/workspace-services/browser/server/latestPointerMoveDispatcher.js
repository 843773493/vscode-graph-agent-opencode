export class LatestPointerMoveDispatcher {
  constructor(dispatch) {
    this.dispatch = dispatch;
    this.pending = null;
    this.active = null;
    this.closed = false;
    this.superseded = 0;
  }

  offer(message) {
    if (this.closed) {
      return;
    }
    if (this.pending !== null) {
      this.superseded += 1;
    }
    this.pending = message;
    this.start();
  }

  async flush() {
    while (this.active !== null) {
      await this.active;
    }
  }

  close() {
    this.closed = true;
    this.pending = null;
  }

  start() {
    if (this.active !== null || this.closed) {
      return;
    }
    const execution = this.drain();
    this.active = execution.finally(() => {
      this.active = null;
      if (this.pending !== null && !this.closed) {
        this.start();
      }
    });
  }

  async drain() {
    while (this.pending !== null && !this.closed) {
      const message = this.pending;
      this.pending = null;
      await this.dispatch(message);
    }
  }
}
