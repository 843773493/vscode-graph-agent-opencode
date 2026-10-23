import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import AppErrorBoundary from './components/shell/AppErrorBoundary';
import { AppProvider } from './hooks';
import WarmConfirmProvider from './components/shell/WarmConfirmProvider';
import '@vscode/codicons/dist/codicon.css';
import { installBoxTeamThemeRuntime } from './theme/theme';
import './styles/foundation/theme.css';
import './index.css';
import './styles/foundation/panelShared.css';
import './styles/session/agentState.css';
import './styles/session/eventQueue.css';
import './styles/session/requestLog.css';
import './styles/workspace/resourcePanel.css';
import './styles/session/childThreadPanel.css';
import './styles/workspace/workspace.css';
import './styles/session/agentSessionsPanel.css';
import './styles/workspace/sessionResourceExplorer.css';
import './styles/foundation/workbenchLayout.css';
import './styles/session/toolControl.css';
import './styles/session/chatMessages.css';
import './styles/workspace/gatewayConsole.css';
import './styles/foundation/workbenchPanel.css';
import './styles/workspace/portForwardPanel.css';
import './styles/inspection/debugPanel.css';
import './styles/foundation/themeSurfaces.css';
import './styles/workspace/gatewayUserAccess.css';

declare global {
  interface Window {
    __graphAgentRoot?: ReactDOM.Root;
  }
}

const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error('找不到前端挂载节点 #root');
}

installBoxTeamThemeRuntime();

const root = window.__graphAgentRoot ?? ReactDOM.createRoot(rootElement);
window.__graphAgentRoot = root;

root.render(
  <React.StrictMode>
    <AppErrorBoundary>
      <AppProvider>
        <WarmConfirmProvider>
          <App />
        </WarmConfirmProvider>
      </AppProvider>
    </AppErrorBoundary>
  </React.StrictMode>,
);

if (import.meta.hot) {
  import.meta.hot.accept();
}
