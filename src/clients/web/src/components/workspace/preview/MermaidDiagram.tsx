import { useEffect, useId, useState } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import { loadMermaid } from "../../../runtime/mermaid";
import { errorMessage } from "../../../utils/errorMessage";

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
          setError(errorMessage(renderError));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [diagramId, source]);

  if (error) {
    return (
      <div className="workspace-markdown-mermaid-error" role="alert">
        <strong>{t("workspace.markdown.mermaid.renderFailed")}</strong>
        <pre>{error}</pre>
      </div>
    );
  }
  if (!svg) {
    return <div className="workspace-markdown-mermaid-loading">{t("workspace.markdown.mermaid.rendering")}</div>;
  }
  return (
    <div
      className="workspace-markdown-mermaid"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
