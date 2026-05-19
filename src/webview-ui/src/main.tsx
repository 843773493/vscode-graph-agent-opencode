import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AppProvider } from './hooks';
import './index.css';

declare global {
  interface ImportMeta {
    hot?: { accept: () => void };
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppProvider>
      <App />
    </AppProvider>
  </React.StrictMode>
);

if (import.meta.hot) {
  import.meta.hot.accept();
}
