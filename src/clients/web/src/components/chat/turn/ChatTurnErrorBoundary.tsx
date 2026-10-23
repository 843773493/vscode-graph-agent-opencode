import React from "react";

type Props = {
  conversationId: string;
  children: React.ReactNode;
};

type State = {
  error: Error | null;
};

export default class ChatTurnErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    console.error(
      `会话轮次渲染失败 conversation_id=${this.props.conversationId}`,
      error,
      info,
    );
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
            此轮消息无法显示：{this.state.error.message}
          </span>
        </div>
      </article>
    );
  }
}
