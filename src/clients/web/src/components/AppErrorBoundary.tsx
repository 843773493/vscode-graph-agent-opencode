import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";

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
          <h1>页面加载失败</h1>
          <p>前端组件发生异常，当前页面没有继续渲染。</p>
          <pre>{this.state.error.message}</pre>
          <button type="button" onClick={() => window.location.reload()}>
            刷新页面
          </button>
        </div>
      </main>
    );
  }
}
