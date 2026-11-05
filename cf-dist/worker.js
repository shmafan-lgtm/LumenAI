// Lumen 受控权重分发 Worker（免费方案：Cloudflare R2 + Workers）
// 只暴露两个带令牌的端点：
//   GET /w/<file>    下载权重（需 X-Lumen-Token 头，支持 Range 断点续传）
//   GET /manifest    返回可用权重清单（需令牌）
// 不提供任何文件列表 / 直链索引；路径穿越一律拒绝。

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      const token = request.headers.get("X-Lumen-Token") || "";

      // 令牌校验：服务端只比对明文令牌（与安装器内 WEIGHTS_TOKEN 一致）
      if (token !== env.LUMEN_TOKEN) {
        return new Response("Forbidden", { status: 403 });
      }

      // 清单
      if (url.pathname === "/manifest") {
        const listed = await env.LUMEN_WEIGHTS.list();
        const files = listed.objects.map((o) => ({ file: o.key, size: o.size }));
        return new Response(JSON.stringify({ files }), {
          headers: { "content-type": "application/json; charset=utf-8" },
        });
      }

      // 权重下载 /w/<file>
      const m = url.pathname.match(/^\/w\/(.+)$/);
      if (!m) return new Response("Not Found", { status: 404 });

      const key = decodeURIComponent(m[1]);
      // 防穿越：拒绝斜杠/反斜杠/点点/空/编码字符
      if (
        !key ||
        key.includes("/") ||
        key.includes("\\") ||
        key.includes("..") ||
        key.includes("%")
      ) {
        return new Response("Bad Request", { status: 400 });
      }

      const range = request.headers.get("range") || undefined;
      const obj = await env.LUMEN_WEIGHTS.get(key, range ? { range } : undefined);
      if (!obj) return new Response("Not Found", { status: 404 });

      const headers = new Headers();
      headers.set("content-type", "application/octet-stream");
      headers.set("accept-ranges", "bytes");

      if (obj.range) {
        const len = obj.range.end - obj.range.offset + 1;
        headers.set("content-length", String(len));
        headers.set(
          "content-range",
          `bytes ${obj.range.offset}-${obj.range.end}/${obj.size}`
        );
        return new Response(obj.body, { status: 206, headers });
      }

      headers.set("content-length", String(obj.size));
      return new Response(obj.body, { status: 200, headers });
    } catch (e) {
      return new Response("Bad Request", { status: 400 });
    }
  },
};
