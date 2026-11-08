import { getErrorMessage } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { isEqual } from '../../upstream/src/vs/base/common/resources.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { ConfigurationTarget } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { IFileService, type IFileStatWithMetadata } from '../../upstream/src/vs/platform/files/common/files.js';
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import type { SettingsTarget } from '../../upstream/src/vs/workbench/contrib/preferences/browser/preferencesWidgets.js';
import { IFilesConfigurationService } from '../../upstream/src/vs/workbench/services/filesConfiguration/common/filesConfigurationService.js';
import { IPreferencesService } from '../../upstream/src/vs/workbench/services/preferences/common/preferences.js';
import { ITextFileService } from '../../upstream/src/vs/workbench/services/textfile/common/textfiles.js';
import { ITargetSettingsContext } from './scope.js';

/** 只投影当前输入的归属与原服务已取得的文件状态，不探测或接管保存。 */
export class SettingsContextHeader extends Disposable {
  private source: SettingsTarget | null | undefined;
  private language: string | undefined;
  private resource: URI | undefined;
  private stat: IFileStatWithMetadata | undefined;
  private resourceError: string | undefined;
  private version = 0;
  private height = 0;

  constructor(private readonly element: HTMLElement, private readonly onDidRender: () => void,
    @ITargetSettingsContext private readonly context: ITargetSettingsContext,
    @IPreferencesService private readonly preferences: IPreferencesService,
    @IWorkspaceContextService private readonly workspace: IWorkspaceContextService,
    @IFileService files: IFileService,
    @IFilesConfigurationService private readonly fileConfiguration: IFilesConfigurationService,
    @ITextFileService private readonly textFiles: ITextFileService,
    @ILogService private readonly log: ILogService) {
    super();
    this.element.className = 'settings-context-label';
    Object.assign(this.element.style, { order: '1', flex: '0 0 100%', fontSize: '12px', lineHeight: '20px', overflowWrap: 'anywhere', paddingBottom: '4px' });
    this._register(fileConfiguration.onDidChangeReadonly(() => this.render()));
    this._register(files.onDidChangeFileSystemProviderCapabilities(() => this.render()));
    this._register(files.onDidChangeFileSystemProviderRegistrations(() => this.render()));
    this._register(textFiles.files.onDidResolve(({ model }) => {
      if (isEqual(this.resource, model.resource)) { this.stat = undefined; this.render(); }
    }));
    this._register(textFiles.files.onDidChangeReadonly(model => {
      if (isEqual(this.resource, model.resource)) { this.stat = undefined; this.render(); }
    }));
    this._register(textFiles.files.onDidRemove(resource => { if (isEqual(this.resource, resource)) this.render(); }));
    this._register(files.onDidRunOperation(event => {
      if (event.target && isEqual(this.resource, event.target.resource)) { this.stat = event.target; this.render(); }
    }));
    this._register(textFiles.files.onDidSave(event => {
      if (isEqual(this.resource, event.model.resource)) { this.stat = event.stat; this.render(); }
    }));
    this._register(files.onDidFilesChange(event => {
      if (this.resource && event.contains(this.resource)) { this.stat = undefined; this.render(); }
    }));
  }

  update(source: SettingsTarget | null, language?: string): void {
    this.language = language;
    if (source === this.source || URI.isUri(source) && URI.isUri(this.source) && isEqual(source, this.source)) {
      this.render();
      return;
    }
    this.source = source;
    this.resource = undefined;
    this.stat = undefined;
    this.resourceError = undefined;
    const version = ++this.version;
    this.render();
    if (source === null) return;
    void this.preferences.getEditableSettingsURI(URI.isUri(source) ? ConfigurationTarget.WORKSPACE_FOLDER : source,
      URI.isUri(source) ? source : undefined).then(resource => {
      if (version !== this.version || this._store.isDisposed) return;
      this.resource = resource ?? undefined;
      this.render();
    }, error => {
      if (version !== this.version || this._store.isDisposed) return;
      this.resourceError = getErrorMessage(error);
      this.log.error(error);
      this.render();
    });
  }

  private render(): void {
    if (this._store.isDisposed) return;
    const target = this.context.target, source = this.source;
    const folder = URI.isUri(source) ? this.workspace.getWorkspaceFolder(source) : undefined;
    const scope = source === ConfigurationTarget.APPLICATION ? '当前工作台应用'
      : source === ConfigurationTarget.USER_LOCAL ? '当前工作台用户'
        : source === ConfigurationTarget.USER_REMOTE ? target ? '目标用户' : '远程用户'
          : source === ConfigurationTarget.WORKSPACE ? '工作区 · ' + this.workspace.getWorkspace().name
            : URI.isUri(source) ? '文件夹 · ' + (folder?.name ?? source.toString()) : '尚未选择';
    const configuration = target?.configuration;
    const targetFile = !!configuration && (source === ConfigurationTarget.USER_REMOTE
      || source === ConfigurationTarget.WORKSPACE && !!this.workspace.getWorkspace().configuration);
    if (targetFile && configuration.kind !== 'settings') throw new Error('设置授权展示缺少原配置准备回执');
    // TODO：普通工作区文件 owner 尚未公开授权与 permissions 快照；补正式能力后消费，不从写入支持推定授权。
    const authorization = targetFile && configuration.kind === 'settings'
      ? (configuration.connection.configuration_write_allowed ? '已授权' : '未授权') + '（打开时）'
      : source === ConfigurationTarget.APPLICATION || source === ConfigurationTarget.USER_LOCAL ? '当前工作台管理'
        : '保存时检查';
    const model = this.resource && this.textFiles.files.get(this.resource);
    const readonly = this.resource && (model?.isReadonly() || this.fileConfiguration.isReadonly(this.resource, this.stat));
    const fileState = this.resourceError ? '不可用：' + this.resourceError
      : readonly ? '只读' + (this.stat?.locked ? '、锁定' : '') : this.stat?.locked ? '锁定' : '保存时检查';
    const origin = target ? target.location.workspace_ids.length ? target.location.workspace_ids.length + ' 个工作区' : '未关联工作区' : '当前工作台';
    this.element.textContent = '范围：' + scope + (this.language ? ' · 语言 ' + this.language : '')
      + ' · 写入授权：' + authorization + ' · 文件：' + fileState + ' · 打开上下文：' + origin;
    this.element.title = (target ? 'Gateway ' + target.location.gateway_id + '\nBackend ' + target.location.backend_id
      + '\nprofile ' + target.location.profile_id + '\nview ' + target.location.view_id + '\n工作区（打开时的顺序）：'
      + (target.location.workspace_ids.length ? '\n' + target.location.workspace_ids.map((id, index) => (index + 1) + '. ' + id).join('\n') : '无')
      : '打开上下文：当前工作台') + '\n范围：' + scope + (this.resource ? '\n' + this.resource.toString() : '')
      + (typeof readonly === 'object' ? '\n' + readonly.value : '')
      + '\n写入授权在打开时确认，保存时会重新校验。未确认的文件权限将在保存时检查。';
    const height = this.element.offsetHeight;
    if (height !== this.height) { this.height = height; this.onDidRender(); }
  }
}
