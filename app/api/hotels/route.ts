import { readFile } from "node:fs/promises";
import path from "node:path";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
type Hotel = { id: string; group: string; name?: string; nameFromUrl?: string; officialUrl: string; rateStatus: string };
export async function GET(request: Request) {
  const params=new URL(request.url).searchParams;
  const q=(params.get("q")||"").toLowerCase().slice(0,200), group=params.get("group");
  const page=Math.max(0,Number(params.get("page"))||0);
  try {
    const catalog=JSON.parse(await readFile(/* turbopackIgnore: true */ path.join(/* turbopackIgnore: true */ process.env.SCRAPER_DATA_DIR||path.resolve("data"),"official-catalog.json"),"utf8"));
    const hotels=(catalog.hotels as Hotel[]).filter(h=>(!group||h.group===group)&&`${h.id} ${h.name||h.nameFromUrl||""}`.toLowerCase().includes(q));
    return Response.json({total:hotels.length,hotels:hotels.slice(page*50,page*50+50)},{headers:{"Cache-Control":"no-store"}});
  } catch(e) {if((e as NodeJS.ErrnoException).code==='ENOENT')return Response.json({total:0,hotels:[]});throw e;}
}

