/**
 * 在途请求登记的唯一实现：把请求登记进 Map，并在结算（成功或失败）后仅当
 * 自己仍是最新登记者时才注销，避免被顶替的旧请求把新请求的登记清掉。
 *
 * 此前这一样板在多个 hook 里逐字重复了八行；任何一处漏写失败分支或漏写
 * 身份判定，都会造成「请求已结束但仍被判定为在途」而永久短路后续读取。
 */
export function trackInFlightRequest<K, V>(
  requests: Map<K, Promise<V>>,
  key: K,
  request: Promise<V>,
): void {
  requests.set(key, request);
  const release = () => {
    if (requests.get(key) === request) {
      requests.delete(key);
    }
  };
  void request.then(release, release);
}
