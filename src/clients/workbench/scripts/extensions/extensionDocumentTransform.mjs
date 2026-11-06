import { join } from 'node:path';

export const extensionDocumentSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadDocumentsAndEditors.ts',
  'src/vs/workbench/api/browser/mainThreadDocuments.ts',
  'src/vs/workbench/api/browser/mainThreadDiagnostics.ts',
];

export function transformExtensionDocuments(root, path, source, replaceOnce) {
  const paths = extensionDocumentSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[2]) {
    let contents = `import { hostAllowsResourceLink, hostOwnsWorkspaceResource } from ${JSON.stringify(join(root, 'src/extensions/targetUris.js'))};\n` + source;
    contents = replaceOnce(contents, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    contents = replaceOnce(contents, 'private readonly _activeOwners = new Set<string>();', 'private readonly _activeOwners = new Set<string>();\n  private _disposed = false;');
    contents = replaceOnce(contents, '\tdispose(): void {', `
  private ownerKey(owner: string): string {
    if (this._disposed) throw new Error('诊断所属宿主代际已经关闭');
    // 集合名称仍是公开 API；原 MarkerService 的内部 owner 按原 extHostId 区分运行代际。
    return this.extHostContext.extensionHost.binding ? JSON.stringify([this.extHostId, owner]) : owner;
  }

  private assertResource(resource: URI): void {
    if (!hostOwnsWorkspaceResource(this.extHostContext.extensionHost, resource)) {
      throw new Error('诊断位置不属于发起宿主的工作区视图：' + resource.toString());
    }
  }

  private projectMarker(marker: IMarkerData): IMarkerData {
    if (!this.extHostContext.extensionHost.binding) return marker;
    const code = marker.code;
    // 只投影位置字段，保留普通诊断正文；不修改其它消费者仍在读取的权威 marker。
    return { ...marker,
      relatedInformation: marker.relatedInformation?.filter(related => hostAllowsResourceLink(this.extHostContext.extensionHost, URI.revive(related.resource))),
      code: code && typeof code !== 'string' && !hostAllowsResourceLink(this.extHostContext.extensionHost, URI.revive(code.target)) ? code.value : code };
  }

\tdispose(): void {
    if (this._disposed) return;
    this._disposed = true;`);
    contents = replaceOnce(contents, 'for (const resource of resources) {', `for (const resource of resources) {
      if (!hostOwnsWorkspaceResource(this.extHostContext.extensionHost, resource)) continue;`);
    contents = replaceOnce(contents, 'allMarkerData.filter(marker => marker.origin !== this.extHostId);', 'allMarkerData.filter(marker => marker.origin !== this.extHostId).map(marker => this.projectMarker(marker));');
    contents = replaceOnce(contents, '$changeMany(owner: string, entries: IMarkerChange[]): void {', `$changeMany(owner: string, entries: IMarkerChange[]): void {
    owner = this.ownerKey(owner);
    // 整批核归属后再发布，不能先修改合法首项，再因后续跨目标位置失败。
    for (const [resource, markers] of entries) {
      this.assertResource(URI.revive(resource));
      for (const marker of markers ?? []) {
        for (const related of marker.relatedInformation ?? []) {
          if (!hostAllowsResourceLink(this.extHostContext.extensionHost, URI.revive(related.resource))) {
            throw new Error('诊断关联位置不属于发起宿主视图或已绑定目标资源');
          }
        }
        if (marker.code && typeof marker.code !== 'string' && !hostAllowsResourceLink(this.extHostContext.extensionHost, URI.revive(marker.code.target))) {
          throw new Error('诊断链接不属于发起宿主视图或已绑定目标资源');
        }
      }
    }`);
    contents = replaceOnce(contents, 'if (marker.origin === undefined)', 'if (this.extHostContext.extensionHost.binding || marker.origin === undefined)');
    return replaceOnce(contents, '$clear(owner: string): void {', '$clear(owner: string): void {\n    owner = this.ownerKey(owner);');
  }
  if (path === paths[1]) return replaceOnce(source, 'return !!model && shouldSynchronizeModel(model);', 'return !!model && this._modelTrackers.has(resource) && shouldSynchronizeModel(model);');
  if (path !== paths[0]) return source;
  let contents = `import { hostOwnsWorkspaceResource } from ${JSON.stringify(join(root, 'src/extensions/targetUris.js'))};
import type { URI } from '../../../base/common/uri.js';\n` + source;
  contents = replaceOnce(contents, 'private readonly _onDidChangeState: (delta: DocumentAndEditorStateDelta) => void,', 'private readonly _onDidChangeState: (delta: DocumentAndEditorStateDelta) => void,\n    private readonly _acceptResource: (resource: URI) => boolean,');
  contents = contents.replaceAll('if (!shouldSynchronizeModel(model))', 'if (!this._acceptResource(model.uri) || !shouldSynchronizeModel(model))');
  contents = replaceOnce(contents, 'if (shouldSynchronizeModel(model)) {', 'if (this._acceptResource(model.uri) && shouldSynchronizeModel(model)) {');
  contents = replaceOnce(contents, 'if (editor.hasModel() && model && shouldSynchronizeModel(model)', 'if (editor.hasModel() && model && this._acceptResource(model.uri) && shouldSynchronizeModel(model)');
  return replaceOnce(contents, 'new MainThreadDocumentAndEditorStateComputer(delta => this._onDelta(delta), _modelService,', 'new MainThreadDocumentAndEditorStateComputer(delta => this._onDelta(delta), resource => hostOwnsWorkspaceResource(extHostContext.extensionHost, resource), _modelService,');
}
