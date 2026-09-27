// Read only rendered website text and links. No booking API calls or response interception.
export function parseGhaPage(text, checkIn, checkOut) {
  const clean=text.replace(/\u00a0/g,' ');
  const start=new Date(checkIn+'T12:00:00Z'),end=new Date(checkOut+'T12:00:00Z');
  if(end-start!==86400000)throw new Error('GHA 页面采集仅支持一晚');
  const range=clean.match(/DATES\s*(?:[A-Za-z]{3},?\s*)?(\d{1,2})\s+([A-Za-z]{3})\s*(\d{4})?\s*[-–]\s*(?:[A-Za-z]{3},?\s*)?(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s*ROOM\s*1:\s*2\s*ADULTS/i);
  const month=d=>d.toLocaleString('en-US',{month:'short',timeZone:'UTC'}).toLowerCase();
  if(!range || Number(range[1])!==start.getUTCDate() || range[2].toLowerCase()!==month(start) || Number(range[3]||range[6])!==start.getUTCFullYear() || Number(range[4])!==end.getUTCDate() || range[5].toLowerCase()!==month(end) || Number(range[6])!==end.getUTCFullYear())throw new Error('GHA 页面未确认请求日期和2成人条件');
  const prices=[...clean.matchAll(/NON-MEMBER RATES\s+([A-Z]{3})\s+([\d,]+(?:\.\d{1,2})?)\s+Excluding taxes and fees/gi)].map(m=>({currency:m[1].toUpperCase(),price:Number(m[2].replaceAll(',',''))})).filter(p=>p.price>0 && Intl.supportedValuesOf('currency').includes(p.currency));
  if(!prices.length)throw new Error('GHA 页面未显示明确的非会员未税价格');
  if(new Set(prices.map(p=>p.currency)).size!==1)throw new Error('GHA 页面币种不一致');
  return {...prices.sort((a,b)=>a.price-b.price)[0],basis:'gha-visible-nonmember-excluding-taxes-1night-2adults-v1'};
}
async function navigate(page,url){
  const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:45000});
  if(!response||response.status()>=400)throw new Error('HTTP '+(response?.status()||'no-response'));
}
export async function scrapeGhaPage(page,hotel,checkIn,checkOut){
  const official=new URL(hotel.officialUrl);
  if(official.origin!=='https://www.ghadiscovery.com')throw new Error('GHA 酒店链接来源不匹配');
  await navigate(page,official.href);
  const link=page.locator('a[href*="/booking/select_room?"]').first();
  await link.waitFor({state:'attached',timeout:20000});
  const booking=new URL(await link.getAttribute('href'),official);
  if(booking.origin!==official.origin||booking.pathname!=='/booking/select_room'||!booking.searchParams.get('hotelId'))throw new Error('GHA 官网未提供支持的预订页面');
  booking.searchParams.set('startDate',checkIn);booking.searchParams.set('endDate',checkOut);
  booking.searchParams.set('room1Adults','2');booking.searchParams.set('room1Children','0');
  await navigate(page,booking.href);
  let lastError;
  for(let attempt=0;attempt<10;attempt++){
    const text=await page.locator('body').innerText();
    if(/verify you are human|access denied|unusual traffic|机器人验证/i.test(text))throw new Error('访问拒绝或人机验证');
    try{return {...parseGhaPage(text,checkIn,checkOut),url:page.url(),checkIn,checkOut};}catch(e){lastError=e;}
    await page.waitForTimeout(2000);
  }
  throw lastError;
}
