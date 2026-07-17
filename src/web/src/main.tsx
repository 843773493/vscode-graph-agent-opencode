import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AppProvider } from './hooks';
import '@vscode/codicons/dist/codicon.css';
import './index.css';
import './styles/panelShared.css';
import './styles/agentState.css';
import './styles/eventQueue.css';
import './styles/requestLog.css';
import './styles/resourcePanel.css';
import './styles/workspace.css';
import './styles/agentSessionsPanel.css';
import './styles/workbenchLayout.css';
import './styles/toolControl.css';
import './styles/chatMessages.css';
import './styles/gatewayConsole.css';

declare global {
  interface Window {
    __graphAgentRoot?: ReactDOM.Root;
  }
}

const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error('找不到前端挂载节点 #root');
}

const root = window.__graphAgentRoot ?? ReactDOM.createRoot(rootElement);
window.__graphAgentRoot = root;

root.render(
  <React.StrictMode>
    <AppProvider>
      <App />
    </AppProvider>
  </React.StrictMode>,
);

if (import.meta.hot) {
  import.meta.hot.accept();
}
