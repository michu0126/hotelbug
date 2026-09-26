import { spawn } from 'node:child_process';
import path from 'node:path';

let stopped=false, child;
for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>{stopped=true;child?.kill(signal);});
async function run(name) {
  await new Promise((resolve,reject)=>{
    child=spawn(process.execPath,[path.join(import.meta.dirname,name)],{stdio:'inherit',env:process.env});
    child.once('error',reject);child.once('exit',code=>{if(code && !stopped)console.error(name+' exited '+code);resolve();});
  });
  child=undefined;
}
while(!stopped){
  try{await run('sync-official-catalog.mjs');if(!stopped)await run('scrape-official-rates.mjs');}catch(e){console.error(e.message);}
  if(process.argv.includes('--once'))break;
  const seconds=Math.max(30,Number(process.env.MONITOR_INTERVAL_SECONDS)||300);
  for(let i=0;i<seconds && !stopped;i++)await new Promise(r=>setTimeout(r,1000));
}

