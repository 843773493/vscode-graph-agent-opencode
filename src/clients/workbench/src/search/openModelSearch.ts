import { t } from '../../../shared/core/i18n/index.js';
import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Disposable, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { FileAccess } from '../../upstream/src/vs/base/common/network.js';
import { WebWorkerDescriptor } from '../../upstream/src/vs/platform/webWorker/browser/webWorkerDescriptor.js';
import type { IWebWorkerService } from '../../upstream/src/vs/platform/webWorker/browser/webWorkerService.js';
import type { OpenModelSearchRequest, OpenModelSearchWorker } from './openModelSearchWorker.js';

/** 查询持有唯一 worker；结束或取消即释放，不在主线程重做匹配。 */
export class OpenModelSearch extends Disposable {
  private readonly source: CancellationTokenSource;
  private readonly worker;
  private timedOut = false;

  constructor(workers: IWebWorkerService, token = CancellationToken.None, deadlineAt?: number) {
    super();
    this.source = new CancellationTokenSource(token);
    this._register(toDisposable(() => this.source.dispose(true)));
    this.worker = this._register(workers.createWorkerClient<OpenModelSearchWorker>(new WebWorkerDescriptor({
      esmModuleLocation: () => FileAccess.asBrowserUri('boxteam/openModelSearchWorker.js'), label: 'open-model-search',
    })));
    this._register(this.source.token.onCancellationRequested(() => this.worker.dispose()));
    if (deadlineAt !== undefined) {
      const timer = setTimeout(() => { this.timedOut = true; this.source.cancel(); }, Math.max(0, deadlineAt - Date.now()));
      this._register(toDisposable(() => clearTimeout(timer)));
    }
  }

  async findMatches(text: string, request: OpenModelSearchRequest) {
    try {
      return await raceCancellationError(this.worker.proxy.$findMatches(text, request), this.source.token);
    } catch (error) {
      if (this.timedOut) throw new Error(t('quickAccess.files.deadlineExceeded'), { cause: error });
      throw error;
    }
  }
}
