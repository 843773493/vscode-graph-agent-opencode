import type { DebugOutputRenderer } from '../../../web/src/components/nodeDebug/NodeDebugConsoleView.js';
import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { handleANSIOutput } from '../../upstream/src/vs/workbench/contrib/debug/browser/debugANSIHandling.js';
import { DebugLinkHoverBehavior, LinkDetector } from '../../upstream/src/vs/workbench/contrib/debug/browser/linkDetector.js';
import replStyle from '../../upstream/src/vs/workbench/contrib/debug/browser/media/repl.css?text';
import { registerThemingParticipant } from '../../upstream/src/vs/platform/theme/common/themeService.js';
import { editorBackground } from '../../upstream/src/vs/platform/theme/common/colorRegistry.js';
import { ansiColorIdentifiers } from '../../upstream/src/vs/workbench/contrib/terminal/common/terminalColorRegistry.js';

// 原 REPL 只给侧栏和面板提供 ANSI 色板；会话位于编辑器，沿相同主题色和对比度计算补齐。
registerThemingParticipant((theme, collector) => {
  const background = theme.getColor(editorBackground);
  const colors = ansiColorIdentifiers.flatMap(id => {
    const color = theme.getColor(id);
    return color ? [`--vscode-debug-ansi-${id.replaceAll('.', '-')}:${background ? background.ensureConstrast(color, 4) : color}`] : [];
  });
  collector.addRule(`.monaco-workbench .part.editor .boxteam-react-surface { ${colors.join(';')} }`);
});

/** 原版 REPL 的解析、链接及样式在所属会话的 ShadowRoot 中复用。 */
export function createDebugOutputRenderer(instantiation: IInstantiationService): DebugOutputRenderer {
  const links = instantiation.createInstance(LinkDetector);
  const css = replStyle
    .replaceAll('.monaco-workbench .repl .repl-tree .output.expression', '.node-debug-output')
    .replaceAll('.monaco-workbench .repl', '.node-debug-console-view');
  return (container, text) => {
    const store = new DisposableStore();
    const style = document.createElement('style');
    style.textContent = css;
    container.replaceChildren(style, handleANSIOutput(text, links, undefined, undefined,
      { type: DebugLinkHoverBehavior.Rich, store }));
    return () => { store.dispose(); container.replaceChildren(); };
  };
}
