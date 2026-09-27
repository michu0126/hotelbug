import test from 'node:test';
import assert from 'node:assert/strict';
import {parseGhaPage} from './gha-page.mjs';
import {mkdtemp,rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {openStore,importHotels,selectHotel,saveSample} from './monitor-store.mjs';
const body='MEMBER RATES FROM\nAED 405\nNON-MEMBER RATES\nAED 450\nExcluding taxes and fees\nVIEW RATES\nDATESWed, 07 Oct - Thu, 08 Oct 2026ROOM 1: 2 ADULTS';
test('GHA visible page parses nonmember quote only after date confirmation',()=>{
  const q=parseGhaPage(body,'2026-10-07','2026-10-08');assert.equal(q.price,450);assert.equal(q.currency,'AED');
  assert.throws(()=>parseGhaPage(body,'2026-10-08','2026-10-09'));
  assert.throws(()=>parseGhaPage(body.replace('2 ADULTS','3 ADULTS'),'2026-10-07','2026-10-08'));
  assert.throws(()=>parseGhaPage(body.replace('Excluding taxes and fees','Including taxes'),'2026-10-07','2026-10-08'));
  assert.throws(()=>parseGhaPage(body.replace('2026','2027'),'2026-10-07','2026-10-08'));
});
test('GHA joins automatic queue and quote bases never cross-compare',async()=>{
 const dir=await mkdtemp(path.join(os.tmpdir(),'hotelbug-gha-'));const db=openStore(dir);
 try{const hotel={id:'gha:test',group:'gha',name:'Test'};importHotels(db,[hotel]);assert.equal(selectHotel(db).id,hotel.id);
 const q={checkIn:'2026-10-07',currency:'AED',price:900,url:'https://www.ghadiscovery.com/'};
 saveSample(db,hotel,q,35);saveSample(db,hotel,{...q,price:450,basis:'gha-visible-nonmember-excluding-taxes-1night-2adults-v1'},35);
 assert.equal(db.prepare('SELECT COUNT(*) AS n FROM alerts').get().n,0);
 }finally{db.close();await rm(dir,{recursive:true});}
});
