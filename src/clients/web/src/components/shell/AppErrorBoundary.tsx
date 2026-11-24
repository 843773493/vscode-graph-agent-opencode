import {
  Component,
  type ContextType,
  type ReactNode,
} from "react";
import { t } from "../../../../shared/core/i18n/index.js";
import { errorDiagnostic, errorSummary } from "../../utils/errorMessage";
import { copyTextToClipboard } from "../../utils/clipboard";
import { ComposerDiagnosticsContext } from "../composer/ComposerDiagnosticsContext";

interface AppErrorBoundaryProps {
  children: ReactNode;
  onDiagnostic?: (message: string) => void;
}

interface AppErrorBoundaryState {
  error: Error | null;
  copyResult?: string;
}

export default class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  static contextType = ComposerDiagnosticsContext;
  declare context: ContextType<typeof ComposerDiagnosticsContext>;
  state: AppErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error) {
    const diagnostic = errorDiagnostic(error);
    try { (this.props.onDiagnostic ?? this.context)(diagnostic); }
    catch { console.error(diagnostic); }
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
          <p>{errorSummary(this.state.error)}</p>
          <details>
            <summary>{t("shell.diagnostic")}</summary>
            <pre>{errorDiagnostic(this.state.error)}</pre>
          </details>
          <button type="button" onClick={() => {
            void copyTextToClipboard(errorDiagnostic(this.state.error)).then(
              () => this.setState({ copyResult: t("chat.actions.copied") }),
              error => this.setState({ copyResult: errorSummary(error, t("chat.actions.copyFailed")) }),
            );
          }}>{t("shell.copyDiagnostic")}</button>
          {this.state.copyResult && <p role="status">{this.state.copyResult}</p>}
          <button type="button" onClick={() => window.location.reload()}>
            {t("shell.reload")}
          </button>
        </div>
      </main>
    );
  }
}
