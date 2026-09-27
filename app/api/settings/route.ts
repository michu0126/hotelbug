import { readSettings, saveSettings, publicSettings } from "@/scripts/settings.mjs";
export const runtime="nodejs";
export const dynamic="force-dynamic";
export async function GET(){return Response.json(publicSettings(await readSettings()),{headers:{"Cache-Control":"no-store"}});}
export async function POST(request:Request){
  const origin=request.headers.get("origin");
  if(origin && new URL(origin).host!==request.headers.get("host"))return Response.json({error:"不允许跨站修改"},{status:403});
  try{
    const input=await request.json();
    if(!input||typeof input!=="object"||Array.isArray(input))throw new Error("配置格式错误");
    return Response.json(publicSettings(await saveSettings(input)));
  }catch(e){return Response.json({error:(e as Error).message},{status:400});}
}
