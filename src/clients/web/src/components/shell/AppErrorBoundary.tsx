import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";
import { t } from "../../../../shared/core/i18n/index.js";

interface AppErrorBoundaryProps {
  children: ReactNode;
}

interface AppErrorBoundaryState {
  error: Error | null;
}

export default class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  state: AppErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("BoxTeam 前端渲染失败", error, errorInfo);
  }

  render() {
    if (!this.state.error) {
      return this.props.children;
    }

    return (
      <main className="app-fatal-error" role="alert">
        <div className="app-fatal-error-card">
          <h1>{t("shell.renderFailed")}</h1>
          <p>{t("shell.renderFailedDetail")}</p>
          <pre>{this.state.error.message}</pre>
          <button type="button" onClick={() => window.location.reload()}>
            {t("shell.reload")}
          </button>
        </div>
      </main>
    );
  }
}
