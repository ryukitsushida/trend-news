// Trend News Service Worker
// サイトはGitHub Pagesのサブパスで配信されるため、このファイルの登録元パスからの
// 相対URLだけを扱う(絶対パス "/xxx" は使わない)。

const CACHE_VERSION = "v2";
const CACHE_NAME = "trend-news-" + CACHE_VERSION;

const APP_SHELL = [
  "./",
  "index.html",
  "archive/index.html",
  "static/style.css",
  "manifest.webmanifest",
  "icon-192.png",
  "icon-512.png",
];

// respondWith に undefined を渡すと TypeError になるため、
// オフラインで取得できなかった場合は必ずこのレスポンスを返す。
function offlineResponse() {
  return new Response("", {
    status: 504,
    statusText: "Offline and not cached",
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}

function isCacheable(res) {
  // エラーページやクロスオリジンのopaqueレスポンスをキャッシュしない
  return res && res.ok && res.type === "basic";
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      // 1つでも欠けるとaddAll全体が失敗しSWが有効化されないため、個別に投入する
      await Promise.allSettled(APP_SHELL.map((url) => cache.add(url)));
      await self.skipWaiting();
    })
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  // 同一オリジンのみ扱う(記事への外部リンク等はSWを通さず素通しする)
  if (new URL(req.url).origin !== self.location.origin) return;

  // ページ遷移(HTML): まずネットワークで最新を取りに行き、駄目ならキャッシュ、
  // それも無ければトップページを返す(通勤中に圏外でも何かしら読める)
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (isCacheable(res)) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(async () => {
          return (
            (await caches.match(req)) ||
            (await caches.match("index.html")) ||
            offlineResponse()
          );
        })
    );
    return;
  }

  // 静的アセット: キャッシュ優先 + バックグラウンド更新(stale-while-revalidate)
  event.respondWith(
    caches.match(req).then((cached) => {
      const fromNetwork = fetch(req)
        .then((res) => {
          if (isCacheable(res)) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          }
          return res;
        })
        .catch(() => cached || offlineResponse());
      return cached || fromNetwork;
    })
  );
});
