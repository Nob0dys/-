import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "智能报价工作台",
  description: "参数优先、单位校验、多制造商方案与审计可追溯的内部报价系统。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
