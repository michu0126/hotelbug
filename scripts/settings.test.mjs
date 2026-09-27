import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {readSettings,saveSettings,publicSettings,applySettings} from './settings.mjs';
test('settings defaults, validation, persistence, secret masking and worker loading',async()=>{
  const dir=await mkdtemp(path.join(os.tmpdir(),'hotelbug-settings-'));
  const old={...process.env};process.env.SCRAPER_DATA_DIR=dir;
  try{
    assert.equal(Number((await readSettings()).SCAN_DAYS),365);
    await saveSettings({TELEGRAM_BOT_TOKEN:'test-secret',TELEGRAM_CHAT_ID:'123',DROP_THRESHOLD:40});
    await saveSettings({TELEGRAM_BOT_TOKEN:'',SCAN_DAYS:180});
    const values=await readSettings();
    assert.equal(values.TELEGRAM_BOT_TOKEN,'test-secret');
    assert.equal(values.SCAN_DAYS,180);
    assert.equal(publicSettings(values).TELEGRAM_BOT_TOKEN,undefined);
    assert.equal(publicSettings(values).tokenConfigured,true);
    await assert.rejects(saveSettings({SCAN_DAYS:366}));
    await assert.rejects(saveSettings({SCRAPER_DELAY_MS:0}));
    await applySettings();assert.equal(process.env.DROP_THRESHOLD,'40');
    await saveSettings({TELEGRAM_CHAT_ID:''});
    assert.equal((await readSettings()).TELEGRAM_CHAT_ID,'');
  }finally{for(const key of Object.keys(process.env))if(!(key in old))delete process.env[key];Object.assign(process.env,old);await rm(dir,{recursive:true,force:true});}
});
