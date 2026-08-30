import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Bok Voice",
  description: "LiveKit 多账号客服语音助手",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
