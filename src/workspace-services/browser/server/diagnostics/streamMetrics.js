/**
 * 当前页面帧在 Node 侧的观察样本，每帧只记一次，不按客户端数量放大。
 * @typedef {object} BrowserFrameObservation
 * @property {number} observedAtMonotonicMs 本进程performance.now向下取整的单调毫秒；只计算窗口与间隔，不持久化。
 * @property {number} jpegPayloadBytes JPEG 正文字节数，不含协议、WebSocket 或网络开销。
 */

/**
 * 从显式观察事实派生流统计，不持有Session、连接、计时器或第二份样本窗口。
 * fps/bitrate_bps是Node观察频率和JPEG正文速率；delivery按连接汇总发送/ACK事实。
 * @param {object} input
 * @param {readonly BrowserFrameObservation[]} input.observations
 * @param {ReadonlyArray<ReturnType<import("../transport/browserFrameFlow.js").BrowserFrameFlow["snapshot"]>>} input.deliveryClients 单连接快照，不是连接句柄。
 * @param {string|null} input.profile
 * @param {number|null|undefined} input.lastAttachFirstFrameMs attach开始到下一帧观察的时差。
 */
export function summarizeBrowserStreamMetrics({ observations, deliveryClients, profile, lastAttachFirstFrameMs }) {
  const delivery = {
    client_count: deliveryClients.length,
    frames_sent: deliveryClients.reduce((total, item) => total + item.frames_sent, 0),
    frames_superseded: deliveryClients.reduce(
      (total, item) => total + item.frames_superseded,
      0,
    ),
    ack_timeouts: deliveryClients.reduce((total, item) => total + item.ack_timeouts, 0),
    max_ack_rtt_ms: deliveryClients.reduce(
      (maximum, item) => Math.max(maximum, item.max_ack_rtt_ms),
      0,
    ),
    clients: deliveryClients,
  };
  if (observations.length < 2) {
    return {
      profile,
      fps: 0,
      bitrate_bps: 0,
      sample_frames: observations.length,
      last_attach_first_frame_ms: lastAttachFirstFrameMs ?? null,
      delivery,
    };
  }
  const observationSpanSeconds = Math.max(
    0.001,
    (observations.at(-1).observedAtMonotonicMs - observations[0].observedAtMonotonicMs) / 1_000,
  );
  const totalJpegPayloadBytes = observations.reduce((total, sample) => total + sample.jpegPayloadBytes, 0);
  return {
    profile,
    fps: Number(((observations.length - 1) / observationSpanSeconds).toFixed(2)),
    bitrate_bps: Math.round((totalJpegPayloadBytes * 8) / observationSpanSeconds),
    sample_frames: observations.length,
    last_attach_first_frame_ms: lastAttachFirstFrameMs ?? null,
    delivery,
  };
}
