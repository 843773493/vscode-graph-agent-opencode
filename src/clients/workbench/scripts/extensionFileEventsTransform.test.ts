import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { extensionFileEventsSourcePaths } from './extensions/extensionFileEventsTransform.mjs';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);

async function transformed(relativePath: string): Promise<string> {
  const path = join(root, 'upstream', relativePath);
  return transform(path, await readFile(path, 'utf8'));
}

test('窗口 BulkEdit SourcePaths 只包含通用服务和预览 owner', () => {
  expect(extensionFileEventsSourcePaths).toEqual([
    'src/vs/workbench/contrib/bulkEdit/browser/bulkEditService.ts',
    'src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEdit.contribution.ts',
    'src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditPane.ts',
    'src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditPreview.ts',
  ]);
});

test('窗口 BulkEdit 将取消令牌传给预览并在确认等待期间取消', async () => {
  const converted = await transformed('src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEdit.contribution.ts');
  expect(converted).toContain('this._previewEdit(edits, options?.token ?? CancellationToken.None)');
  expect(converted).toContain('if (!confirmed || token.isCancellationRequested)');
});

test('窗口 BulkEdit PreviewPane 的迟到异步结果不会覆盖新输入', async () => {
  const converted = await transformed('src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditPane.ts');
  expect(converted).toContain('if (this._currentInput !== input || this._currentProvider !== provider) return;');
  expect(converted).toContain('this._sessionDisposables.add(token.onCancellationRequested(() => this._done(false)))');
});

test('窗口 BulkEdit 保留原用户版本校验并在完成后释放模型引用', async () => {
  const converted = await transformed('src/vs/workbench/contrib/bulkEdit/browser/bulkEditService.ts');
  expect(converted).toContain('const references = new DisposableStore()');
  expect(converted).toContain('version.current() !== version.expected');
  expect(converted).toContain('finally { references.dispose(); }');
  expect(converted).toContain('CancellationTokenSource(options?.token)');
});

test('窗口 BulkEditPreview 的异步资源获取在 dispose 后释放且拒绝迟到内容', async () => {
  const converted = await transformed('src/vs/workbench/contrib/bulkEdit/browser/preview/bulkEditPreview.ts');
  expect(converted).toContain('if (this._disposables.isDisposed) { ref.dispose(); return; }');
  expect(converted).toContain('if (this._disposables.isDisposed) throw new CancellationError();');
  expect(converted).toContain('if (!this._disposables.isDisposed) onUnexpectedError(error)');
});

test('转换链不再触碰旧宿主文件事件和保存参与者 owner', async () => {
  for (const path of [
    'src/vs/workbench/api/browser/mainThreadFileSystemEventService.ts',
    'src/vs/workbench/api/browser/mainThreadSaveParticipant.ts',
    'src/vs/workbench/api/browser/mainThreadNotebookSaveParticipant.ts',
    'src/vs/workbench/api/browser/mainThreadBulkEdits.ts',
  ]) {
    const original = await readFile(join(root, 'upstream', path), 'utf8');
    expect(await transform(join(root, 'upstream', path), original)).toBe(original);
  }
});
