import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import type { AttachmentRef } from "../../../../src/clients/web/src/types/backend";
import MessageAttachments from "../../../../src/clients/web/src/components/chat/MessageAttachments";
import WorkspaceAttachmentPreview from "../../../../src/clients/web/src/components/workspace/preview/WorkspaceAttachmentPreview";
import WorkspaceAuxiliaryPanel from "../../../../src/clients/web/src/components/workspace/WorkspaceAuxiliaryPanel";

const attachments: AttachmentRef[] = [
  {
    file_id: "boxteam-session://session-test/attachments/valid.png",
    name: "valid.png",
    content_type: "image/png",
  },
  {
    file_id: "boxteam-session://session-test/attachments/broken.png",
    name: "broken.png",
    content_type: "image/png",
  },
  {
    file_id: "boxteam-session://session-test/attachments/report.pdf",
    name: "report.pdf",
    content_type: "application/pdf",
  },
  {
    file_id: "boxteam-session://session-test/attachments/notes.txt",
    name: "notes.txt",
    content_type: "text/plain",
  },
];

function StructuredUserMessageAttachmentsHarness(): React.ReactNode {
  const [selected, setSelected] = useState<AttachmentRef | null>(null);

  return (
    <main style={{ display: "flex", minHeight: "100vh", gap: "24px", padding: "24px" }}>
      <section
        data-testid="message-surface"
        style={{ width: "420px", minHeight: "400px", padding: "16px" }}
      >
        <h1>请检查这些附件</h1>
        <p>正文只展示用户输入和附件摘要。</p>
        <MessageAttachments
          attachments={attachments}
          apiPort={8014}
          sessionId="session-test"
          workspaceId="workspace-test"
          onOpenAttachment={setSelected}
        />
      </section>
      <WorkspaceAuxiliaryPanel
        visible
        flexRatio={1}
        tab="files"
        apiPort={8014}
        workspaceId="workspace-test"
        workspaceFileTreeReady={false}
        workspaceName="附件集成测试工作区"
        workspaceRoot="/workspace"
        sessionId="session-test"
        sessionTitle="结构化附件测试会话"
        activeFilePath={null}
        sessionChangesets={[]}
        selectedChangesetId={null}
        activeChangeset={null}
        sessionChangesLoading={false}
        sessionChangesError={null}
        sessionChangesLoadedAt={null}
        searchOpen={false}
        collapseVersion={0}
        expandedFileTreePaths={[]}
        onExpandedFileTreePathsChange={() => undefined}
        attachmentPreview={selected ? (
          <WorkspaceAttachmentPreview
            attachment={selected}
            apiPort={8014}
            sessionId="session-test"
            workspaceId="workspace-test"
          />
        ) : null}
        resourcePanel={null}
        runtimePreview={null}
        debugPanel={null}
        onToggleSearch={() => undefined}
        onCollapseAll={() => undefined}
        onSelectSessionChangeset={() => undefined}
        onRefreshSessionChanges={() => undefined}
        onOpenSessionChangeFile={() => undefined}
        onReviewSessionChangeFile={async () => undefined}
        onOpenFile={() => undefined}
        onStatusChange={() => undefined}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StructuredUserMessageAttachmentsHarness />,
);
