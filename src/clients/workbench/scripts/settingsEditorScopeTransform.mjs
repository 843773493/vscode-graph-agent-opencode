import { join } from 'node:path';

export const settingsEditorScopeSourcePaths = [
  'src/vs/workbench/common/editor/editorInput.ts',
  'src/vs/workbench/browser/parts/editor/editorPanes.ts',
  'src/vs/workbench/browser/parts/editor/sideBySideEditor.ts',
  'src/vs/workbench/common/editor/sideBySideEditorInput.ts',
];

function replaceSection(source, start, end, replacement, replaceOnce) {
  const from = source.indexOf(start), to = source.indexOf(end, from + start.length);
  if (from < 0 || to < 0 || source.indexOf(start, from + start.length) >= 0) {
    throw new Error(`编辑器作用域转换锚点必须唯一且有序：${start}`);
  }
  return replaceOnce(source, source.slice(from, to), replacement);
}

/** 在原输入与原 Pane 生命周期内准备作用域 DI，不增加编辑器注册或宿主状态。 */
export function transformSettingsEditorScope(root, path, contents, replaceOnce) {
  const upstream = join(root, 'upstream');
  if (path === join(upstream, settingsEditorScopeSourcePaths[0])) {
    contents = replaceOnce(contents, "import { IDisposable } from '../../../base/common/lifecycle.js';",
      "import { DisposableStore, IDisposable } from '../../../base/common/lifecycle.js';\nimport { CancellationToken } from '../../../base/common/cancellation.js';\nimport { IInstantiationService } from '../../../platform/instantiation/common/instantiation.js';");
    contents = replaceOnce(contents, '\treadonly closeHandler?: IEditorCloseHandler;', `\treadonly closeHandler?: IEditorCloseHandler;

	/** 可选的 Pane 生命周期所有者；启用后整个输入寿命内保持同一身份。 */
	get editorPaneScope(): EditorInput | undefined { return undefined; }

	private editorPaneVersion = 0;
	get editorPaneScopeVersion(): number { return this.editorPaneVersion; }
	private readonly _onDidChangeEditorPaneScope = this._register(new Emitter<void>());
	readonly onDidChangeEditorPaneScope = this._onDidChangeEditorPaneScope.event;
	protected invalidateEditorPaneScope(): void {
		this.editorPaneVersion++;
		this._onDidChangeEditorPaneScope.fire();
	}

	/** 从当前组的 DI 派生服务；新增服务必须登记到 Pane 持有的 disposables。 */
	async prepareEditorPane(parent: IInstantiationService, disposables: DisposableStore, token: CancellationToken): Promise<IInstantiationService> {
		return parent;
	}`);
  }

  if (path === join(upstream, settingsEditorScopeSourcePaths[1])) {
    contents = replaceOnce(contents, "import { Emitter } from '../../../../base/common/event.js';",
      "import { Emitter, Event } from '../../../../base/common/event.js';");
    contents = replaceOnce(contents, 'IEditorProgressService, LongRunningOperation', 'IEditorProgressService, IOperation, LongRunningOperation');
    contents = replaceOnce(contents, "import { isCancellationError } from '../../../../base/common/errors.js';",
      "import { CancellationError, isCancellationError } from '../../../../base/common/errors.js';");
    contents = replaceOnce(contents, '\tprivate readonly editorPanes: EditorPane[] = [];', `	private readonly editorPanes: { descriptor: IEditorPaneDescriptor; scope: EditorInput | undefined; version: number; pane: EditorPane; disposables: DisposableStore }[] = [];
	private editorOpening: { editor: EditorInput; operation: IOperation } | undefined;`);
    contents = replaceOnce(contents, '\tprivate registerListeners(): void {', `	private registerListeners(): void {
		this._register(this.groupView.onDidCloseEditor(e => this.doCloseScopedEditorPane(e.editor)));`);
    contents = replaceSection(contents, '\tprivate async doOpenEditor(', '\tprivate shouldRestoreFocus(', `	private async doOpenEditor(descriptor: IEditorPaneDescriptor, editor: EditorInput, options: IEditorOptions | undefined, internalOptions: IInternalEditorOpenOptions | undefined, context: IEditorOpenContext = Object.create(null)): Promise<IOpenEditorResult> {
		// 错误和信任占位仍用原组服务，不能再次执行失败的输入准备。
		const scope = this.editorPanesRegistry.getEditorPane(editor) === descriptor ? editor.editorPaneScope : undefined;
		const version = scope?.editorPaneScopeVersion ?? 0;
		if (scope?.isDisposed()) {
			return { cancelled: true };
		}

		let pane = this.editorPanes.find(entry => entry.descriptor === descriptor && entry.scope === scope && entry.version === version)?.pane;
		const reuseInput = pane && pane === this._activeEditorPane && pane.input?.matches(editor) && !options?.forceReload;
		if (pane !== this._activeEditorPane) {
			this.doHideActiveEditorPane();
		}

		// 同输入的选项更新等待原 setInput；其余操作从服务准备开始共用原取消令牌。
		const operation = reuseInput ? undefined : this.editorOperation.start(this.layoutService.isRestored() ? 800 : 3200);
		if (operation) {
			this.editorOpening = { editor, operation };
		}
		const preparation = scope && Event.any(scope.onWillDispose, scope.onDidChangeEditorPaneScope)(() => operation?.stop());
		const activeElementBeforePreparation = scope && !pane ? getActiveElement() : undefined;
		let services: DisposableStore | undefined;
		try {
			if (!pane) {
				let instantiationService = this.instantiationService;
				if (scope) {
					services = new DisposableStore();
					const opening = assertReturnsDefined(operation);
					instantiationService = await editor.prepareEditorPane(this.instantiationService, services, opening.token);
					if (this._store.isDisposed || scope.isDisposed() || scope.editorPaneScopeVersion !== version || opening.token.isCancellationRequested || !opening.isCurrent()) {
						return { cancelled: true };
					}
				}
				pane = this.doInstantiateEditorPane(descriptor, scope, version, instantiationService, services);
				services = undefined;
			}
			if (!pane || operation && (!operation.isCurrent() || operation.token.isCancellationRequested)) {
				return { cancelled: true };
			}

			this.doShowEditorPane(pane);
			if (operation && (!operation.isCurrent() || operation.token.isCancellationRequested)) {
				return { cancelled: true };
			}
			const activeElement = activeElementBeforePreparation === undefined ? getActiveElement() : activeElementBeforePreparation;
			const result = await this.doSetInput(pane, editor, options, context, operation);
			const cancelled = result.cancelled || this._activeEditorPane !== pane || !!operation && (!operation.isCurrent() || operation.token.isCancellationRequested);
			if (!cancelled) {
				const focus = !options?.preserveFocus;
				if (focus && this.shouldRestoreFocus(activeElement)) {
					pane.focus();
				} else if (!internalOptions?.preserveWindowOrder) {
					this.hostService.moveTop(getWindowById(this.groupView.windowId, true).window);
				}
			}
			return { pane, changed: result.changed, cancelled };
		} catch (error) {
			if (operation && (!operation.isCurrent() || operation.token.isCancellationRequested)) {
				return { cancelled: true };
			}
			throw error;
		} finally {
			preparation?.dispose();
			// 等待准备结算后再释放，保证取消后迟到登记的 child 仍由同一 store 接住。
			services?.dispose();
			if (this.editorOpening?.operation === operation) {
				this.editorOpening = undefined;
			}
			operation?.stop();
		}
	}

`, replaceOnce);
    contents = replaceSection(contents, '\tprivate doShowEditorPane(', '\tprivate doCreateEditorPane(', `	private doShowEditorPane(editorPane: EditorPane): EditorPane {
		if (this._activeEditorPane === editorPane) {
			return editorPane;
		}

		// 旧 Pane 已在新 operation 开始前隐藏，不能在准备完成后停止新 operation。
		this.doCreateEditorPane(editorPane);
		this.doSetActiveEditorPane(editorPane);
		const container = assertReturnsDefined(editorPane.getContainer());
		this.editorPanesParent.appendChild(container);
		show(container);
		editorPane.setVisible(true);
		if (this.pagePosition) {
			editorPane.layout(new Dimension(this.pagePosition.width, this.pagePosition.height), { top: this.pagePosition.top, left: this.pagePosition.left });
		}
		if (this.boundarySashes) {
			editorPane.setBoundarySashes(this.boundarySashes);
		}
		return editorPane;
	}

`, replaceOnce);
    contents = replaceOnce(contents, `	private doCreateEditorPane(descriptor: IEditorPaneDescriptor): EditorPane {

		// Instantiate editor
		const editorPane = this.doInstantiateEditorPane(descriptor);`, '\tprivate doCreateEditorPane(editorPane: EditorPane): EditorPane {');
    contents = replaceOnce(contents, '\t\t\t\teditorPane.create(editorPaneContainer);',
      '\t\t\t\teditorPane.create(editorPaneContainer);\n\t\t\t\tif (!this.editorPanes.some(entry => entry.pane === editorPane)) {\n\t\t\t\t\tthrow new CancellationError();\n\t\t\t\t}');
    contents = replaceOnce(contents, '\t\t\t\thide(editorPaneContainer);\n\n\t\t\t\tthrow error;',
      '\t\t\t\thide(editorPaneContainer);\n\t\t\t\tthis.doDisposeEditorPane(editorPane, false);\n\n\t\t\t\tthrow error;');
    contents = replaceSection(contents, 'private doInstantiateEditorPane(', '\tprivate doSetActiveEditorPane(', `	private doInstantiateEditorPane(descriptor: IEditorPaneDescriptor, scope?: EditorInput, version = scope?.editorPaneScopeVersion ?? 0, instantiationService = this.instantiationService, services?: DisposableStore): EditorPane {
		const existing = this.editorPanes.find(entry => entry.descriptor === descriptor && entry.scope === scope && entry.version === version);
		if (existing) {
			return existing.pane;
		}

		const pane = descriptor.instantiate(instantiationService, this.groupView);
		if (this._store.isDisposed || scope?.isDisposed() || scope && scope.editorPaneScopeVersion !== version || services?.isDisposed) {
			pane.dispose();
			throw new CancellationError();
		}
		// Pane 必须先于其构造服务释放。
		const disposables = this._register(new DisposableStore());
		disposables.add(pane);
		if (services) {
			disposables.add(services);
		}
		this.editorPanes.push({ descriptor, scope, version, pane, disposables });
		if (scope) {
			disposables.add(Event.any(scope.onWillDispose, scope.onDidChangeEditorPaneScope)(() => this.doDisposeEditorPane(pane)));
		}
		return pane;
	}

	private doDisposeEditorPane(pane: EditorPane, cancelOpening = true): void {
		const index = this.editorPanes.findIndex(entry => entry.pane === pane);
		if (index < 0) {
			return;
		}
		const [entry] = this.editorPanes.splice(index, 1);
		if (cancelOpening && entry.scope && this.editorOpening?.editor.editorPaneScope === entry.scope) {
			this.editorOpening.operation.stop();
		}
		if (this._activeEditorPane === pane) {
			this.doHideActiveEditorPane();
		}
		this.mapEditorPaneToPendingSetInput.delete(pane);
		pane.getContainer()?.remove();
		this._store.delete(entry.disposables);
	}

	private doCloseScopedEditorPane(editor: EditorInput): void {
		const scope = editor.editorPaneScope;
		if (!scope) {
			return;
		}
		if (this.editorOpening?.editor.editorPaneScope === scope) {
			this.editorOpening.operation.stop();
		}
		for (const entry of [...this.editorPanes]) {
			if (entry.scope === scope) {
				this.doDisposeEditorPane(entry.pane);
			}
		}
	}

`, replaceOnce);
    contents = replaceSection(contents, '\tprivate async doSetInput(', '\tprivate doHideActiveEditorPane(', `	private async doSetInput(editorPane: EditorPane, editor: EditorInput, options: IEditorOptions | undefined, context: IEditorOpenContext, operation: IOperation | undefined): Promise<{ changed: boolean; cancelled: boolean }> {
		let inputMatches = editorPane.input?.matches(editor);
		if (inputMatches && !options?.forceReload) {
			if (this.mapEditorPaneToPendingSetInput.has(editorPane)) {
				await this.mapEditorPaneToPendingSetInput.get(editorPane);
			}
			inputMatches = editorPane.input?.matches(editor);
			if (inputMatches) {
				editorPane.setOptions(options);
			}
			return { changed: false, cancelled: !inputMatches };
		}

		editorPane.clearInput();
		const pendingSetInput = editorPane.setInput(editor, options, context, assertReturnsDefined(operation).token);
		this.mapEditorPaneToPendingSetInput.set(editorPane, pendingSetInput);
		try {
			await pendingSetInput;
		} finally {
			if (this.mapEditorPaneToPendingSetInput.get(editorPane) === pendingSetInput) {
				this.mapEditorPaneToPendingSetInput.delete(editorPane);
			}
		}
		return { changed: !inputMatches, cancelled: false };
	}

`, replaceOnce);
    contents = replaceOnce(contents, '\tcloseEditor(editor: EditorInput): void {', `	closeEditor(editor: EditorInput): void {
		this.doCloseScopedEditorPane(editor);
		if (this.editorOpening && editor.matches(this.editorOpening.editor)) {
			this.editorOpening.operation.stop();
		}`);
  }

  if (path === join(upstream, settingsEditorScopeSourcePaths[2])) {
    contents = replaceOnce(contents, "import { CancellationToken } from '../../../../base/common/cancellation.js';",
      "import { CancellationToken } from '../../../../base/common/cancellation.js';\nimport { CancellationError } from '../../../../base/common/errors.js';");
    contents = replaceOnce(contents, '\t\tawait super.setInput(input, options, context, token);', `		await super.setInput(input, options, context, token);
		if (token.isCancellationRequested || this.input !== input || input.isDisposed()) {
			throw new CancellationError();
		}`);
    contents = replaceOnce(contents, '\t\t\tthis.createEditors(input);', '\t\t\tawait this.createEditors(input, token);');
    contents = replaceOnce(contents, 'if (!oldInput || !input.matches(oldInput)) {',
      'if (!oldInput || !input.matches(oldInput) || !this.primaryEditorPane || !this.secondaryEditorPane) {');
    contents = replaceOnce(contents, '\t\t// Restore any previous view state', `		if (token.isCancellationRequested || this.input !== input || input.isDisposed()) {
			throw new CancellationError();
		}

		// Restore any previous view state`);
    contents = replaceOnce(contents, '\t\t]);\n\n\t\t// Update focus if target is provided', `		]);

		if (token.isCancellationRequested || this.input !== input || input.isDisposed()) {
			throw new CancellationError();
		}

		// Update focus if target is provided`);
    contents = replaceSection(contents, '\tprivate createEditors(', '\tprivate onDidFocusChange(', `	private async createEditors(newInput: SideBySideEditorInput, token: CancellationToken): Promise<void> {
		const disposables = this.editorDisposables.add(new DisposableStore());
		const panes = disposables.add(new DisposableStore()), services = disposables.add(new DisposableStore());
		const disposeCurrent = () => {
			if (!disposables.isDisposed) {
				this.disposeEditors();
			}
		};
		disposables.add(newInput.onWillDispose(disposeCurrent));
		const cancellation = token.onCancellationRequested(disposeCurrent);
		let created = false;
		try {
			const [secondary, primary] = await Promise.all([
				this.doCreateEditor(newInput.secondary, assertReturnsDefined(this.secondaryEditorContainer), token, disposables, panes, services, disposeCurrent),
				this.doCreateEditor(newInput.primary, assertReturnsDefined(this.primaryEditorContainer), token, disposables, panes, services, disposeCurrent)
			]);
			if (disposables.isDisposed || token.isCancellationRequested || this.input !== newInput) {
				throw new CancellationError();
			}
			this.secondaryEditorPane = secondary;
			this.primaryEditorPane = primary;
			this.layout(this.dimension);
			this._onDidChangeSizeConstraints.input = Event.any(
				Event.map(secondary.onDidChangeSizeConstraints, () => undefined),
				Event.map(primary.onDidChangeSizeConstraints, () => undefined)
			);
			this.onDidCreateEditors.fire(undefined);
			panes.add(primary.onDidFocus(() => this.onDidFocusChange(Side.PRIMARY)));
			panes.add(secondary.onDidFocus(() => this.onDidFocusChange(Side.SECONDARY)));
			created = true;
		} finally {
			cancellation.dispose();
			if (!created) {
				disposeCurrent();
				this.editorDisposables.delete(disposables);
			}
		}
	}

	private async doCreateEditor(editorInput: EditorInput, container: HTMLElement, token: CancellationToken, disposables: DisposableStore, panes: DisposableStore, services: DisposableStore, disposeCurrent: () => void): Promise<EditorPane> {
		const descriptor = Registry.as<IEditorPaneRegistry>(EditorExtensions.EditorPane).getEditorPane(editorInput);
		if (!descriptor) {
			throw new Error('No editor pane descriptor for editor found');
		}
		const scope = editorInput.editorPaneScope;
		const version = scope?.editorPaneScopeVersion;
		if (disposables.isDisposed || token.isCancellationRequested || scope?.isDisposed() || editorInput.isDisposed()) {
			throw new CancellationError();
		}
		if (scope) {
			disposables.add(Event.any(scope.onWillDispose, scope.onDidChangeEditorPaneScope)(disposeCurrent));
		}
		const preparation = new DisposableStore();
		let retained = false;
		try {
			const instantiationService = scope
				? await editorInput.prepareEditorPane(this.instantiationService, preparation, token)
				: this.instantiationService;
			if (disposables.isDisposed || token.isCancellationRequested || scope?.isDisposed() || scope?.editorPaneScopeVersion !== version || editorInput.isDisposed()) {
				throw new CancellationError();
			}
			const editorPane = descriptor.instantiate(instantiationService, this.group);
			if (disposables.isDisposed || token.isCancellationRequested || scope?.isDisposed() || scope?.editorPaneScopeVersion !== version || editorInput.isDisposed()) {
				editorPane.dispose();
				throw new CancellationError();
			}
			panes.add(editorPane);
			services.add(preparation);
			retained = true;
			editorPane.create(container);
			if (disposables.isDisposed || token.isCancellationRequested) {
				throw new CancellationError();
			}
			editorPane.setVisible(this.isVisible());
			if (isEditorPaneWithSelection(editorPane)) {
				panes.add(editorPane.onDidChangeSelection(e => this._onDidChangeSelection.fire(e)));
			}
			return editorPane;
		} finally {
			if (!retained) {
				preparation.dispose();
			}
		}
	}

`, replaceOnce);
  }
  if (path === join(upstream, settingsEditorScopeSourcePaths[3])) {
    contents = replaceOnce(contents, '\tprivate hasIdenticalSides: boolean;', `\tprivate hasIdenticalSides: boolean;
	override get editorPaneScope(): EditorInput | undefined {
		return this.primary.editorPaneScope || this.secondary.editorPaneScope ? this : undefined;
	}`);
    contents = replaceOnce(contents, '\tprivate registerListeners(): void {', `\tprivate registerListeners(): void {
		this._register(Event.any(this.primary.onDidChangeEditorPaneScope, this.secondary.onDidChangeEditorPaneScope)(() => this.invalidateEditorPaneScope()));`);
  }
  return contents;
}
