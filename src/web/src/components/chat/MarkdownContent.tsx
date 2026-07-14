import React from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import {
  isLikelyWorkspaceFileReference,
  remarkWorkspaceFileReferences,
} from "../../utils/workspaceFileReferences";
import WorkspaceFileReferenceLink from "./WorkspaceFileReferenceLink";

function isExternalHref(href: string): boolean {
  return /^(https?:|mailto:|tel:)/i.test(href);
}

const REMARK_PLUGINS = [remarkGfm, remarkWorkspaceFileReferences];
const REHYPE_PLUGINS = [rehypeSanitize];

export default function MarkdownContent({
  value,
  className = "",
}: {
  value: string;
  className?: string;
}): React.ReactNode {
  return (
    <div className={`chat-markdown ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={{
          a: ({ children, href }) => {
            if (href && isExternalHref(href)) {
              return (
                <a href={href} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              );
            }
            return href ? (
              <WorkspaceFileReferenceLink target={href}>
                {children}
              </WorkspaceFileReferenceLink>
            ) : (
              <span>{children}</span>
            );
          },
          code: ({ children, className }) => {
            const value = String(children).replace(/\n$/, "");
            if (className || String(children).endsWith("\n")) {
              return <code className={className}>{children}</code>;
            }
            if (!isLikelyWorkspaceFileReference(value)) {
              return <code>{children}</code>;
            }
            return (
              <WorkspaceFileReferenceLink target={value} inlineCode>
                {children}
              </WorkspaceFileReferenceLink>
            );
          },
        }}
      >
        {value}
      </ReactMarkdown>
    </div>
  );
}
