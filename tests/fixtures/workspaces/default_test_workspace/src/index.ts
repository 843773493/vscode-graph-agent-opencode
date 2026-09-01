import { buildContextSummary, explainWorkspace } from './context';
import { formatEditSummary, normalizeHeadline } from './editor';
import { collectWorkspaceHints } from './files';
import { createDemoState } from './state';

const state = createDemoState();
const summary = buildContextSummary(collectWorkspaceHints());
const headline = normalizeHeadline(explainWorkspace(state.title), false);

console.log(formatEditSummary(headline));
console.log(summary);
console.log(state.retries);
