/** 只收集原服务已经启动的异步任务；安装事实仍由原 profile 服务持有。 */
export class NativeManagementLifecycle {
  private readonly pending = new Set<Promise<void>>();
  private readonly failures: unknown[] = [];

  track(task: Promise<unknown>): void {
    const settled = task.then(() => undefined, error => { this.failures.push(error); });
    this.pending.add(settled);
    void settled.then(() => this.pending.delete(settled));
  }

  async drain(): Promise<void> {
    // 原事件回调还可以启动后续原任务，必须一直等到整个已准入链路为空。
    while (this.pending.size) await Promise.all([...this.pending]);
    if (this.failures.length === 1) throw this.failures[0];
    if (this.failures.length) throw new AggregateError(this.failures, '原生扩展管理异步任务失败');
  }
}
