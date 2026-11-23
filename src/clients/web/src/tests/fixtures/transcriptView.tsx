import React from "react";
import { TranscriptViewContext, useTranscriptView } from "../../components/chat/TranscriptViewContext";

/** 使用产品的唯一 owner，组件测试不再依赖各行的局部折叠状态。 */
export function TranscriptTestView({children}: {children: React.ReactNode}) {
  const {transcriptView} = useTranscriptView("test-thread");
  return <TranscriptViewContext.Provider value={transcriptView}>{children}</TranscriptViewContext.Provider>;
}
