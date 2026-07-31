import type { Mermaid } from "mermaid";

let mermaidPromise: Promise<Mermaid> | null = null;

export function loadMermaid(): Promise<Mermaid> {
  if (mermaidPromise) {
    return mermaidPromise;
  }
  mermaidPromise = import("mermaid").then(({ default: mermaid }) => {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      flowchart: { htmlLabels: false },
    });
    return mermaid;
  });
  return mermaidPromise;
}
