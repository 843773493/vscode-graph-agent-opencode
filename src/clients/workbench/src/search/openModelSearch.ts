import { t } from '../../../shared/core/i18n/index.js';
import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Disposable, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { FileAccess } from '../../upstream/src/vs/base/common/network.js';
import { WebWorkerDescriptor } from '../../upstream/src/vs/platform/webWorker/browser/webWorkerDescriptor.js';
import type { IWebWorkerService } from '../../upstream/src/vs/platform/webWorker/browser/webWorkerService.js';
import type { OpenModelSearchRequest, OpenModelSearchWorker } from './openModelSearchWorker.js';
import { Range, type IRange } from '../../upstream/src/vs/editor/common/core/range.js';
import type { IPosition } from '../../upstream/src/vs/editor/common/core/position.js';
import { FindMatch } from '../../upstream/src/vs/editor/common/model.js';
import type { IPatternInfo } from '../../upstream/src/vs/workbench/services/search/common/search.js';

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
    return this.request(this.worker.proxy.$findMatches(text, request));
  }

  async findEditorMatches(text: string, pattern: IPatternInfo, ranges: IRange[], captureMatches: boolean, limit: number) {
    const matches = await this.request(this.worker.proxy.$findEditorMatches(text, pattern, ranges, captureMatches, limit));
    return matches.map(match => new FindMatch(Range.lift(match.range), match.matches));
  }

  async findEditorMatch(text: string, pattern: IPatternInfo, position: IPosition, previous: boolean, captureMatches: boolean) {
    const match = await this.request(this.worker.proxy.$findEditorMatch(text, pattern, position, previous, captureMatches));
    return match ? new FindMatch(Range.lift(match.range), match.matches) : null;
  }

  async replaceAll(text: string, pattern: IPatternInfo, replacement: string, preserveCase: boolean) {
    return this.request(this.worker.proxy.$replaceAll(text, pattern, replacement, preserveCase));
  }

  private async request<T>(result: Promise<T>): Promise<T> {
    try {
      return await raceCancellationError(result, this.source.token);
    } catch (error) {
      if (this.timedOut) throw new Error(t('quickAccess.files.deadlineExceeded'), { cause: error });
      throw error;
    }
  }
}
