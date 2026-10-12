/** @type {import('next').NextConfig} */
// 后端地址：本地开发默认 127.0.0.1:8000；部署时由 BACKEND_URL 环境变量提供
// （Vercel 上在 Project Settings → Environment Variables 配置，构建期注入）。
const backendUrl = (process.env.BACKEND_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/backend/:path*",
        destination: `${backendUrl}/:path*`,
      },
    ];
  },
};

export default nextConfig;
