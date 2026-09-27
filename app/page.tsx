"use client";

import { useEffect, useState } from "react";
import SettingsPanel from "./settings-panel";
import { Radar, RefreshCw, Globe2, CalendarDays, Activity, BellRing, ExternalLink, Search } from "lucide-react";

const names: Record<string,string>={marriott:"万豪",ihg:"IHG",hilton:"希尔顿",hyatt:"凯悦",gha:"GHA"};
type Quote={hotel:string;checkIn:string;price:number;currency:string;checkedAt:string;url:string;drop?:number;telegramSent?:boolean;basis?:string};
type Source={group:string;status:string;hotels:number;pendingPages:number;failures:{url:string;error:string}[]};
type Status={telegramConfigured:boolean;monitoringWindowDays:number;threshold:number;catalog:{count:number;updatedAt:string;sources:Source[]}|null;monitor:{updatedAt:string;workerRunning:boolean;groups:{name:string;hotels:number;checks:number;successfulHotels:number;failedHotels:number;unsupportedHotels:number}[];freshHotelDates:number;latest:Quote[];alerts:Quote[];errors:{id:string;error:string}[];cooldowns:{group_name:string;until_at:number;reason:string}[]}|null};
type Hotel={id:string;group:string;name?:string;nameFromUrl?:string;officialUrl:string;rateStatus:string};
const time=(value:string)=>new Date(value).toLocaleString("zh-CN");
export default function Home(){
  const [status,setStatus]=useState<Status|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false),[message,setMessage]=useState("");
  const [query,setQuery]=useState(""),[group,setGroup]=useState(""),[page,setPage]=useState(0),[hotels,setHotels]=useState<Hotel[]>([]),[total,setTotal]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();
    async function refresh(){try{const r=await fetch("/api/system/status",{cache:"no-store",signal:controller.signal});if(!r.ok)throw new Error("读取监控状态失败");setStatus(await r.json());setError("");}catch(e){if(!controller.signal.aborted)setError((e as Error).message);}}
    void refresh();const timer=setInterval(refresh,15000);return()=>{controller.abort();clearInterval(timer);};
  },[]);
  useEffect(()=>{
    const controller=new AbortController();
    const timer=setTimeout(()=>{void fetch("/api/hotels?"+new URLSearchParams({q:query,group,page:String(page)}),{signal:controller.signal}).then(r=>{if(!r.ok)throw new Error("读取酒店库失败");return r.json() as Promise<{hotels:Hotel[];total:number}>;}).then(r=>{setHotels(r.hotels);setTotal(r.total);}).catch(e=>{if(!controller.signal.aborted)setError(e.message);});},250);
    return()=>{clearTimeout(timer);controller.abort();};
  },[query,group,page,status?.catalog?.updatedAt]);
  async function scan(){setBusy(true);try{const r=await fetch("/api/scan",{method:"POST"});const o=await r.json() as {error?:string};setMessage(r.ok?"已提交目录同步和房价查询批次，结果会自动刷新。":o.error||"提交失败");}catch{setMessage("无法连接扫描服务");}finally{setBusy(false);}}
  const monitor=status?.monitor;
  const checks=monitor?.groups.reduce((n,g)=>n+g.checks,0)||0;
  const verified=monitor?.groups.reduce((n,g)=>n+g.successfulHotels,0)||0;
  const running=!!monitor?.workerRunning;
  return <main className="min-h-screen bg-slate-50 text-slate-900">
    <header className="bg-slate-950 text-white"><div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-6 py-7"><div className="flex items-center gap-3"><Radar className="size-10 text-rose-400"/><div><h1 className="text-2xl font-bold">RateDrop 全球酒店雷达</h1><p className="mt-1 text-sm text-slate-400">集团官网目录 · 滚动日期查询 · Telegram 降价线索</p></div></div><button onClick={scan} disabled={busy} className="flex items-center gap-2 rounded-xl bg-white px-4 py-3 font-semibold text-slate-950 disabled:opacity-50"><RefreshCw className={busy?"size-4 animate-spin":"size-4"}/>{busy?"提交中":"同步并查询一批"}</button></div></header>
    <div className="mx-auto max-w-7xl space-y-6 px-6 py-7">
      {(error||message)&&<p role="status" className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm">{error||message}</p>}
      <div className="rounded-xl border border-blue-100 bg-blue-50 p-4 text-sm leading-6 text-blue-950">酒店目录持续同步中，收录数量不等于房价覆盖数量。只有页面确认日期、币种和每晚价格后才会记入报价。GHA 已启用官网页面自动采价，记录非会员未税起价；各集团成功覆盖以实际报价为准。</div>
      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[{icon:Globe2,label:"官网酒店名录",value:status?.catalog?.count||0,note:"尚未验证集团全量覆盖"},{icon:CalendarDays,label:"滚动目标日期",value:(status?.monitoringWindowDays||365)+" 天",note:"按批次推进，非一年价格已查完"},{icon:Activity,label:"实际查询次数",value:checks,note:"最近查询成功酒店："+verified},{icon:BellRing,label:"近24小时有效酒店日期",value:monitor?.freshHotelDates||0,note:"每家酒店 × 每个入住日期"}].map(c=><article key={c.label} className="rounded-2xl border border-slate-200 bg-white p-5"><c.icon className="mb-4 size-5 text-slate-500"/><p className="text-3xl font-bold">{typeof c.value==="number"?c.value.toLocaleString():c.value}</p><h2 className="mt-2 font-medium">{c.label}</h2><p className="mt-2 text-xs text-slate-500">{c.note}</p></article>)}
      </section>
      <section className="rounded-2xl border bg-white p-5"><h2 className="mb-4 text-lg font-bold">集团官网接入进度</h2><div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="text-slate-500"><tr><th className="py-3">集团</th><th>已收录</th><th>待处理目录</th><th>房价状态</th><th>目录状态 / 最近错误</th></tr></thead><tbody>{Object.entries(names).map(([id,name])=>{const source=status?.catalog?.sources.find(s=>s.group===id),g=monitor?.groups.find(s=>s.name===id),pause=monitor?.cooldowns.find(s=>s.group_name===id);return <tr key={id} className="border-t"><td className="py-4 font-semibold">{name}</td><td>{source?.hotels||0}</td><td>{source?.pendingPages??"—"}</td><td>{pause?"暂停至 "+time(new Date(pause.until_at).toISOString()):g?.successfulHotels?g.successfulHotels+" 家最近成功":"尚无成功报价"}</td><td className="max-w-xs break-words">{source?.failures?.at(-1)?.error||(source?.status==="directory_only"?"当前目录遍历完成，完整性待核验":source?"同步中":"等待同步")}</td></tr>;})}</tbody></table></div></section>
      <section className="rounded-2xl border bg-white p-5"><div className="mb-4 flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-bold">最近官网报价</h2><span className="text-sm text-slate-500">{running?"查询批次运行中":"等待下一批查询"} · Telegram {status?.telegramConfigured?"已配置":"未配置"}</span></div><p className="mb-4 text-xs text-slate-500">1间 · 2成人 · 1晚。保留原币种，税费、会员条件、房型和取消政策未统一。</p><Quotes rows={monitor?.latest||[]}/></section>
      <section className="rounded-2xl border bg-white p-5"><h2 className="mb-2 text-lg font-bold">降价线索与推送</h2><p className="mb-4 text-sm text-slate-500">与同酒店、同入住日、同币种的上次最低可见每晚价比较，阈值 {status?.threshold||35}%。不同房型或规则可能造成价格变化。</p><Quotes rows={monitor?.alerts||[]} alerts/></section>
      <section className="rounded-2xl border bg-white p-5"><div className="mb-4 flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-bold">官网酒店库 <span className="text-sm font-normal text-slate-500">{total.toLocaleString()} 条</span></h2><div className="flex flex-wrap gap-2"><select aria-label="酒店集团" value={group} onChange={e=>{setGroup(e.target.value);setPage(0);}} className="rounded-lg border p-2"><option value="">全部集团</option>{Object.entries(names).map(([id,name])=><option key={id} value={id}>{name}</option>)}</select><label className="flex items-center gap-2 rounded-lg border px-3"><Search className="size-4"/><input aria-label="搜索酒店" placeholder="酒店名称或代码" value={query} onChange={e=>{setQuery(e.target.value);setPage(0);}} className="min-w-0 py-2 outline-none"/></label></div></div><p className="mb-3 text-xs text-slate-500">部分名称由官网链接生成；点击官网可核对酒店正式名称。</p><div className="divide-y">{hotels.map(h=><a key={h.id} href={h.officialUrl} target="_blank" rel="noreferrer" className="flex items-center justify-between gap-3 py-3 text-sm hover:text-blue-700"><span><span className="mr-3 text-slate-400">{names[h.group]}</span>{h.name||h.nameFromUrl||h.id}<span className="ml-3 text-xs text-slate-400">{h.id}</span></span><ExternalLink className="size-4 shrink-0"/></a>)}</div>{!hotels.length&&<p className="py-8 text-center text-slate-500">暂无匹配酒店，目录同步后显示。</p>}<div className="mt-4 flex justify-between"><button className="rounded-lg border px-4 py-2 disabled:opacity-30" disabled={page===0} onClick={()=>setPage(page-1)}>上一页</button><span className="p-2 text-sm">第 {page+1} 页</span><button className="rounded-lg border px-4 py-2 disabled:opacity-30" disabled={(page+1)*50>=total} onClick={()=>setPage(page+1)}>下一页</button></div></section>
      {!!monitor?.errors.length&&<section className="rounded-2xl border bg-white p-5"><h2 className="mb-3 font-bold">最近查询问题</h2>{monitor.errors.map(e=><p key={e.id} className="py-1 text-sm text-slate-600">{e.id}：{e.error}</p>)}</section>}
      <SettingsPanel/>
      <footer className="text-xs text-slate-500">采集状态更新：{monitor?.updatedAt?time(monitor.updatedAt):"尚无记录"} · 所有统计来自本机采集，无演示房价。</footer>
    </div>
  </main>;
}
function Quotes({rows,alerts=false}:{rows:Quote[];alerts?:boolean}){
  if(!rows.length)return <p className="rounded-xl bg-slate-50 py-10 text-center text-sm text-slate-500">{alerts?"暂无降价记录；需要同一入住日的前后两次有效报价。":"尚无通过日期核验的报价。可在下方查看查询问题。"}</p>;
  return <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="text-slate-500"><tr><th className="pb-3">酒店</th><th>入住日期</th><th>每晚报价</th><th>{alerts?"推送状态":"采集时间"}</th><th>官网</th></tr></thead><tbody>{rows.map((r,i)=><tr key={r.hotel+r.checkIn+i} className="border-t"><td className="max-w-sm py-4 pr-3">{r.hotel}</td><td className="whitespace-nowrap pr-3">{r.checkIn}</td><td className="whitespace-nowrap pr-3 font-semibold">{r.currency} {r.price.toLocaleString()}{r.basis?.startsWith("gha-")&&<span className="block text-xs font-normal text-slate-500">非会员 · 未含税费起价</span>}{alerts&&<span className="ml-2 text-rose-600">-{r.drop}%</span>}</td><td className="pr-3 text-slate-500">{alerts?(r.telegramSent?"已发送":"待发送 / 重试"):time(r.checkedAt)}</td><td><a href={r.url} target="_blank" rel="noreferrer" className="text-blue-600">核对</a></td></tr>)}</tbody></table></div>;
}
