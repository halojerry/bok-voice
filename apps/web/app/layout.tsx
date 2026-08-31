import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Bok Voice",
  description: "Bok Voice — 本地优先的实时客服语音助手与同声传译工作台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
