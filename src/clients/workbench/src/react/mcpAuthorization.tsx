import McpAuthorizationPage from '../../../web/src/components/workspace/mcp/McpAuthorizationPage.js';
import themeStyle from '../../../web/src/styles/foundation/theme.css?text';
import { ReactSurface } from './surface.js';
import { productStyles } from './productStyles.js';

/** 沿用聊天链接的独立授权入口，不启动没有编辑器的 Workbench 服务。 */
export function startMcpAuthorization(workspaceId: string): void {
  const container = document.getElementById('workbench');
  if (!container) throw new Error('缺少 MCP 授权页容器');
  document.title = '工作区 MCP 连接与授权';
  const surface = new ReactSurface(container, [
    ...productStyles,
    // 独立页面没有原版工作台的主题变量，使用原产品主题。
    themeStyle.replaceAll(':root', ':host'),
    ':host{font:13px system-ui;color:var(--bt-text-primary);background:var(--bt-page-background)}main{height:100%;overflow:auto;padding:16px}',
  ]);
  surface.render(<McpAuthorizationPage workspaceId={workspaceId} />);
}
