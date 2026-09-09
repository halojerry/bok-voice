import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Bok Voice",
  description: "Bok Voice — 本地优先的实时客服语音助手与同声传译工作台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        {/* 运行时配置注入：节点本地托管时由 node-agent 覆写 runtime-config.js，
            必须先于所有业务脚本执行（body 首位、无 async）。 */}
        <script src="/runtime-config.js" />
        {children}
      </body>
    </html>
  );
}
