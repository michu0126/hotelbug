import { mkdir, writeFile, rename, readFile } from 'node:fs/promises';
import { applySettings } from './settings.mjs';
await applySettings();
import path from 'node:path';
import { sources, locations, acceptSitemap, hotelFromUrl, ghaCandidate, ghaFromHtml } from './official-adapters.mjs';
import { openStore, acquireLease, importHotels, dataDir } from './monitor-store.mjs';

await mkdir(dataDir,{recursive:true});
const db=openStore(), release=acquireLease(db,'catalog');
if (!release) {console.log('目录同步已在运行');db.close();process.exit(0);}
const checkpointFile=path.join(dataDir,'catalog-checkpoint.json');
let checkpoint={version:2,hotels:[],groups:{}};
try {
  const old=JSON.parse(await readFile(checkpointFile,'utf8'));
  checkpoint=old.version===2 ? old : {...checkpoint,hotels:old.hotels || []};
} catch(e) {if(e.code!=='ENOENT') {release();db.close();throw e;}}
const hotels=new Map(checkpoint.hotels.map(h=>[h.id,h]));
const budget=Math.max(1,Math.min(1000,Number(process.env.CATALOG_PAGES_PER_GROUP)||25));
async function persist() {
  checkpoint.hotels=[...hotels.values()];
  await writeFile(checkpointFile+'.tmp',JSON.stringify(checkpoint));await rename(checkpointFile+'.tmp',checkpointFile);
  const file=path.join(dataDir,'official-catalog.json');
  await writeFile(file+'.tmp',JSON.stringify({updatedAt:new Date().toISOString(),coverage:'unverified',sources:Object.values(checkpoint.groups).map(g=>g.report).filter(Boolean),hotels:checkpoint.hotels}));await rename(file+'.tmp',file);
  importHotels(db,checkpoint.hotels);
}
try {
  for(const [group,seed] of sources) {
    const previous=checkpoint.groups[group];
    if((previous?.retryAt>Date.now() && previous.report.failures.some(f=>!/HTTP (404|410)/.test(f.error))) || (previous?.report?.hotels>0 && previous?.completedAt>Date.now()-7*86400000)) continue;
    const queue=previous?.queue?.length ? [...previous.queue] : [seed];
    const seen=new Set(previous?.queue?.length ? previous.seen : []);
    const report={group,status:'running',pages:0,hotels:0,failures:[],pendingPages:queue.length};
    let attempts=0,retryAt=0;
    while(queue.length && attempts<budget) {
      const url=queue.shift();if(seen.has(url)) continue;
      seen.add(url);attempts++;
      try {
        if(new URL(url).origin!==new URL(seed).origin) throw new Error('Unexpected sitemap origin');
        const response=await fetch(url,{signal:AbortSignal.timeout(20000),redirect:'error'});
        if(!response.ok) throw new Error('HTTP '+response.status);
        const body=await response.text();report.pages++;
        if(url.endsWith('robots.txt')) {
          const maps=[...body.matchAll(/^Sitemap:\s*(\S+)/gmi)].map(m=>m[1]).filter(u=>acceptSitemap(group,u));
          if(!maps.length) throw new Error('No usable official sitemaps');queue.push(...maps);
        } else if(/<sitemapindex\b/i.test(body)) {
          queue.push(...locations(body).filter(u=>acceptSitemap(group,u)));
        } else if(/<urlset\b/i.test(body)) {
          for(const link of locations(body)) {
            const hotel=hotelFromUrl(group,link,url);
            if(hotel) hotels.set(hotel.id,hotel);
            if(group==='gha' && new URL(link).origin===new URL(seed).origin && ghaCandidate(link)) queue.push(link);
          }
        } else if(group==='gha') {
          const hotel=ghaFromHtml(body,url);if(hotel) hotels.set(hotel.id,hotel);
        } else throw new Error('Not an XML sitemap');
      } catch(error) {
        report.failures.push({url,error:[error.message,error.cause?.code].filter(Boolean).join(' / ')});
        if(!/HTTP (404|410)/.test(error.message)) {seen.delete(url);queue.push(url);}
        if(!/HTTP (404|410)/.test(error.message)) retryAt=Date.now()+(/HTTP (403|429)/.test(error.message)?6*3600000:5*60000);
      }
      report.hotels=[...hotels.values()].filter(h=>h.group===group).length;
      const pending=[...new Set(queue)].filter(u=>!seen.has(u));
      report.pendingPages=pending.length;
      report.status=report.failures.length?'partial_or_blocked':pending.length?'partial_directory':report.hotels?'directory_only':'needs_adapter';
      checkpoint.groups[group]={queue:pending,seen:[...seen],retryAt,completedAt:pending.length?null:Date.now(),report};
      await persist();
      if(retryAt) break;
      await new Promise(r=>setTimeout(r,1500));
    }
    console.log(JSON.stringify(report));
  }
  await persist();
  console.log('已保存 '+hotels.size+' 家官网酒店；名录不代表房价已验证。');
} finally {release();db.close();}
