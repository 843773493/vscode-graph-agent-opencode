import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AppProvider } from './hooks';
import './index.css';
import { initializePreviewState } from './previewState';

declare global {
  interface ImportMeta {
    hot?: { accept: () => void; dispose: (fn: () => void) => void };
  }
}

const previewState = initializePreviewState();
void previewState;

const rootElement = document.getElementById('root')!;
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