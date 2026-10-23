export function createRemoteFrameRenderer({ canvas, context, onFrameError }) {
  let latestFrameSerial = 0;
  let pendingFrame = null;
  let frameDecodeInProgress = false;
  const frameImage = new Image();

  function releaseDecodedFrame() {
    frameImage.onload = null;
    frameImage.onerror = null;
    frameImage.removeAttribute("src");
  }

  function releaseFrameAndContinue() {
    frameDecodeInProgress = false;
    try {
      releaseDecodedFrame();
    } finally {
      decodeNextFrame();
    }
  }

  function decodeNextFrame() {
    if (frameDecodeInProgress || !pendingFrame) {
      return;
    }

    const queuedFrame = pendingFrame;
    pendingFrame = null;
    frameDecodeInProgress = true;

    frameImage.onload = () => {
      try {
        if (queuedFrame.serial === latestFrameSerial) {
          if (canvas.width !== queuedFrame.frameMessage.width || canvas.height !== queuedFrame.frameMessage.height) {
            canvas.width = queuedFrame.frameMessage.width;
            canvas.height = queuedFrame.frameMessage.height;
          }
          context.drawImage(frameImage, 0, 0, canvas.width, canvas.height);
        }
      } catch (error) {
        onFrameError(error);
      } finally {
        releaseFrameAndContinue();
      }
    };
    frameImage.onerror = () => {
      try {
        if (queuedFrame.serial === latestFrameSerial) {
          onFrameError(new Error("远程屏幕图像解码失败"));
        }
      } finally {
        releaseFrameAndContinue();
      }
    };
    frameImage.src = queuedFrame.frameMessage.dataUrl;
  }

  return (frameMessage) => {
    pendingFrame = {
      serial: ++latestFrameSerial,
      frameMessage,
    };
    decodeNextFrame();
  };
}
