import { createRoot, type Root } from 'react-dom/client';
import type { ReactNode } from 'react';
import { OverlayRootContext } from '../../../web/src/components/overlays/OverlayRootContext.js';
import AppErrorBoundary from '../../../web/src/components/shell/AppErrorBoundary.js';
import baseStyle from './surface.css?text';
import themeStyle from '../../../web/src/styles/foundation/theme.css?text';
import codiconStyle from '../../node_modules/@vscode/codicons/dist/codicon.css?text';

/** 每个编辑器实例拥有自己的 React 根和浮层，业务样式不会进入原版工作台。 */
export class ReactSurface {
  private readonly root: Root;
  private readonly overlay = document.createElement('div');
  private readonly host = document.createElement('div');

  constructor(container: HTMLElement, styles: readonly string[]) {
    this.host.className = 'boxteam-react-surface';
    this.host.style.cssText = 'display:block;position:relative;min-width:0;height:100%';
    // 上游工作台根节点会取消 wheel 默认行为；React 产品区域使用浏览器原生滚动。
    this.host.addEventListener('wheel', event => event.stopPropagation(), { passive: true });
    const shadow = this.host.attachShadow({ mode: 'open' });
    const sheet = new CSSStyleSheet();
    // 字体由原版工作台注册；ShadowRoot 仅复用图标类，避免重载相对字体 URL。
    sheet.replaceSync([themeStyle.replaceAll(':root', ':host'), baseStyle, codiconStyle.slice(codiconStyle.indexOf('.codicon')), ...styles].join('\n'));
    shadow.adoptedStyleSheets = [sheet];
    const mount = document.createElement('div');
    mount.className = 'react-session-content';
    this.overlay.className = 'react-session-overlays';
    shadow.append(mount, this.overlay);
    container.replaceChildren(this.host);
    this.root = createRoot(mount);
  }

  render(children: ReactNode): void {
    this.root.render(<AppErrorBoundary><OverlayRootContext.Provider value={this.overlay}>{children}</OverlayRootContext.Provider></AppErrorBoundary>);
  }

  focus(selector: string): void { this.host.shadowRoot?.querySelector<HTMLElement>(selector)?.focus(); }

  dispose(): void { this.root.unmount(); this.host.remove(); }
}
