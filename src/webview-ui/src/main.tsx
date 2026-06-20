import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AppProvider } from './hooks';
import './index.css';

declare global {
  interface ImportMeta {
    hot?: { accept: () => void; dispose: (fn: () => void) => void };
  }
}

const rootElement = document.getElementById('root')!;

// 防止 Vite HMR 时重复调用 createRoot 导致 React 报错
let root: ReactDOM.Root | null = (rootElement as unknown as { _reactRoot?: ReactDOM.Root })._reactRoot ?? null;
if (!root) {
  root = ReactDOM.createRoot(rootElement);
  (rootElement as unknown as { _reactRoot?: ReactDOM.Root })._reactRoot = root;
}

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
    root?.unmount();
    (rootElement as unknown as { _reactRoot?: ReactDOM.Root })._reactRoot = undefined;
  });
}
