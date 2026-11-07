import { join } from 'node:path';

export const fileDialogSourcePaths = [
  'src/vs/platform/files/common/fileService.ts',
  'src/vs/workbench/services/dialogs/browser/simpleFileDialog.ts',
];

/** 目录失败沿原 FileService／Quick Input 传递，取消与发布仍由原对话框持有。 */
export function transformFileDialog(root, path, source, replaceOnce) {
  if (path === join(root, 'upstream', fileDialogSourcePaths[0])) {
    let contents = replaceOnce(source, `\t\t} catch (error) {

\t\t\t// Specially handle file not found case as file operation result`, `\t\t} catch (error) {
      if (resource.scheme === 'boxteam-file') throw ensureFileSystemProviderError(error);

\t\t\t// Specially handle file not found case as file operation result`);
    // 只有查找本身缺失才能作为新文件候选；readdir 的 404 保留原 provider 错误。
    contents = replaceOnce(contents, `\t\tconst stat = await provider.stat(resource);

\t\tlet trie:`, `\t\tlet stat: IStat;
    try {
      stat = await provider.stat(resource);
    } catch (error) {
      if (resource.scheme === 'boxteam-file' && toFileSystemProviderErrorCode(error) === FileSystemProviderErrorCode.FileNotFound) {
        throw Object.assign(new FileOperationError(error.message, FileOperationResult.FILE_NOT_FOUND), { cause: error });
      }
      throw error;
    }

\t\tlet trie:`);
    contents = replaceOnce(contents, `\t\t\t\t\t} catch (error) {
\t\t\t\t\t\tthis.logService.trace(error);`, `\t\t\t\t\t} catch (error) {
            if (resource.scheme === 'boxteam-file') throw error;
\t\t\t\t\t\tthis.logService.trace(error);`);
    return replaceOnce(contents, `\t\t\t} catch (error) {
\t\t\t\tthis.logService.trace(error);

\t\t\t\tfileStat.children = [];`, `\t\t\t} catch (error) {
        if (resource.scheme === 'boxteam-file') throw error;
\t\t\t\tthis.logService.trace(error);

\t\t\t\tfileStat.children = [];`);
  }
  if (path !== join(root, 'upstream', fileDialogSourcePaths[1])) return source;
  let contents = replaceOnce(source, 'import * as nls', "import { isCancellationError } from '../../../../base/common/errors.js';\nimport * as nls");
  contents = replaceOnce(contents, 'IFileService, IFileStat, FileKind,', 'IFileService, IFileStat, FileKind, FileOperationError, FileOperationResult,');
  contents = replaceOnce(contents, 'Disposable, DisposableStore, IDisposable, MutableDisposable }', 'Disposable, DisposableStore, IDisposable, MutableDisposable, toDisposable }');
  contents = replaceOnce(contents, 'private readonly onBusyChangeEmitter =', 'private readonly onDidErrorEmitter = this._register(new Emitter<unknown>());\n\tprivate readonly onBusyChangeEmitter =');
  contents = replaceOnce(contents, 'private hidden: boolean = false;', 'private hidden: boolean = false;\n  private confirming = false;');
  contents = replaceOnce(contents, '\tset busy(busy: boolean) {', '\tset busy(busy: boolean) {\n    if (this._store.isDisposed || (!busy && this.updatingPromise)) return;');
  contents = replaceOnce(contents, `\t\t\tawait this.tryUpdateItems(folderValue, this.currentFolder, true);
\t\t\tthis.filePickBox.value = startingValue;`, `      try {
        await this.tryUpdateItems(folderValue, this.currentFolder, true);
        if (!this._store.isDisposed && this.filePickBox.value === folderValue) this.filePickBox.value = startingValue;
      } catch (error) {
        this.onDidErrorEmitter.fire(error);
      }`);
  contents = replaceOnce(contents, `\t\treturn new Promise<URI | undefined>((resolve) => {
\t\t\tthis.pickResource(true).then(result => {
\t\t\t\tresolve(Array.isArray(result) ? result[0] : result);
\t\t\t});
\t\t});`, `    const result = await this.pickResource(true);
    return Array.isArray(result) ? result[0] : result;`);
  contents = replaceOnce(contents, `\t\t\t} catch (e) {
\t\t\t\t// The file or folder doesn't exist`, `\t\t\t} catch (e) {
        if (this.scheme === 'boxteam-file' && toFileSystemProviderErrorCode(e) !== FileSystemProviderErrorCode.FileNotFound) throw e;
\t\t\t\t// The file or folder doesn't exist`);
  contents = replaceOnce(contents, 'return new Promise<URI[] | URI | undefined>((resolve) => {', 'return new Promise<URI[] | URI | undefined>((resolve, reject) => {');
  contents = replaceOnce(contents, '\t\t\tlet isResolving: number = 0;', '\t\t\tlet settled = false;\n      let isHandingOff = false;');
  contents = replaceOnce(contents, '\t\t\tconst doResolve = (uriOrUris: URI | URI[] | undefined) => {', `      const finish = () => {
        settled = true;
        this.contextKey.set(false);
        this.updatingPromise?.cancel();
        this.updatingPromise = undefined;
        this.busy = false;
      };
      const doReject = (error: unknown) => {
        if (settled || isCancellationError(error)) return;
        finish();
        reject(error);
      };
      this._register(this.onDidErrorEmitter.event(doReject));
\t\t\tconst doResolve = (uriOrUris: URI | URI[] | undefined) => {
        if (settled) return;`);
  contents = replaceOnce(contents, `\t\t\t\tresolve(uriOrUris);
\t\t\t\tthis.contextKey.set(false);
\t\t\t\tthis.dispose();`, `        finish();
\t\t\t\tresolve(uriOrUris);`);
  contents = replaceOnce(contents, `\t\t\t\tresolve(uriOrUris);
\t\t\t};`, `\t\t\t\tresolve(uriOrUris);
\t\t\t};
      this._register(toDisposable(() => doResolve(undefined)));`);
  contents = replaceOnce(contents, 'if (isAcceptHandled || this.busy) {', 'if (settled || isAcceptHandled || this.busy) {');
  contents = replaceOnce(contents, `\t\t\t\tisAcceptHandled = true;
\t\t\t\tisResolving++;
\t\t\t\tif (this.options.availableFileSystems`, `\t\t\t\tisAcceptHandled = true;
        isHandingOff = true;
\t\t\t\tif (this.options.availableFileSystems`);
  contents = replaceOnce(contents, `\t\t\t\t\t\tdoResolve(result);
\t\t\t\t\t});
\t\t\t\t} else {`, `\t\t\t\t\t\tdoResolve(result);
\t\t\t\t\t}, doReject);
\t\t\t\t} else {`);
  contents = replaceOnce(contents, `\t\t\t\t\t\tdoResolve(result);
\t\t\t\t\t});
\t\t\t\t}
\t\t\t}));`, `\t\t\t\t\t\tdoResolve(result);
\t\t\t\t\t}, doReject);
\t\t\t\t}
\t\t\t}));`);
  contents = replaceOnce(contents, '\t\t\tconst handleAccept = () => {', '\t\t\tconst handleAccept = () => {\n        if (settled) return;');
  contents = replaceOnce(contents, `\t\t\t\tisAcceptHandled = true;
\t\t\t\tisResolving++;
\t\t\t\tthis.onDidAccept().then(resolveValue => {
\t\t\t\t\tif (resolveValue) {
\t\t\t\t\t\tthis.filePickBox.hide();`, `\t\t\t\tisAcceptHandled = true;
\t\t\t\tthis.onDidAccept().then(resolveValue => {
          if (settled) return;
\t\t\t\t\tif (resolveValue) {`);
  contents = replaceOnce(contents, '\t\t\t\t\t\tisResolving--;\n', '');
  contents = replaceOnce(contents, `\t\t\t\t\t\tisAcceptHandled = false;
\t\t\t\t\t}
\t\t\t\t});`, `\t\t\t\t\t\tisAcceptHandled = false;
\t\t\t\t\t}
\t\t\t\t}, doReject);`);
  contents = replaceOnce(contents, 'if (isResolving === 0) {', 'if (!isHandingOff && !this.confirming) {');
  contents = replaceOnce(contents, 'this.updateItems(homedir, true, this.trailing).then(() => {', 'this.updateItems(homedir, true, this.trailing).then(() => {\n        if (settled) return;');
  contents = replaceOnce(contents, `\t\t\t\tthis.busy = false;
\t\t\t});
\t\t});
\t}


\tprivate async handleValueChange`, `\t\t\t\tthis.busy = false;
\t\t\t}, doReject);
\t\t});
\t}


\tprivate async handleValueChange`);
  contents = replaceOnce(contents, `\t\t} catch {
\t\t\t// Since any text can be entered`, `\t\t} catch (error) {
      if (this.scheme === 'boxteam-file') this.onDidErrorEmitter.fire(error);
\t\t\t// Since any text can be entered`);
  contents = replaceOnce(contents, '\tprivate async handleValueChange(value: string) {', '\tprivate async handleValueChange(value: string) {\n    if (this._store.isDisposed) return;');
  contents = replaceOnce(contents, '\t\t\t\t\t\tupdated = await this.tryUpdateItems(value, filePickBoxUri);', '\t\t\t\t\t\tupdated = await this.tryUpdateItems(value, filePickBoxUri);\n            if (this._store.isDisposed || this.filePickBox.value !== value) return;');
  // 原 QuickPick.value setter 已发 ChangeValue；手调会重复读取同一目录。
  contents = replaceOnce(contents, '\t\t\t\tthis.filePickBox.value = wholeValue;\n\t\t\t\tthis.handleValueChange(wholeValue);', '\t\t\t\tthis.filePickBox.value = wholeValue;');
  contents = replaceOnce(contents, '\t\t\tthis.filePickBox.value = wholeValue;\n\t\t\tthis.handleValueChange(wholeValue);', '\t\t\tthis.filePickBox.value = wholeValue;');
  // 输入探测允许路径不存在；权限／离线错误不能继续伪装成候选缺失。
  contents = replaceOnce(contents, `\t\t\t\t} catch (e) {
\t\t\t\t\t// do nothing`, `\t\t\t\t} catch (e) {
          if (this._store.isDisposed || this.filePickBox.value !== value) return UpdateResult.NotUpdated;
          if (this.scheme === 'boxteam-file' && toFileSystemProviderErrorCode(e) !== FileSystemProviderErrorCode.FileNotFound) throw e;
\t\t\t\t\t// do nothing`);
  contents = replaceOnce(contents, `\t\t\t\t\t\t} catch (e) {
\t\t\t\t\t\t\t// do nothing`, `\t\t\t\t\t\t} catch (e) {
              if (this._store.isDisposed || this.filePickBox.value !== value) return UpdateResult.NotUpdated;
              if (this.scheme === 'boxteam-file' && toFileSystemProviderErrorCode(e) !== FileSystemProviderErrorCode.FileNotFound) throw e;
\t\t\t\t\t\t\t// do nothing`);
  contents = replaceOnce(contents, '\t\t\t\tif (stat?.isDirectory', '\t\t\t\tif (this._store.isDisposed || this.filePickBox.value !== value) return UpdateResult.NotUpdated;\n\t\t\t\tif (stat?.isDirectory');
  contents = replaceOnce(contents, '\t\t\t\t\t\tif (statWithoutTrailing?.isDirectory)', '\t\t\t\t\t\tif (this._store.isDisposed || this.filePickBox.value !== value) return UpdateResult.NotUpdated;\n\t\t\t\t\t\tif (statWithoutTrailing?.isDirectory)');
  contents = replaceOnce(contents, `\t\tconst statDirname = parentResult.status === 'fulfilled' ? parentResult.value : undefined;`, `    if (this._store.isDisposed) return false;
    if (this.scheme === 'boxteam-file' && targetResult.status === 'rejected') {
      if (toFileSystemProviderErrorCode(targetResult.reason) !== FileSystemProviderErrorCode.FileNotFound) throw targetResult.reason;
      if (parentResult.status === 'rejected' && toFileSystemProviderErrorCode(parentResult.reason) !== FileSystemProviderErrorCode.FileNotFound) throw parentResult.reason;
    }
\t\tconst statDirname = parentResult.status === 'fulfilled' ? parentResult.value : undefined;`);
  const promptStart = '\tprivate yesNoPrompt(uri: URI, message: string): Promise<boolean> {';
  const promptEnd = '\n\tprivate async validate(uri: URI | undefined): Promise<boolean> {';
  const originalPrompt = contents.slice(contents.indexOf(promptStart), contents.indexOf(promptEnd));
  let prompt = replaceOnce(originalPrompt, `${promptStart}\n`, `${promptStart}\n    if (this._store.isDisposed) return Promise.resolve(false);\n`);
  prompt = replaceOnce(prompt, 'const disposableStore = new DisposableStore();', 'const disposableStore = this._register(new DisposableStore());');
  prompt = replaceOnce(prompt, `\t\t\t\t\tresolve(false);
\t\t\t\t\tthis.filePickBox.show();`, `\t\t\t\t\tresolve(false);
          if (!this._store.isDisposed) {
\t\t\t\t\tthis.filePickBox.show();`);
  prompt = replaceOnce(prompt, '\t\t\t\t\tthis.filePickBox.items = currentItems;', '\t\t\t\t\tthis.filePickBox.items = currentItems;\n          }');
  prompt = replaceOnce(prompt, '\t\t\t\tthis.hidden = false;', '\t\t\t\tthis.confirming = false;\n\t\t\t\tthis.hidden = false;');
  prompt = replaceOnce(prompt, '\t\t\tprompt.show();', '\t\t\tthis.confirming = true;\n\t\t\tprompt.show();');
  contents = replaceOnce(contents, originalPrompt, prompt);
  contents = replaceOnce(contents, `\t\t\t\t\tconst message = nls.localize('remoteFileDialog.validateCreateDirectoryOpen'`, `          if (this._store.isDisposed) return false;
\t\t\t\t\tconst message = nls.localize('remoteFileDialog.validateCreateDirectoryOpen'`);
  contents = replaceOnce(contents, 'if (!shouldCreate) {', 'if (!shouldCreate || this._store.isDisposed) {');
  contents = replaceOnce(contents, '\t\t\t\t\t\tawait this.fileService.createFolder(uri);\n\t\t\t\t\t\treturn true;', '\t\t\t\t\t\tawait this.fileService.createFolder(uri);\n\t\t\t\t\t\treturn !this._store.isDisposed;');
  contents = replaceOnce(contents, `\t\t\t\t\t} catch (e) {
\t\t\t\t\t\tthis.filePickBox.validationMessage = nls.localize('remoteFileDialog.createFolderFailed'`, `\t\t\t\t\t} catch (e) {
            if (this._store.isDisposed) return false;
\t\t\t\t\t\tthis.filePickBox.validationMessage = nls.localize('remoteFileDialog.createFolderFailed'`);
  contents = replaceOnce(contents, '\t\t\t\t// File or folder doesn\'t exist', '\t\t\t\tif (this._store.isDisposed) return false;\n\t\t\t\t// File or folder doesn\'t exist');
  contents = replaceOnce(contents, '\t\tlet candidate = uri;\n\t\twhile (true) {', '\t\tlet candidate = uri;\n\t\twhile (true) {\n      if (this._store.isDisposed) return false;');
  contents = replaceOnce(contents, `\t\t\t\tif (toFileSystemProviderErrorCode(e instanceof Error ? e : undefined) !== FileSystemProviderErrorCode.FileNotFound) {
\t\t\t\t\treturn false;`, `\t\t\t\tif (toFileSystemProviderErrorCode(e instanceof Error ? e : undefined) !== FileSystemProviderErrorCode.FileNotFound) {
          if (this.scheme === 'boxteam-file') throw e;
\t\t\t\t\treturn false;`);
  // 接受验证可能在关闭后才返回；继续沿 busy setter 消费原取消生命周期。
  const acceptStart = '\tprivate async onDidAccept(): Promise<URI | URI[] | undefined> {';
  const acceptEnd = '\n\tprivate root(value: URI) {';
  const originalAccept = contents.slice(contents.indexOf(acceptStart), contents.indexOf(acceptEnd));
  contents = replaceOnce(contents, originalAccept, originalAccept.replaceAll('this.filePickBox.busy = false;', 'this.busy = false;'));
  const updateStart = '\tprivate async updateItems(newFolder: URI, force: boolean = false, trailing?: string): Promise<boolean> {';
  const updateEnd = '\n\tprivate async resolvePathFormatting(): Promise<void> {';
  const originalUpdate = contents.slice(contents.indexOf(updateStart), contents.indexOf(updateEnd));
  let update = replaceOnce(originalUpdate, `\t\t\t} catch (e) {
\t\t\t\t// The file/directory doesn't exist`, `\t\t\t} catch (e) {
        if (token.isCancellationRequested || this._store.isDisposed) return false;
        if (this.scheme === 'boxteam-file' && !(e instanceof FileOperationError && e.fileOperationResult === FileOperationResult.FILE_NOT_FOUND)) throw e;
\t\t\t\t// The file/directory doesn't exist`);
  update = replaceOnce(update, `${updateStart}\n`, `${updateStart}\n    if (this._store.isDisposed) return false;\n`);
  update = replaceOnce(update, `\t\t\tconst newValue =`, `      if (token.isCancellationRequested || this._store.isDisposed) return false;
\t\t\tconst newValue =`);
  update = replaceOnce(update, `\t\t\t\tif (token.isCancellationRequested) {
\t\t\t\t\tthis.busy = false;`, `\t\t\t\tif (token.isCancellationRequested || this._store.isDisposed) {`);
  update = replaceOnce(update, `\t\t\t\tthis.busy = false;
\t\t\t\tthis.updatingPromise = undefined;
`, '');
  update = replaceOnce(update, `\t\treturn updatingPromise;`, `    try {
      return await updatingPromise;
    } catch (error) {
      // 先终结失败，再解除 busy，避免已排队的接受操作消费旧目录项。
      if (this.updatingPromise === updatingPromise && !this._store.isDisposed) this.onDidErrorEmitter.fire(error);
      throw error;
    } finally {
      // 取消的旧更新只释放自己的状态，不能清除后来请求的 busy 或句柄。
      if (this.updatingPromise === updatingPromise) {
        this.updatingPromise = undefined;
        if (!this._store.isDisposed) this.busy = false;
      }
    }`);
  contents = replaceOnce(contents, originalUpdate, update);
  contents = replaceOnce(contents, '\t\tconst backDir = await this.createBackItem(currentFolder);', '\t\tconst backDir = await this.createBackItem(currentFolder);\n    if (token.isCancellationRequested || this._store.isDisposed) return [];');
  contents = replaceOnce(contents, `\t\t} catch (e) {
\t\t\t// ignore
\t\t\tconsole.log(e);`, `\t\t} catch (e) {
      if (token.isCancellationRequested || this._store.isDisposed) return [];
      if (this.scheme === 'boxteam-file' && !(e instanceof FileOperationError && e.fileOperationResult === FileOperationResult.FILE_NOT_FOUND)) throw e;
\t\t\t// ignore
\t\t\tconsole.log(e);`);
  // 每次公共入口创建的原 owner 在同一 finally 释放，包括尚未创建 picker 的失败。
  for (const [start, end] of [
    ['\tpublic async showOpenDialog(options: IOpenDialogOptions = {}): Promise<URI[] | undefined> {', '\n\tpublic async showSaveDialog'],
    ['\tpublic async showSaveDialog(options: ISaveDialogOptions): Promise<URI | undefined> {', '\n\tprivate getOptions'],
  ]) {
    const original = contents.slice(contents.indexOf(start), contents.indexOf(end));
    let method = replaceOnce(original, `${start}\n`, `${start}\n    try {\n`);
    method = replaceOnce(method, '\n\t}\n', '\n    } finally {\n      this.dispose();\n    }\n\t}\n');
    contents = replaceOnce(contents, original, method);
  }
  return contents;
}
