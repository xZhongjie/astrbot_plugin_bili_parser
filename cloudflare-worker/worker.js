/**
 * B 站视频/图片转发 Worker（Cloudflare Workers）
 *
 * 用途：当 AstrBot 所在服务器无法直接下载 B 站 CDN 资源时，
 * 把下载请求交给这个 Worker，由它去 B 站取数据并以流的方式返回。
 *
 * 部署：
 * 1. 打开 https://dash.cloudflare.com/ → Workers & Pages → Create → Worker
 * 2. 把本文件内容整个粘贴进去，Deploy
 * 3. 在插件配置里填：
 *      video_proxy: https://<你的worker>.workers.dev/?url={url}
 *      cover_proxy: https://<你的worker>.workers.dev/?url={url}
 *
 * 注意：
 * - Worker 的出口 IP 不在中国大陆，B 站 CDN 对部分海外 IP 也可能只返回
 *   初始化分片；如果出现这种情况，请改用大陆 VPS 上的 nginx/HTTP 代理。
 * - 只允许 B 站相关域名，避免被当成任意 URL 代理（SSRF）。
 */
export default {
  async fetch(request, env, ctx) {
    const reqUrl = new URL(request.url);
    const target = reqUrl.searchParams.get("url");
    if (!target) {
      return new Response("missing ?url=", { status: 400 });
    }

    let parsed;
    try {
      parsed = new URL(target);
    } catch (e) {
      return new Response("bad url", { status: 400 });
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return new Response("bad protocol", { status: 400 });
    }

    const host = parsed.hostname.toLowerCase();
    const allowed = [
      /\.bilivideo\.com$/,
      /\.hdslb\.com$/,
      /\.akamaized\.net$/,
      /(^|\.)bilibili\.com$/,
      /(^|\.)bili22\.cn$/,
      /(^|\.)bili23\.cn$/,
      /(^|\.)bili33\.cn$/,
    ];
    if (!allowed.some((re) => re.test(host))) {
      return new Response("host not allowed", { status: 403 });
    }

    const headers = new Headers();
    headers.set(
      "User-Agent",
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    );
    headers.set("Referer", "https://www.bilibili.com/");
    headers.set("Accept", "*/*");

    const range = request.headers.get("range");
    if (range) {
      headers.set("Range", range);
    }

    let upstream;
    try {
      upstream = await fetch(parsed.toString(), {
        method: "GET",
        headers,
        redirect: "follow",
      });
    } catch (e) {
      return new Response("upstream fetch failed: " + e.message, { status: 502 });
    }

    const respHeaders = new Headers();
    for (const name of [
      "content-type",
      "content-length",
      "content-range",
      "accept-ranges",
      "etag",
      "last-modified",
    ]) {
      const value = upstream.headers.get(name);
      if (value) {
        respHeaders.set(name, value);
      }
    }
    respHeaders.set("Access-Control-Allow-Origin", "*");
    respHeaders.set("Cache-Control", "public, max-age=1800");

    return new Response(upstream.body, {
      status: upstream.status,
      headers: respHeaders,
    });
  },
};
