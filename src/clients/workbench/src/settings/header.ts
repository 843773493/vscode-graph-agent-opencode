import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ITargetSettingsContext } from './scope.js';
import { sourceStatus } from './sourceOwner.js';

/** 来源和生效状态只读原 owner 回执，不为标题重复请求。 */
export class SettingsContextHeader extends Disposable {
  constructor(private readonly element: HTMLElement, private readonly layout: () => void,
    @ITargetSettingsContext private readonly context: ITargetSettingsContext) {
    super();
    this.element.className = 'settings-context-label';
    Object.assign(this.element.style, { order: '1', flex: '0 0 100%', fontSize: '12px', lineHeight: '20px', overflowWrap: 'anywhere' });
    this.update();
  }
  update(): void {
    const source = this.context.target;
    const unbound = source?.unboundPluginParameters()?.map(plugin => {
      const identity = `${plugin.packageId}/${plugin.pluginId}`;
      if (plugin.installation === 'declared') return `${identity} 已安装但未绑定，参数未生效`;
      if (plugin.installation === 'unknown') return `${identity} 未绑定，安装声明不可用，参数未生效`;
      return `${identity} 当前目标未见配置声明（可能已卸载），参数未生效`;
    });
    const warning = source && unbound === undefined ? ' · 当前来源 JSONC 无效，无法判定未绑定参数'
      : unbound?.length ? ` · ${unbound.join('；')}；可在来源 JSONC 编辑或删除` : '';
    this.element.textContent = source ? `${source.location.gateway_id} · ${source.location.workspace_id ?? 'Gateway'} · ${sourceStatus(source.document)}${source.document.writable ? '' : ' · 只读'} · 业务设置不支持语言覆盖${warning}` : '当前工作台 profile：主题、布局及窗口设置';
    this.layout();
  }
}
