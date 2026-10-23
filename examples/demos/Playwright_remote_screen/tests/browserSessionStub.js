import { BrowserSession } from "../src/server/browserSession.js";

export function createBrowserSessionStub(
  cdpSession,
  { pageTitle = "fixture page", pageUrl = "https://example.test/" } = {},
) {
  const session = new BrowserSession();
  session.browser = {};
  session.context = {};
  let currentPageUrl = pageUrl;
  session.page = {
    title: async () => pageTitle,
    isClosed: () => false,
    url: () => currentPageUrl,
    goto: async (url) => {
      currentPageUrl = url;
    },
  };
  session.cdpSession = cdpSession;
  return session;
}
