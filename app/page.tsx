"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, BellRing, Bot, CalendarDays, Check, ChevronsUpDown, CircleAlert, CloudCog, Globe2, Hotel, LayoutDashboard, Menu, Radar, RefreshCw, Search, Send, Settings2, ShieldCheck, SlidersHorizontal, TrendingDown, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Toaster } from "@/components/ui/sonner";

type Deal = { id: number; group: string; brand: string; groupTone: string; hotel: string; city: string; country: string; checkIn: string; nights: number; oldPrice: number; price: number; drop: number; detected: string; confidence: "高" | "中"; history: number[] };

const deals: Deal[] = [
  { id: 1, group: "M", brand: "万豪", groupTone: "bg-[#6d1f2f]", hotel: "大阪万豪都酒店", city: "大阪", country: "日本", checkIn: "2026-11-18", nights: 2, oldPrice: 2680, price: 692, drop: 74, detected: "2 分钟前", confidence: "高", history: [2620, 2710, 2580, 2670, 2510, 692] },
  { id: 2, group: "H", brand: "希尔顿", groupTone: "bg-[#153f83]", hotel: "Conrad Maldives Rangali Island", city: "南阿里环礁", country: "马尔代夫", checkIn: "2027-05-12", nights: 3, oldPrice: 7430, price: 2389, drop: 68, detected: "6 分钟前", confidence: "高", history: [7210, 7590, 7480, 7110, 7390, 2389] },
  { id: 3, group: "IHG", brand: "IHG", groupTone: "bg-[#111827]", hotel: "InterContinental Paris Le Grand", city: "巴黎", country: "法国", checkIn: "2027-02-03", nights: 1, oldPrice: 3960, price: 1548, drop: 61, detected: "11 分钟前", confidence: "高", history: [3880, 4020, 3910, 4070, 3820, 1548] },
  { id: 4, group: "H", brand: "凯悦", groupTone: "bg-[#4e2b77]", hotel: "Park Hyatt Sydney", city: "悉尼", country: "澳大利亚", checkIn: "2027-03-21", nights: 2, oldPrice: 6180, price: 2781, drop: 55, detected: "18 分钟前", confidence: "中", history: [6040, 6250, 6010, 6180, 5870, 2781] },
  { id: 5, group: "GHA", brand: "GHA", groupTone: "bg-[#796120]", hotel: "Capella Bangkok", city: "曼谷", country: "泰国", checkIn: "2026-12-07", nights: 2, oldPrice: 4310, price: 2155, drop: 50, detected: "24 分钟前", confidence: "高", history: [4250, 4390, 4180, 4470, 4310, 2155] },
  { id: 6, group: "M", brand: "万豪", groupTone: "bg-[#6d1f2f]", hotel: "The St. Regis Rome", city: "罗马", country: "意大利", checkIn: "2027-01-15", nights: 1, oldPrice: 5220, price: 2871, drop: 45, detected: "31 分钟前", confidence: "中", history: [5100, 5360, 5180, 5270, 5010, 2871] },
];

const groups = ["全部", "万豪", "IHG", "希尔顿", "凯悦", "GHA"];

function Sparkline({ values }: { values: number[] }) {
  const max = Math.max(...values), min = Math.min(...values);
  const points = values.map((value, index) => `${(index / (values.length - 1)) * 76 + 2},${28 - ((value - min) / Math.max(max - min, 1)) * 22 + 2}`).join(" ");
  const last = points.split(" ").at(-1)?.split(",")[1] ?? "28";
  return <svg viewBox="0 0 80 34" className="h-9 w-20" role="img" aria-label="近期价格走势"><path d="M2 30H78" stroke="currentColor" className="text-slate-200" strokeWidth="1" /><polyline points={points} fill="none" stroke="currentColor" className="text-rose-500" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round" /><circle cx="78" cy={last} r="3" className="fill-rose-500" /></svg>;
}

function BrandMark() {
  return <div className="flex items-center gap-3"><div className="relative grid size-10 shrink-0 place-items-center rounded-[13px] bg-[#081728] text-white shadow-[0_8px_22px_rgba(8,23,40,.18)]"><Radar className="size-5" /><span className="absolute right-[9px] top-[9px] size-1.5 rounded-full bg-[#e84a5f] ring-2 ring-[#081728]" /></div><div><div className="text-[17px] font-extrabold tracking-[-.04em] text-slate-950">RateDrop</div><div className="text-[11px] font-medium tracking-[.13em] text-slate-400">HOTEL RADAR</div></div></div>;
}

export default function Home() {
  const [activeGroup, setActiveGroup] = useState("全部");
  const [query, setQuery] = useState("");
  const [threshold, setThreshold] = useState([35]);
  const [selected, setSelected] = useState<Deal>(deals[0]);
  const [scanning, setScanning] = useState(false);
  const [telegramOn, setTelegramOn] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [lastScan, setLastScan] = useState("16:58");
  const [telegramConfigured, setTelegramConfigured] = useState(false);
  const [rateSourceConfigured, setRateSourceConfigured] = useState(false);

  const filtered = useMemo(() => deals.filter((deal) => (activeGroup === "全部" || deal.brand === activeGroup) && `${deal.hotel}${deal.city}${deal.country}`.toLowerCase().includes(query.toLowerCase()) && deal.drop >= threshold[0]), [activeGroup, query, threshold]);

  useEffect(() => {
    const context = typeof document === "undefined" ? undefined : (document as Document & { modelContext?: { registerTool?: (tool: unknown, options?: { signal?: AbortSignal }) => void | Promise<void> } }).modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    void Promise.resolve(context.registerTool({ name: "configure_hotel_price_alerts", title: "配置酒店降价提醒", description: "设置页面中的最低降价百分比和 Telegram 通知开关。", inputSchema: { type: "object", properties: { threshold: { type: "number", minimum: 20, maximum: 80 }, telegramEnabled: { type: "boolean" } }, required: ["threshold"], additionalProperties: false }, annotations: { readOnlyHint: false, untrustedContentHint: false }, execute(input: unknown) { const value = input as { threshold?: number; telegramEnabled?: boolean }; if (typeof value.threshold !== "number" || value.threshold < 20 || value.threshold > 80) throw new Error("threshold 必须在 20–80 之间"); setThreshold([Math.round(value.threshold)]); if (typeof value.telegramEnabled === "boolean") setTelegramOn(value.telegramEnabled); return { threshold: Math.round(value.threshold), telegramEnabled: typeof value.telegramEnabled === "boolean" ? value.telegramEnabled : telegramOn }; } }, { signal: lifecycle.signal })).catch(() => {});
    return () => lifecycle.abort();
  }, [telegramOn]);

  useEffect(() => {
    fetch("/api/system/status", { cache: "no-store" })
      .then((response) => response.json())
      .then((raw) => {
        const status = raw as { telegramConfigured?: boolean; rateSourceConfigured?: boolean };
        setTelegramConfigured(Boolean(status.telegramConfigured));
        setRateSourceConfigured(Boolean(status.rateSourceConfigured));
      })
      .catch(() => {});
  }, []);

  async function runScan() {
    if (scanning) return;
    setScanning(true); toast.loading("正在扫描未来 365 天价格…", { id: "scan" });
    try {
      const response = await fetch("/api/scan", { method: "POST" });
      const result = await response.json() as { configured?: boolean; error?: string };
      await new Promise((resolve) => window.setTimeout(resolve, 900));
      setLastScan(new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }));
      if (!response.ok) toast.warning(result.error ?? "扫描未完成", { id: "scan", description: "当前继续展示接入前演示数据。" });
      else toast.success("实时扫描任务已提交", { id: "scan" });
    } catch {
      toast.error("扫描服务暂时不可用", { id: "scan" });
    } finally { setScanning(false); }
  }

  async function sendTelegramTest() {
    const response = await fetch("/api/telegram/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hotel: selected.hotel, checkIn: selected.checkIn, nights: selected.nights, price: selected.price, drop: selected.drop }) });
    const result = await response.json() as { error?: string };
    if (!response.ok) toast.warning(result.error ?? "测试发送失败", { description: "发布后配置 Bot Token 与 Chat ID 即可启用。" });
    else toast.success("测试消息已发送到 Telegram");
  }

  return <div className="min-h-screen bg-[#f3f6f8] text-slate-900">
    <Toaster position="top-center" richColors />
    <div className="mx-auto flex min-h-screen max-w-[1720px]">
      <aside className={`fixed inset-y-0 left-0 z-40 flex w-[236px] flex-col border-r border-slate-200/80 bg-white px-4 py-5 transition-transform lg:sticky lg:top-0 lg:h-screen lg:translate-x-0 ${sidebarOpen ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="flex items-center justify-between px-2 pb-7"><BrandMark /><Button aria-label="关闭导航" variant="ghost" size="icon-sm" className="lg:hidden" onClick={() => setSidebarOpen(false)}><X /></Button></div>
        <nav aria-label="主导航" className="space-y-1"><button className="nav-item nav-item-active"><LayoutDashboard />监控总览</button><button className="nav-item"><TrendingDown />降价发现<span className="ml-auto rounded-full bg-rose-50 px-2 py-0.5 text-xs font-bold text-rose-600">6</span></button><button className="nav-item"><Hotel />酒店库</button><button className="nav-item"><BellRing />推送记录</button></nav>
        <div className="my-5 h-px bg-slate-100" /><p className="px-3 pb-2 text-xs font-bold uppercase tracking-[.12em] text-slate-400">系统</p>
        <nav className="space-y-1"><button className="nav-item"><CloudCog />数据源</button><button className="nav-item"><Settings2 />监控设置</button></nav>
        <div className="mt-auto rounded-2xl border border-slate-200 bg-[#f7f9fb] p-4"><div className="mb-3 flex items-center gap-2 text-sm font-bold"><Bot className="size-4 text-[#1495d4]" />Telegram 推送</div><div className="mb-3 flex items-center justify-between text-sm"><span className="flex items-center gap-2 text-slate-500"><span className={`size-2 rounded-full ${telegramConfigured && telegramOn ? "bg-emerald-500" : "bg-amber-400"}`} />{telegramConfigured ? (telegramOn ? "已启用" : "已暂停") : "待配置"}</span><Switch checked={telegramOn} onCheckedChange={setTelegramOn} aria-label="切换 Telegram 推送" /></div><p className="text-xs leading-5 text-slate-400">命中阈值后，将酒店、入住日期与价格发送到机器人。</p></div>
      </aside>
      {sidebarOpen && <button aria-label="关闭导航遮罩" className="fixed inset-0 z-30 bg-slate-950/25 lg:hidden" onClick={() => setSidebarOpen(false)} />}

      <main className="min-w-0 flex-1 px-4 pb-8 sm:px-6 xl:px-8">
        <header className="sticky top-0 z-20 -mx-4 mb-5 flex h-[76px] items-center justify-between border-b border-slate-200/80 bg-[#f3f6f8]/90 px-4 backdrop-blur-lg sm:-mx-6 sm:px-6 xl:-mx-8 xl:px-8"><div className="flex items-center gap-3"><Button aria-label="打开导航" variant="outline" size="icon" className="bg-white lg:hidden" onClick={() => setSidebarOpen(true)}><Menu /></Button><div><h1 className="text-xl font-extrabold tracking-[-.035em] sm:text-2xl">全球酒店价格雷达</h1><p className="mt-0.5 hidden text-sm text-slate-500 sm:block">未来 365 天 · 全球范围 · 标准可取消价</p></div></div><div className="flex items-center gap-2"><div className="hidden items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-500 md:flex"><span className={`size-2 rounded-full ${rateSourceConfigured ? "animate-pulse bg-emerald-500" : "bg-amber-400"}`} />{rateSourceConfigured ? "实时监控中" : "演示模式"}</div><Button onClick={runScan} disabled={scanning} className="h-10 rounded-xl bg-[#081728] px-4 hover:bg-[#132d47]"><RefreshCw className={scanning ? "animate-spin" : ""} />{scanning ? "扫描中" : "立即扫描"}</Button></div></header>

        <section aria-label="监控概览" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[
            { icon: Globe2, label: "覆盖酒店", value: "18,642", note: "全球 191 个国家/地区", accent: "text-[#3157a4] bg-blue-50" },
            { icon: CalendarDays, label: "监控日期", value: "365 天", note: "2026.09.15 — 2027.09.14", accent: "text-violet-600 bg-violet-50" },
            { icon: Activity, label: "今日价格检查", value: "2.84M", note: "上次完成 16:58", accent: "text-emerald-600 bg-emerald-50" },
            { icon: CircleAlert, label: "异常降价", value: "6", note: "24 小时内 · 待核验", accent: "text-rose-600 bg-rose-50" },
          ].map((item) => <div key={item.label} className="rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,23,42,.02)]"><div className="mb-4 flex items-center justify-between"><span className={`grid size-9 place-items-center rounded-xl ${item.accent}`}><item.icon className="size-[18px]" /></span><ChevronsUpDown className="size-4 text-slate-300" /></div><div className="text-2xl font-black tracking-[-.04em] text-slate-950">{item.value}</div><div className="mt-1 text-sm font-semibold text-slate-600">{item.label}</div><div className="mt-2 text-xs text-slate-400">{item.note}</div></div>)}
        </section>

        <section className="mt-5 overflow-hidden rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,23,42,.02)]">
          <div className="flex flex-col gap-4 border-b border-slate-100 p-5 xl:flex-row xl:items-center xl:justify-between"><div><div className="flex items-center gap-2"><span className="grid size-8 place-items-center rounded-lg bg-rose-50 text-rose-600"><TrendingDown className="size-4" /></span><h2 className="text-lg font-extrabold tracking-[-.025em]">实时异常降价</h2><span className="rounded-full bg-rose-50 px-2 py-0.5 text-xs font-bold text-rose-600">{filtered.length}</span></div><p className="mt-1 pl-10 text-sm text-slate-400">按历史中位价、同星期与同房型交叉判断</p></div><div className="flex flex-col gap-2 sm:flex-row"><div className="relative min-w-[220px]"><Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-400" /><Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索酒店或城市" className="h-10 rounded-xl bg-slate-50 pl-9 shadow-none" /></div><Select defaultValue="drop"><SelectTrigger className="h-10 w-full rounded-xl bg-white sm:w-[156px]"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="drop">降幅从高到低</SelectItem><SelectItem value="new">最新发现</SelectItem><SelectItem value="price">价格从低到高</SelectItem></SelectContent></Select></div></div>
          <div className="flex gap-2 overflow-x-auto border-b border-slate-100 px-5 py-3 scrollbar-none">{groups.map((group) => <button key={group} onClick={() => setActiveGroup(group)} className={`shrink-0 rounded-lg px-3.5 py-2 text-sm font-semibold transition ${activeGroup === group ? "bg-[#081728] text-white" : "text-slate-500 hover:bg-slate-100 hover:text-slate-900"}`}>{group}</button>)}</div>
          <div className="hidden lg:block"><Table><TableHeader><TableRow className="hover:bg-transparent"><TableHead className="pl-5 text-xs text-slate-400">酒店</TableHead><TableHead className="text-xs text-slate-400">入住</TableHead><TableHead className="text-xs text-slate-400">原价 / 当前价</TableHead><TableHead className="text-xs text-slate-400">降幅</TableHead><TableHead className="text-xs text-slate-400">走势</TableHead><TableHead className="text-xs text-slate-400">发现时间</TableHead><TableHead className="pr-5 text-right text-xs text-slate-400">操作</TableHead></TableRow></TableHeader><TableBody>{filtered.map((deal) => <TableRow key={deal.id} className={`cursor-pointer ${selected.id === deal.id ? "bg-slate-50/80" : ""}`} onClick={() => setSelected(deal)}><TableCell className="py-4 pl-5"><div className="flex items-center gap-3"><span className={`grid size-9 place-items-center rounded-lg text-[11px] font-black text-white ${deal.groupTone}`}>{deal.group}</span><div><div className="max-w-[260px] truncate font-bold text-slate-900">{deal.hotel}</div><div className="mt-1 text-xs text-slate-400">{deal.city} · {deal.country}</div></div></div></TableCell><TableCell><div className="font-semibold">{deal.checkIn.slice(5).replace("-", ".")}</div><div className="mt-1 text-xs text-slate-400">{deal.nights} 晚</div></TableCell><TableCell><div className="text-xs text-slate-400 line-through">¥{deal.oldPrice.toLocaleString()}</div><div className="mt-1 text-base font-black text-slate-950">¥{deal.price.toLocaleString()}</div></TableCell><TableCell><span className="inline-flex items-center gap-1 rounded-md bg-rose-50 px-2 py-1 text-sm font-black text-rose-600"><TrendingDown className="size-3.5" />{deal.drop}%</span></TableCell><TableCell><Sparkline values={deal.history} /></TableCell><TableCell><div className="text-sm text-slate-600">{deal.detected}</div><div className="mt-1 flex items-center gap-1 text-xs text-emerald-600"><ShieldCheck className="size-3" />置信度{deal.confidence}</div></TableCell><TableCell className="pr-5 text-right"><Button variant="outline" size="sm" className="rounded-lg" onClick={(event) => { event.stopPropagation(); toast.success(`已重新推送 ${deal.hotel}`); }}><Send />推送</Button></TableCell></TableRow>)}</TableBody></Table></div>
          <div className="divide-y divide-slate-100 lg:hidden">{filtered.map((deal) => <button key={deal.id} onClick={() => setSelected(deal)} className="w-full p-4 text-left hover:bg-slate-50"><div className="flex items-start gap-3"><span className={`grid size-9 shrink-0 place-items-center rounded-lg text-[11px] font-black text-white ${deal.groupTone}`}>{deal.group}</span><div className="min-w-0 flex-1"><div className="truncate font-bold">{deal.hotel}</div><div className="mt-1 text-xs text-slate-400">{deal.city} · {deal.checkIn}</div></div><span className="rounded-md bg-rose-50 px-2 py-1 text-sm font-black text-rose-600">-{deal.drop}%</span></div><div className="mt-3 flex items-end justify-between pl-12"><div><span className="text-xs text-slate-400 line-through">¥{deal.oldPrice.toLocaleString()}</span><span className="ml-2 text-lg font-black">¥{deal.price.toLocaleString()}</span></div><span className="text-xs text-slate-400">{deal.detected}</span></div></button>)}</div>
          {filtered.length === 0 && <div className="grid place-items-center px-6 py-16 text-center"><Search className="mb-3 size-8 text-slate-300" /><p className="font-bold">没有匹配的异常价格</p><p className="mt-1 text-sm text-slate-400">降低阈值或更换酒店集团后再查看。</p></div>}
        </section>

        <section className="mt-5 grid gap-5 xl:grid-cols-[1.55fr_1fr]">
          <div className="rounded-2xl border border-slate-200/80 bg-white p-5"><div className="mb-5 flex items-start justify-between"><div><h2 className="font-extrabold">监控强度</h2><p className="mt-1 text-sm text-slate-400">降幅达到阈值时进入核验并触发推送</p></div><SlidersHorizontal className="size-5 text-slate-400" /></div><div className="rounded-xl bg-[#f6f8fa] p-4"><div className="mb-4 flex items-center justify-between"><span className="text-sm font-semibold text-slate-600">最低降价幅度</span><span className="rounded-lg bg-white px-3 py-1.5 text-lg font-black text-rose-600 shadow-sm">{threshold[0]}%</span></div><Slider min={20} max={80} step={5} value={threshold} onValueChange={setThreshold} aria-label="最低降价幅度" className="[&_[data-slot=slider-range]]:bg-rose-500 [&_[data-slot=slider-thumb]]:border-rose-500" /><div className="mt-3 flex justify-between text-xs text-slate-400"><span>20% 灵敏</span><span>80% 严格</span></div></div><div className="mt-4 flex flex-wrap gap-x-6 gap-y-3 text-sm text-slate-500"><span className="flex items-center gap-2"><Check className="size-4 text-emerald-500" />同房型比价</span><span className="flex items-center gap-2"><Check className="size-4 text-emerald-500" />税费标准化</span><span className="flex items-center gap-2"><Check className="size-4 text-emerald-500" />多币种换算</span><span className="flex items-center gap-2"><Check className="size-4 text-emerald-500" />二次可订验证</span></div></div>
          <div className="rounded-2xl bg-[#081728] p-5 text-white shadow-[0_18px_45px_rgba(8,23,40,.15)]"><div className="flex items-start justify-between"><div><div className="mb-3 flex items-center gap-2 text-xs font-bold uppercase tracking-[.13em] text-sky-300"><Bot className="size-4" />Telegram</div><h2 className="text-lg font-extrabold">{telegramConfigured ? "推送通道已就绪" : "等待机器人凭据"}</h2><p className="mt-2 text-sm leading-6 text-slate-300">通知包含酒店全名、入住日期、含税价格、降幅和核验提示。</p></div><span className={`size-2.5 rounded-full ring-4 ${telegramConfigured ? "bg-emerald-400 ring-emerald-400/15" : "bg-amber-400 ring-amber-400/15"}`} /></div><div className="mt-5 flex items-center justify-between rounded-xl bg-white/[.07] p-3"><div><div className="text-sm font-bold">{telegramConfigured ? "机器人已连接" : "Bot Token + Chat ID"}</div><div className="mt-1 text-xs text-slate-400">{telegramConfigured ? "可发送实时异常提醒" : "需在发布环境中配置"}</div></div><Dialog><DialogTrigger asChild><Button variant="outline" size="sm" className="border-white/15 bg-white/5 text-white hover:bg-white/10 hover:text-white">测试推送</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>发送测试消息</DialogTitle><DialogDescription>将使用已配置的机器人向目标会话发送一条模拟降价提醒。</DialogDescription></DialogHeader><div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm leading-6"><strong>{selected.hotel}</strong><br />{selected.checkIn} · {selected.nights} 晚<br /><span className="font-bold text-rose-600">¥{selected.price.toLocaleString()}（-{selected.drop}%）</span></div><DialogFooter><Button onClick={sendTelegramTest}><Send />发送测试</Button></DialogFooter></DialogContent></Dialog></div></div>
        </section>
        <footer className="mt-5 flex flex-col gap-2 px-1 text-xs text-slate-400 sm:flex-row sm:items-center sm:justify-between"><span>数据更新时间：今天 {lastScan} · 页面展示为接入前演示数据</span><span className="flex items-center gap-1.5"><ShieldCheck className="size-3.5" />仅使用官方或授权价格源</span></footer>
      </main>
    </div>
  </div>;
}
