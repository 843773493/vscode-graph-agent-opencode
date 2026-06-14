import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AppProvider } from './hooks';
import './index.css';

declare global {
  interface ImportMeta {
    hot?: { accept: () => void };
  }

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
  import.meta.hot.dispose(() => {
    window.__graphAgentRoot = undefined;
  });
}
