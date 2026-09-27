import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title:"RateDrop · 全球酒店价格雷达",description:"监控全球主要酒店集团未来一年价格，发现异常降价并通过 Telegram 推送。",icons:{icon:"/favicon.svg",shortcut:"/favicon.svg"} };
export default function RootLayout({ children }:Readonly<{ children:React.ReactNode }>) { return <html lang="zh-CN"><body className="antialiased">{children}</body></html>; }
