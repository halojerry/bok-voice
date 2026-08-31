/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  typescript: { ignoreBuildErrors: true },
  output: "export",
  // 静态导出：SPA 在浏览器端通过 window.fetch 访问本机控制面(:8000)与
  // 同传 WS(:8790)，不需要 Next 服务端。trailingSlash 确保 out/ 路由可直开文件。
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
