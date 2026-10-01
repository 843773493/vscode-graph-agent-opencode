import React from "react";
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useComposerDraft } from "./useComposerDraft";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");

function installStorage(): Map<string, string> {
  const storage = new Map<string, string>();
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => {
          storage.set(key, value);
        },
        removeItem: (key: string) => {
          storage.delete(key);
        },
      },
    },
  });
  return storage;
}

afterEach(() => {
  restoreGlobalDescriptor("window", originalWindow);
});

describe("Composer 草稿超长输入", () => {
  test("超出存储上限的输入不得让界面崩溃，只做有界持久化", async () => {
    const storage = installStorage();
    let latest = "";
    let setDraft: React.Dispatch<React.SetStateAction<string>> | null = null;
    function Harness(): React.ReactElement {
      const [draft, update] = useComposerDraft("workspace", "session");
      latest = draft;
      setDraft = update;
      return <span>{draft.length}</span>;
    }
    let renderer: ReactTestRenderer | undefined;
    act(() => {
      renderer = create(<Harness />);
    });

    const huge = "x".repeat(100_001);
    await act(async () => {
      setDraft?.(huge);
    });

    expect(latest.length).toBe(100_001);
    expect(storage.get("boxteam.web.composerDrafts")).toBeUndefined();
    renderer?.unmount();
  });
});
