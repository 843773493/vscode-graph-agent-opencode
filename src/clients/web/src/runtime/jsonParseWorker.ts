interface JsonParseSuccess {
  ok: true;
  value: unknown;
}

interface JsonParseFailure {
  ok: false;
  message: string;
}

type JsonParseResult = JsonParseSuccess | JsonParseFailure;

interface JsonParseWorkerScope {
  onmessage: ((event: MessageEvent<ArrayBuffer>) => void) | null;
  postMessage(message: JsonParseResult): void;
}

const workerScope = self as unknown as JsonParseWorkerScope;

workerScope.onmessage = (event) => {
  try {
    const text = new TextDecoder().decode(event.data);
    workerScope.postMessage({ ok: true, value: JSON.parse(text) as unknown });
  } catch (error) {
    workerScope.postMessage({
      ok: false,
      message: error instanceof Error ? error.message : String(error),
    });
  }
};

export {};
