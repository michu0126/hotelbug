import { readFile, mkdir, writeFile, rename } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
export const defaults = { TELEGRAM_BOT_TOKEN:'', TELEGRAM_CHAT_ID:'', SCAN_DAYS:365, SCAN_BATCH_SIZE:30, CATALOG_PAGES_PER_GROUP:25, MONITOR_INTERVAL_SECONDS:300, SCRAPER_DELAY_MS:5000, DROP_THRESHOLD:35 };
const ranges = {SCAN_DAYS:[1,365],SCAN_BATCH_SIZE:[1,500],CATALOG_PAGES_PER_GROUP:[1,1000],MONITOR_INTERVAL_SECONDS:[30,86400],SCRAPER_DELAY_MS:[1500,60000],DROP_THRESHOLD:[5,95]};
const file = () => path.join(/* turbopackIgnore: true */ process.env.SCRAPER_DATA_DIR || path.resolve('data'),'settings.json');
export async function readSettings() {
  let saved={};
  try {saved=JSON.parse(await readFile(/* turbopackIgnore: true */ file(),'utf8'));} catch(e) {if(e.code!=='ENOENT')throw e;}
  return Object.fromEntries(Object.entries(defaults).map(([key,value])=>[key,saved[key] ?? process.env[key] ?? value]));
}
export async function saveSettings(input) {
  const values=await readSettings();
  for(const [key,value] of Object.entries(input)) {
    if(!Object.hasOwn(defaults,key))throw new Error('未知配置项');
    if(key in ranges) {
      const n=Number(value),[min,max]=ranges[key];
      if(!Number.isInteger(n)||n<min||n>max)throw new Error(`${key} 必须是 ${min}–${max} 的整数`);
      values[key]=n;
    } else {
      if(typeof value!=='string'||value.length>256)throw new Error('Telegram 配置格式错误');
      if(key==='TELEGRAM_BOT_TOKEN' && value==='')continue;
      values[key]=value.trim();
    }
  }
  const target=file(),tmp=target+'.'+randomUUID()+'.tmp';
  await mkdir(path.dirname(target),{recursive:true});
  await writeFile(tmp,JSON.stringify(values),{mode:0o600});await rename(tmp,target);
  return values;
}
export async function applySettings() {
  for(const [key,value] of Object.entries(await readSettings()))process.env[key]=String(value);
}
export function publicSettings(values) {
  const {TELEGRAM_BOT_TOKEN,...rest}=values;
  return {...rest,tokenConfigured:Boolean(TELEGRAM_BOT_TOKEN)};
}
