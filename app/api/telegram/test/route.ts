import { readSettings } from "@/scripts/settings.mjs";
export const runtime="nodejs";
export async function POST(request:Request){
  const origin=request.headers.get("origin");
  if(origin && new URL(origin).host!==request.headers.get("host"))return Response.json({error:"不允许跨站发送"},{status:403});
  const settings=await readSettings();
  if(!settings.TELEGRAM_BOT_TOKEN||!settings.TELEGRAM_CHAT_ID)return Response.json({error:"请先保存 Telegram 配置"},{status:424});
  try{
    const r=await fetch(`https://api.telegram.org/bot${settings.TELEGRAM_BOT_TOKEN}/sendMessage`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({chat_id:settings.TELEGRAM_CHAT_ID,text:"✅ Hotelbug 测试消息：Telegram 通知连接成功。这不是酒店报价。"}),signal:AbortSignal.timeout(15000)});
    const result=await r.json();
    if(!r.ok||!result.ok)throw new Error("发送失败");
    return Response.json({ok:true});
  }catch{return Response.json({error:"Telegram 发送失败，请检查配置和网络"},{status:502});}
}
