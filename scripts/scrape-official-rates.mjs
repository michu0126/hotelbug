import { chromium } from 'playwright-core';
import { access, readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { rateUrl, nightlyPrices, datesPresent } from './official-adapters.mjs';
import { openStore, acquireLease, importHotels, selectHotel, planDate, saveSample, writeStatus } from './monitor-store.mjs';

const days = Math.max(1,Math.min(365,Number(process.env.SCAN_DAYS)||365));
const batch = Math.max(1,Math.min(500,Number(process.env.SCAN_BATCH_SIZE)||30));
const delay = Math.max(1500,Number(process.env.SCRAPER_DELAY_MS)||5000);
const threshold = Math.max(5,Math.min(95,Number(process.env.DROP_THRESHOLD)||35));
const db=openStore(), release=acquireLease(db,'rates');
if(!release){console.log('房价查询已在运行');db.close();process.exit(0);}
const sleep = ms => new Promise(r=>setTimeout(r,ms));
const iso = date => date.toISOString().slice(0,10);
const plus = (date,n) => {const d=new Date(date+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+n);return iso(d);};
let browser;
const results=[];
async function sendPending() {
  const token=process.env.TELEGRAM_BOT_TOKEN, chat=process.env.TELEGRAM_CHAT_ID;
  if(!token || !chat || process.env.TELEGRAM_ENABLED==='false') return;
  for(const row of db.prepare('SELECT * FROM alerts WHERE sent_at IS NULL AND attempts<10 ORDER BY created_at LIMIT 5').all()) {
    const a=JSON.parse(row.payload);
    db.prepare('UPDATE alerts SET attempts=attempts+1 WHERE id=?').run(row.id);
    try {
      const response=await fetch('https://api.telegram.org/bot'+token+'/sendMessage',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({chat_id:chat,text:['酒店官网降价线索',a.hotel,'入住：'+a.checkIn+'（1晚，2成人，1间）',a.currency+' '+a.price+'，上次 '+a.previousPrice+'，下降 '+a.drop+'%',a.url,'最低可见每晚价；房型、会员资格、税费和取消政策请以官网为准。'].join('\n'),disable_web_page_preview:true}),
        signal:AbortSignal.timeout(15000)
      });
      const result=await response.json();
      if(response.ok && result.ok) db.prepare('UPDATE alerts SET sent_at=? WHERE id=?').run(Date.now(),row.id);
      if(response.status===429) break;
    } catch {console.error('Telegram 发送失败，消息已保留待重试');}
  }
}
try {
  // Migrate the hand-selected examples into the same durable queue.
  const file=process.env.SCRAPER_TARGETS_FILE || path.resolve('config/official-hotels.json');
  const examples=JSON.parse(await readFile(file,'utf8'));
  const definitions=examples.flatMap(h=>{
    const u=new URL(h.urlTemplate);
    const code=u.searchParams.get('propertyCode')||u.searchParams.get('ctyhocn')||u.searchParams.get('hotelCode')||(h.group==='hyatt'?u.pathname.split('/').filter(Boolean).at(-1):null);
    return code?[{...h,id:h.group+':'+code.toLowerCase(),code,nameVerified:true,officialUrl:u.origin}]:[];
  });
  importHotels(db,definitions);
  const paths=[process.env.CHROMIUM_PATH,'/usr/bin/chromium','/usr/bin/chromium-browser','C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'];
  let executablePath;
  for(const p of paths.filter(Boolean)){try{await access(p);executablePath=p;break;}catch{}}
  if(!executablePath) throw new Error('未找到 Chromium，请配置 CHROMIUM_PATH');
  browser=await chromium.launch({executablePath,headless:process.env.SCRAPER_HEADLESS!=='false',args:['--no-sandbox','--disable-dev-shm-usage']});
  const context=await browser.newContext({locale:'en-US',viewport:{width:1365,height:900}});
  const page=await context.newPage();
  page.setDefaultTimeout(10000);
  await sendPending();
  for(let i=0;i<batch;i++){
    const job=selectHotel(db);if(!job)break;
    const hotel=JSON.parse(job.definition), today=iso(new Date());
    const {checkIn,nextOffset}=planDate(db,job,days,today);
    const checkOut=plus(checkIn,1), url=rateUrl(hotel,checkIn,checkOut);
    const now=Date.now();
    db.prepare('UPDATE hotels SET last_attempt=?,checks=checks+1 WHERE id=?').run(now,hotel.id);
    try{
      if(!url) throw new Error('尚无此集团的日期预订适配器');
      const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
      if(!response || response.status()>=400) throw new Error('HTTP '+(response?.status()||'no-response'));
      for(const name of [/accept all/i,/accept cookies/i,/接受全部/]){
        const button=page.getByRole('button',{name}).first();
        if(await button.isVisible().catch(()=>false)){await button.click({timeout:2000}).catch(()=>{});break;}
      }
      await page.waitForTimeout(6000);
      const body=await page.locator('body').innerText();
      if(/captcha|verify you are human|access denied|unusual traffic|机器人验证/i.test(body))throw new Error('访问拒绝或人机验证');
      // A URL parameter alone is not proof the booking engine used the requested dates.
      const fields=await page.locator('input').evaluateAll(es=>es.filter(e=>e.type!=='hidden').map(e=>e.value).join(' '));
      const verifiedDates=datesPresent(body+' '+fields,checkIn,checkOut);
      if(!verifiedDates) throw new Error('页面未确认请求的入住和离店日期');
      if(/sold out|no rooms available|无可用客房|暂无空房/i.test(body)) {
        db.prepare("UPDATE hotels SET state='sold_out',error=NULL,next_offset=?,retry_at=? WHERE id=?").run(nextOffset,now+60000,hotel.id);
        results.push({hotelId:hotel.id,checkIn,available:false});
      } else {
        const prices=nightlyPrices(body);
        if(!prices.length)throw new Error('未识别到明确币种的每晚房价');
        const currencies=new Set(prices.map(p=>p.currency));
        if(currencies.size!==1)throw new Error('页面含多种币种，无法确定可比较房价');
        const record={checkIn,checkOut,...prices[0],url:page.url()};
        saveSample(db,hotel,record,threshold);
        db.prepare("UPDATE hotels SET state='ok',error=NULL,next_offset=?,retry_at=? WHERE id=?").run(nextOffset,now+60000,hotel.id);
        results.push({hotelId:hotel.id,...record});
      }
    }catch(error){
      const message=error.message;
      const blocked=/HTTP (403|429)|访问拒绝|人机验证/.test(message);
      const retry=now+(blocked?6:1)*3600000;
      db.prepare("UPDATE hotels SET state='error',error=?,retry_at=? WHERE id=?").run(message,retry,hotel.id);
      if(blocked) db.prepare('INSERT OR REPLACE INTO cooldowns VALUES(?,?,?)').run(hotel.group,retry,message);
      results.push({hotelId:hotel.id,checkIn,error:message});
      if(process.env.SCRAPER_DEBUG_DIR){
        await mkdir(process.env.SCRAPER_DEBUG_DIR,{recursive:true});
        await writeFile(path.join(process.env.SCRAPER_DEBUG_DIR,hotel.id.replaceAll(':','-')+'.txt'),await page.locator('body').innerText().catch(()=>''),'utf8');
      }
    }
    await sendPending();
    await writeStatus(db,{windowDays:days});
    console.log(JSON.stringify(results.at(-1)));
    await sleep(delay);
  }
  db.prepare('DELETE FROM samples WHERE check_in<?').run(iso(new Date()));
}finally{
  if(browser)await browser.close();
  release();
  await writeStatus(db,{windowDays:days}).catch(e=>console.error(e.message));
  db.close();
}
console.log(JSON.stringify({queries:results.length,windowDays:days}));

