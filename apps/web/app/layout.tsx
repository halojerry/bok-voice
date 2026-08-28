import "./globals.css";
import type { Metadata } from "next";
import { Shell } from "@/components/Shell";

export const metadata: Metadata = {
  title: "Bok Voice",
  description: "LiveKit 多账号客服语音助手",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
