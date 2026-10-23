import { describe, expect, test } from "bun:test";
import { createRemoteFrameRenderer } from "../src/client/remoteFrameRenderer.js";

class FakeImage {
  static instances = [];

  constructor() {
    this.currentSource = "";
    this.requestedSources = [];
    this.removedSourceCount = 0;
    this.loadedSource = "";
    FakeImage.instances.push(this);
  }

  get src() {
    return this.currentSource;
  }

  set src(source) {
    this.currentSource = source;
    if (source) {
      this.requestedSources.push(source);
    }
  }

  removeAttribute(attributeName) {
    if (attributeName === "src") {
      this.currentSource = "";
      this.removedSourceCount += 1;
    }
  }

  finishLoad() {
    this.loadedSource = this.currentSource;
    this.onload?.();
  }

  finishError() {
    this.onerror?.();
  }
}

function withFakeImage(callback) {
  const previousImage = globalThis.Image;
  globalThis.Image = FakeImage;
  FakeImage.instances.length = 0;
  try {
    callback();
  } finally {
    if (previousImage === undefined) {
      delete globalThis.Image;
    } else {
      globalThis.Image = previousImage;
    }
  }
}

describe("远程屏幕帧绘制", () => {
  test("解码期间只保留最新帧，并跳过过期帧绘制", () => {
    withFakeImage(() => {
      const canvas = { width: 10, height: 10 };
      const drawCalls = [];
      const renderFrame = createRemoteFrameRenderer({
        canvas,
        context: {
          drawImage(image) {
            drawCalls.push({ source: image.loadedSource, width: canvas.width, height: canvas.height });
          },
        },
        onFrameError: () => {
          throw new Error("测试不期望解码失败");
        },
      });
      const frameImage = FakeImage.instances[0];

      renderFrame({ dataUrl: "frame-1", width: 10, height: 10 });
      renderFrame({ dataUrl: "frame-2", width: 20, height: 20 });
      renderFrame({ dataUrl: "frame-3", width: 30, height: 40 });
      frameImage.finishLoad();

      expect(frameImage.requestedSources).toEqual(["frame-1", "frame-3"]);
      expect(frameImage.removedSourceCount).toBe(1);
      expect(drawCalls).toEqual([]);
      expect(canvas).toEqual({ width: 10, height: 10 });

      frameImage.finishLoad();

      expect(drawCalls).toEqual([{ source: "frame-3", width: 30, height: 40 }]);
      expect(frameImage.removedSourceCount).toBe(2);
      expect(frameImage.src).toBe("");
    });
  });

  test("过期帧解码失败后释放来源并继续渲染最新帧", () => {
    withFakeImage(() => {
      const decodeErrors = [];
      const drawnSources = [];
      const renderFrame = createRemoteFrameRenderer({
        canvas: { width: 10, height: 10 },
        context: {
          drawImage(image) {
            drawnSources.push(image.loadedSource);
          },
        },
        onFrameError: (error) => decodeErrors.push(error.message),
      });
      const frameImage = FakeImage.instances[0];

      renderFrame({ dataUrl: "stale-frame", width: 10, height: 10 });
      renderFrame({ dataUrl: "latest-frame", width: 20, height: 20 });
      frameImage.finishError();

      expect(decodeErrors).toEqual([]);
      expect(frameImage.requestedSources).toEqual(["stale-frame", "latest-frame"]);
      expect(frameImage.removedSourceCount).toBe(1);
      frameImage.finishLoad();

      expect(drawnSources).toEqual(["latest-frame"]);
      expect(frameImage.removedSourceCount).toBe(2);
      expect(frameImage.src).toBe("");
    });
  });

  test("当前帧解码失败时释放图片来源并报告错误", () => {
    withFakeImage(() => {
      let decodeErrors = 0;
      createRemoteFrameRenderer({
        canvas: { width: 10, height: 10 },
        context: { drawImage() {} },
        onFrameError: () => {
          decodeErrors += 1;
        },
      })({ dataUrl: "invalid-frame", width: 10, height: 10 });
      const frameImage = FakeImage.instances[0];

      frameImage.finishError();

      expect(decodeErrors).toBe(1);
      expect(frameImage.removedSourceCount).toBe(1);
      expect(frameImage.src).toBe("");
    });
  });

  test("绘制失败时释放当前图片来源并保留后续帧能力", () => {
    withFakeImage(() => {
      const errors = [];
      const drawnSources = [];
      let failFirstDraw = true;
      const renderFrame = createRemoteFrameRenderer({
        canvas: { width: 10, height: 10 },
        context: {
          drawImage(image) {
            if (failFirstDraw) {
              failFirstDraw = false;
              throw new Error("canvas context lost");
            }
            drawnSources.push(image.loadedSource);
          },
        },
        onFrameError: (error) => errors.push(error.message),
      });
      const frameImage = FakeImage.instances[0];

      renderFrame({ dataUrl: "frame-1", width: 10, height: 10 });
      frameImage.finishLoad();

      expect(errors).toEqual(["canvas context lost"]);
      expect(frameImage.removedSourceCount).toBe(1);
      expect(frameImage.src).toBe("");

      renderFrame({ dataUrl: "frame-2", width: 10, height: 10 });
      frameImage.finishLoad();

      expect(drawnSources).toEqual(["frame-2"]);
      expect(frameImage.removedSourceCount).toBe(2);
    });
  });
});
