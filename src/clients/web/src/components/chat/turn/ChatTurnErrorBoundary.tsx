import { t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import { errorDiagnostic, errorSummary } from "../../../utils/errorMessage";
import { failureSummary } from "../../../../../shared/core/failurePresentation";
import { ComposerDiagnosticsContext } from "../../composer/ComposerDiagnosticsContext";

type Props = {
  conversationId: string;
  children: React.ReactNode;
};

type State = {
  error: Error | null;
};

export default class ChatTurnErrorBoundary extends React.Component<Props, State> {
  static contextType = ComposerDiagnosticsContext;
  declare context: React.ContextType<typeof ComposerDiagnosticsContext>;
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error): void {
    const diagnostic = `conversation_id=${this.props.conversationId}\n${errorDiagnostic(error)}`;
    try { this.context(diagnostic); }
    catch { console.error(diagnostic); }
  }

  componentDidUpdate(previousProps: Props): void {
    if (
      this.state.error &&
      previousProps.conversationId !== this.props.conversationId
    ) {
      this.setState({ error: null });
    }
  }

  render(): React.ReactNode {
    if (!this.state.error) {
      return this.props.children;
    }
    return (
      <article className="chat-turn chat-turn-error" role="alert">
        <div className="chat-inline-error">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>
            {failureSummary(t("chat.turn.renderFailed", errorSummary(this.state.error)))}
          </span>
        </div>
      </article>
    );
  }
}
