"use client";
import { useEffect, useState } from "react";
const fields:[string,string,number,number][]=[['SCAN_DAYS','监测日期范围（天）',1,365],['DROP_THRESHOLD','降价阈值（%）',5,95],['SCAN_BATCH_SIZE','每批报价查询数',1,500],['CATALOG_PAGES_PER_GROUP','每集团每批目录页数',1,1000],['MONITOR_INTERVAL_SECONDS','批次间隔（秒）',30,86400],['SCRAPER_DELAY_MS','报价请求间隔（毫秒）',1500,60000]];
export default function SettingsPanel(){
  const [values,setValues]=useState<Record<string,string|number>>({}),[configured,setConfigured]=useState(false),[ready,setReady]=useState(false),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  useEffect(()=>{fetch('/api/settings').then(async r=>{if(!r.ok)throw new Error();return r.json();}).then(({tokenConfigured,...rest})=>{setConfigured(tokenConfigured);setValues({...rest,TELEGRAM_BOT_TOKEN:''});setReady(true);}).catch(()=>setMessage('读取配置失败，请刷新重试'));},[]);
  async function action(test=false){setBusy(true);setMessage('');try{
    const r=await fetch(test?'/api/telegram/test':'/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(test?{}:values)});
    const body=await r.json();if(!r.ok)throw new Error(body.error||'操作失败');
    if(!test){const {tokenConfigured,...rest}=body;setConfigured(tokenConfigured);setValues({...rest,TELEGRAM_BOT_TOKEN:''});}
    setMessage(test?'测试消息已发送，请查看 Telegram':'已保存，后台下一批任务生效，无需重启');
  }catch(e){setMessage((e as Error).message);}finally{setBusy(false);}}
  return <section className="rounded-2xl border bg-white p-5"><h2 className="mb-2 text-lg font-bold">监控设置</h2><p className="mb-4 text-sm text-slate-500">时区内置 Asia/Shanghai。设置保存在数据卷中；Token 留空保留原值，清空 Chat ID 可停止通知。请仅在可信内网访问此管理页面。</p>
    <form onSubmit={e=>{e.preventDefault();void action();}}><div className="grid gap-4 sm:grid-cols-2">
      <label className="text-sm">Telegram Bot Token<input type="password" autoComplete="new-password" value={values.TELEGRAM_BOT_TOKEN||''} placeholder={configured?'已配置，留空不修改':'输入机器人 Token'} onChange={e=>setValues({...values,TELEGRAM_BOT_TOKEN:e.target.value})} className="mt-2 block w-full rounded-lg border p-3"/></label>
      <label className="text-sm">Telegram Chat ID<input value={values.TELEGRAM_CHAT_ID||''} onChange={e=>setValues({...values,TELEGRAM_CHAT_ID:e.target.value})} className="mt-2 block w-full rounded-lg border p-3"/></label>
      {fields.map(([key,label,min,max])=><label key={key} className="text-sm">{label}<input type="number" required min={min} max={max} step="1" value={values[key]??''} onChange={e=>setValues({...values,[key]:e.target.value})} className="mt-2 block w-full rounded-lg border p-3"/></label>)}
    </div><div className="mt-5 flex flex-wrap gap-3"><button disabled={busy||!ready} className="rounded-lg bg-slate-950 px-5 py-3 text-white disabled:opacity-40">保存设置</button><button type="button" disabled={busy||!ready} onClick={()=>void action(true)} className="rounded-lg border px-5 py-3 disabled:opacity-40">测试已保存的 Telegram 配置</button></div></form>{message&&<p role="status" className="mt-4 text-sm">{message}</p>}
  </section>;
}
