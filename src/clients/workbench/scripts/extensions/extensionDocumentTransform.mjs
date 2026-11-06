import { join } from 'node:path';

export const extensionDocumentSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadDocumentsAndEditors.ts',
  'src/vs/workbench/api/browser/mainThreadDocuments.ts',
];

export function transformExtensionDocuments(root, path, source, replaceOnce) {
  const paths = extensionDocumentSourcePaths.map(value => join(root, 'upstream', value));
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
