import { useEffect, useId, useState } from "react";
import { loadMermaid } from "../../../runtime/mermaid";

export default function MermaidDiagram({ source }: { source: string }) {
  const reactId = useId();
  const diagramId = `workspace-mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSvg(null);
    setError(null);
    void loadMermaid()
      .then((mermaid) => mermaid.render(diagramId, source))
      .then((result) => {
        if (!cancelled) {
          setSvg(result.svg);
        }
      })
      .catch((renderError: unknown) => {
        if (!cancelled) {
          setError(renderError instanceof Error ? renderError.message : String(renderError));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [diagramId, source]);

  if (error) {
    return (
      <div className="workspace-markdown-mermaid-error" role="alert">
        <strong>Mermaid 渲染失败</strong>
        <pre>{error}</pre>
      </div>
    );
  }
  if (!svg) {
    return <div className="workspace-markdown-mermaid-loading">正在渲染 Mermaid 图表…</div>;
  }
  return (
    <div
      className="workspace-markdown-mermaid"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
