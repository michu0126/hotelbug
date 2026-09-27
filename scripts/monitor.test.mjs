import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { hotelFromUrl, rateUrl, acceptSitemap, nightlyPrices, datesPresent, ghaFromHtml } from './official-adapters.mjs';
import { openStore, acquireLease, importHotels, selectHotel, planDate, saveSample } from './monitor-store.mjs';

test('hotel IDs deduplicate regions, and date URLs survive year boundaries',()=>{
  const h=hotelFromUrl('ihg','https://www.ihg.com/intercontinental/hotels/us/en/new-york/nycha/hoteldetail','sitemap');
  assert.equal(h.id,'ihg:nycha');
  assert.equal(hotelFromUrl('ihg','https://other.example/intercontinental/hotels/us/en/new-york/nycha/hoteldetail','sitemap'),null);
  const u=new URL(rateUrl(h,'2026-12-31','2027-01-01'));
  assert.equal(u.searchParams.get('checkInMonthYear'),'122026');
  assert.equal(u.searchParams.get('checkOutMonthYear'),'012027');
  assert(acceptSitemap('ihg','https://www.ihg.com/bin/sitemapindex.xml'));
  assert(!acceptSitemap('ihg','https://www.ihg.com/bin/sitemap.ihg.zh-cn.hoteldetail.xml'));
});
test('nightly quotes require ISO currency and do not treat points or parking as rates',()=>{
  assert.deepEqual(nightlyPrices('Parking USD 25. From 9,000 points. 446\nUSD\nper night'),[{price:446,currency:'USD'}]);
  assert.deepEqual(nightlyPrices('$123 per night'),[]);
  assert.deepEqual(nightlyPrices('AUD 12.50 per night'),[{price:12.5,currency:'AUD'}]);
  assert.deepEqual(nightlyPrices('1.234,56 EUR per night'),[{price:1234.56,currency:'EUR'}]);
  assert(!datesPresent('Sep 28, 2026 to Sep 29, 2026','2026-09-27','2026-09-28'));
  assert(datesPresent('2026-09-27 2026-09-28','2026-09-27','2026-09-28'));
});
test('GHA validates page type before adding hotels',()=>{
  const html='<script id="__NEXT_DATA__">'+JSON.stringify({props:{pageProps:{page:{type:'landing_page',name:'Offers'}}}})+'</script>';
  assert.equal(ghaFromHtml(html,'https://www.ghadiscovery.com/our-partners/test'),null);
});
test('queue, group cooldown and deduplicated alerts survive restart',async()=>{
  const dir=await mkdtemp(path.join(os.tmpdir(),'hotelbug-test-'));
  let db=openStore(dir),release;
  try{
    const h={id:'ihg:nycha',group:'ihg',code:'NYCHA',name:'Test hotel'};
    importHotels(db,[h]);
    release=acquireLease(db,'rates');assert(release);assert.equal(acquireLease(db,'rates'),null);
    assert.equal(selectHotel(db).id,h.id);
    assert.deepEqual(planDate(db,selectHotel(db),365,'2026-12-31'),{checkIn:'2027-01-01',nextOffset:2});
    db.prepare('INSERT INTO cooldowns VALUES(?,?,?)').run('ihg',Date.now()+100000,'HTTP 429');
    assert.equal(selectHotel(db),undefined);
    const r={checkIn:'2027-01-01',currency:'USD',price:100,url:'https://www.ihg.com/'};
    saveSample(db,h,r,35);saveSample(db,h,{...r,price:50},35);saveSample(db,h,{...r,price:50},35);
    assert.equal(db.prepare('SELECT COUNT(*) AS n FROM alerts').get().n,1);
    db.prepare('UPDATE samples SET checked_at=?').run(Date.now()-7*3600000);
    assert.equal(planDate(db,{...h,checks:1,next_offset:20},365,'2026-12-31').checkIn,'2027-01-01');
    release();release=null;db.close();db=openStore(dir);
    assert.equal(db.prepare('SELECT price FROM samples').get().price,50);
    assert.equal(selectHotel(db),undefined);
  }finally{if(release)release();db.close();await rm(dir,{recursive:true});}
});

