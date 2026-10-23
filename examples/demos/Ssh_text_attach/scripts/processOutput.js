const MAX_LOG_SEGMENT_CODE_UNITS = 8 * 1024;
const LOG_OUTPUT_BATCH_FLUSH_THRESHOLD_CODE_UNITS = 32 * 1024;

export function pipePrefixedOutput(sourceStream, processLabel, outputStream) {
  let pendingText = "";
  let sourceEnded = false;
  let waitingForDrain = false;
  sourceStream.setEncoding("utf8");

  function takeLogSegment() {
    const newlineIndex = pendingText.indexOf("\n");
    if (newlineIndex === 0) {
      pendingText = pendingText.slice(1);
      return "";
    }

    let segmentEnd;
    let lineEnded = false;
    if (newlineIndex > 0 && newlineIndex <= MAX_LOG_SEGMENT_CODE_UNITS) {
      segmentEnd = newlineIndex;
      lineEnded = true;
    } else if (pendingText.length >= MAX_LOG_SEGMENT_CODE_UNITS) {
      segmentEnd = MAX_LOG_SEGMENT_CODE_UNITS;
    } else if (sourceEnded && pendingText.length > 0) {
      segmentEnd = pendingText.length;
      lineEnded = true;
    } else {
      return null;
    }

    const lastCodeUnit = pendingText.charCodeAt(segmentEnd - 1);
    const nextCodeUnit = pendingText.charCodeAt(segmentEnd);
    if (lastCodeUnit >= 0xD800 && lastCodeUnit <= 0xDBFF
      && nextCodeUnit >= 0xDC00 && nextCodeUnit <= 0xDFFF) {
      segmentEnd -= 1;
      lineEnded = false;
    }

    const segment = pendingText.slice(0, segmentEnd);
    pendingText = pendingText.slice(segmentEnd + (lineEnded ? 1 : 0));
    return segment;
  }

  function writeLogBatch(batch) {
    if (batch.length === 0 || outputStream.write(batch)) {
      return true;
    }
    waitingForDrain = true;
    sourceStream.pause();
    outputStream.once("drain", () => {
      waitingForDrain = false;
      processPendingSegments();
      if (!waitingForDrain) {
        sourceStream.resume();
      }
    });
    return false;
  }

  function processPendingSegments() {
    if (waitingForDrain) {
      return;
    }
    let outputBatch = "";
    while (true) {
      if (outputBatch.length >= LOG_OUTPUT_BATCH_FLUSH_THRESHOLD_CODE_UNITS) {
        if (!writeLogBatch(outputBatch)) {
          return;
        }
        outputBatch = "";
      }
      const segment = takeLogSegment();
      if (segment === null) {
        writeLogBatch(outputBatch);
        return;
      }
      outputBatch += `[${processLabel}] ${segment}\n`;
    }
  }

  sourceStream.on("data", (chunk) => {
    pendingText += chunk;
    processPendingSegments();
  });
  sourceStream.on("end", () => {
    sourceEnded = true;
    processPendingSegments();
  });
}
