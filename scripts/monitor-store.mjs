import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { writeFile, rename } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

export const dataDir = process.env.SCRAPER_DATA_DIR || path.resolve('data');
export function openStore(dir = dataDir) {
  mkdirSync(dir, { recursive: true });
  const db = new DatabaseSync(path.join(dir, 'monitor.sqlite'));
  db.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS hotels(id TEXT PRIMARY KEY, group_name TEXT, definition TEXT, last_attempt INTEGER DEFAULT 0, next_date TEXT, retry_at INTEGER DEFAULT 0, state TEXT DEFAULT 'unverified', error TEXT, checks INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS samples(hotel_id TEXT, check_in TEXT, currency TEXT, price REAL, checked_at INTEGER, url TEXT, basis TEXT, PRIMARY KEY(hotel_id,check_in,currency,basis));
    CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY, payload TEXT, created_at INTEGER, sent_at INTEGER, attempts INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS cooldowns(group_name TEXT PRIMARY KEY, until_at INTEGER, reason TEXT);
    CREATE TABLE IF NOT EXISTS leases(name TEXT PRIMARY KEY, owner TEXT, expires INTEGER);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
  `);
  if (!db.prepare('PRAGMA table_info(hotels)').all().some(c=>c.name==='next_offset')) db.exec('ALTER TABLE hotels ADD COLUMN next_offset INTEGER DEFAULT 1');
  db.prepare("UPDATE hotels SET state='unverified' WHERE group_name='gha' AND state='needs_booking_adapter'").run();
  return db;
}
export function planDate(db, job, days, today) {
  const add = offset => {const d=new Date(today+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+offset);return d.toISOString().slice(0,10);};
  // Alternate discovery with revisiting an existing hotel-date, so alerts can obtain a second quote.
  if (job.checks % 2 === 1) {
    const previous = db.prepare('SELECT check_in FROM samples WHERE hotel_id=? AND check_in>=? AND check_in<=? AND checked_at<? ORDER BY checked_at LIMIT 1').get(job.id,add(1),add(days),Date.now()-6*3600000);
    if (previous) return {checkIn:previous.check_in,nextOffset:job.next_offset};
  }
  const offset = Math.max(1,Math.min(days,job.next_offset || 1));
  return {checkIn:add(offset),nextOffset:offset % days+1};
}
export function acquireLease(db, name, ttl = 180000) {
  const owner = randomUUID(), now = Date.now();
  const r = db.prepare(`INSERT INTO leases VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires WHERE leases.expires < ?`).run(name,owner,now+ttl,now);
  if (!r.changes) return null;
  const timer = setInterval(() => db.prepare('UPDATE leases SET expires=? WHERE name=? AND owner=?').run(Date.now()+ttl,name,owner),30000);
  timer.unref();
  return () => {clearInterval(timer);db.prepare('DELETE FROM leases WHERE name=? AND owner=?').run(name,owner);};
}
export function importHotels(db, hotels) {
  const insert = db.prepare(`INSERT INTO hotels(id,group_name,definition,state) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET definition=excluded.definition`);
  db.exec('BEGIN');
  try { for (const h of hotels) insert.run(h.id,h.group,JSON.stringify(h),'unverified'); db.exec('COMMIT'); } catch(e) {db.exec('ROLLBACK');throw e;}
}
export function selectHotel(db, now = Date.now()) {
  return db.prepare(`SELECT * FROM hotels WHERE retry_at<=? AND NOT EXISTS(SELECT 1 FROM cooldowns c WHERE c.group_name=hotels.group_name AND c.until_at>?) ORDER BY last_attempt,id LIMIT 1`).get(now,now);
}
export function saveSample(db, hotel, record, threshold) {
  const basis = record.basis || 'lowest-visible-nightly-2-adults-1-room-v1';
  const prior = db.prepare('SELECT * FROM samples WHERE hotel_id=? AND check_in=? AND currency=? AND basis=?').get(hotel.id,record.checkIn,record.currency,basis);
  db.exec('BEGIN');
  try {
    db.prepare('INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?)').run(hotel.id,record.checkIn,record.currency,record.price,Date.now(),record.url,basis);
    if (prior && record.price < prior.price && Date.now()-prior.checked_at < 30*86400000) {
      const drop = Math.floor((1-record.price/prior.price)*100);
      if (drop >= threshold) {
        const id = `${hotel.id}|${record.checkIn}|${record.currency}|${prior.price}|${record.price}`;
        const payload = {...record, hotel:hotel.name || hotel.nameFromUrl || hotel.id, hotelId:hotel.id, group:hotel.group, previousPrice:prior.price,drop,checkedAt:new Date().toISOString(),basis};
        db.prepare('INSERT OR IGNORE INTO alerts(id,payload,created_at) VALUES(?,?,?)').run(id,JSON.stringify(payload),Date.now());
      }
    }
    db.exec('COMMIT');
  } catch(e) {db.exec('ROLLBACK');throw e;}
}
export async function writeStatus(db, extra = {}) {
  const now = Date.now(), today = new Date().toISOString().slice(0,10);
  const groups = db.prepare(`SELECT group_name AS name, COUNT(*) AS hotels, SUM(checks) AS checks, SUM(state='ok') AS successfulHotels, SUM(state='error') AS failedHotels, SUM(state='needs_booking_adapter') AS unsupportedHotels FROM hotels GROUP BY group_name`).all();
  const samples = db.prepare('SELECT COUNT(*) AS count FROM samples WHERE check_in>=? AND checked_at>?').get(today,now-86400000).count;
  const latest = db.prepare('SELECT s.*,h.definition FROM samples s JOIN hotels h ON h.id=s.hotel_id WHERE check_in>=? ORDER BY checked_at DESC LIMIT 30').all(today).map(row => ({hotel:JSON.parse(row.definition).name || JSON.parse(row.definition).nameFromUrl,checkIn:row.check_in,basis:row.basis,currency:row.currency,price:row.price,checkedAt:new Date(row.checked_at).toISOString(),url:row.url}));
  const alerts = db.prepare('SELECT payload,sent_at,attempts FROM alerts ORDER BY created_at DESC LIMIT 30').all().map(a=>({...JSON.parse(a.payload),telegramSent:!!a.sent_at,attempts:a.attempts}));
  const errors = db.prepare('SELECT id,group_name,error,last_attempt FROM hotels WHERE error IS NOT NULL ORDER BY last_attempt DESC LIMIT 10').all();
  const workerLease = db.prepare("SELECT expires FROM leases WHERE name='rates'").get();
  const status = {updatedAt:new Date().toISOString(),windowDays:Number(process.env.SCAN_DAYS || 365),groups,freshHotelDates:samples,latest,alerts,errors,cooldowns:db.prepare('SELECT * FROM cooldowns WHERE until_at>?').all(now),workerRunning:!!workerLease && workerLease.expires>now,...extra};
  const file = path.join(dataDir,'monitor-status.json');
  await writeFile(`${file}.tmp`,JSON.stringify(status));await rename(`${file}.tmp`,file);
}
