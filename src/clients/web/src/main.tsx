import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import AppErrorBoundary from './components/AppErrorBoundary';
import { AppProvider } from './hooks';
import WarmConfirmProvider from './components/WarmConfirmProvider';
import '@vscode/codicons/dist/codicon.css';
import { installBoxTeamThemeRuntime } from './theme';
import './styles/theme.css';
import './index.css';
import './styles/panelShared.css';
import './styles/agentState.css';
import './styles/eventQueue.css';
import './styles/requestLog.css';
import './styles/resourcePanel.css';
import './styles/workspace.css';
import './styles/agentSessionsPanel.css';
import './styles/sessionResourceExplorer.css';
import './styles/workbenchLayout.css';
import './styles/toolControl.css';
import './styles/chatMessages.css';
import './styles/gatewayConsole.css';
import './styles/workbenchPanel.css';
import './styles/portForwardPanel.css';
import './styles/debugPanel.css';
import './styles/themeSurfaces.css';
import './styles/gatewayUserAccess.css';

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
