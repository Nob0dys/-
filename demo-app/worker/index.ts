/** Cloudflare Worker entry point for the vinext-starter template. */
import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";

interface Env {
  ASSETS: Fetcher;
  DB: D1Database;
  IMAGES: {
    input(stream: ReadableStream): {
      transform(options: Record<string, unknown>): {
        output(options: { format: string; quality: number }): Promise<{ response(): Response }>;
      };
    };
  };
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

// Image security config. SVG sources with .svg extension auto-skip the
// optimization endpoint on the client side (served directly, no proxy).
// To route SVGs through the optimizer (with security headers), set
// dangerouslyAllowSVG: true in next.config.js and uncomment below:
// const imageConfig: ImageConfig = { dangerouslyAllowSVG: true };

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    // 本地生产模式 API 代理：/api/* 转发到后端 FastAPI (127.0.0.1:8000)。
    // 演示/交付时前端与后端同机运行，此转发让前端相对路径 /api 直达后端。
    if (url.pathname.startsWith("/api/")) {
      const target = new URL(url.pathname + url.search, "http://127.0.0.1:8000");
      const headers = new Headers(request.headers);
      headers.delete("host");
      headers.delete("expect"); // undici 不支持 expect 头
      const init: RequestInit = { method: request.method, headers, redirect: "manual" };
      if (request.body) {
        init.body = request.body;
        // Node 18+ fetch 要求带 body 时显式声明 duplex
        (init as RequestInit & { duplex: string }).duplex = "half";
      }
      return fetch(new Request(target, init));
    }

    if (url.pathname === "/_vinext/image") {
      const allowedWidths = [...DEFAULT_DEVICE_SIZES, ...DEFAULT_IMAGE_SIZES];
      return handleImageOptimization(request, {
        fetchAsset: (path) => env.ASSETS.fetch(new Request(new URL(path, request.url))),
        transformImage: async (body, { width, format, quality }) => {
          const result = await env.IMAGES.input(body).transform(width > 0 ? { width } : {}).output({ format, quality });
          return result.response();
        },
      }, allowedWidths);
    }

    return handler.fetch(request, env, ctx);
  },
};

export default worker;
