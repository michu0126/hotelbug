import { mkdir, writeFile, rename, readFile } from 'node:fs/promises';
import path from 'node:path';

// Directory discovery only: never interpret directory URLs as verified room rates.
const sources = [
  ['marriott', 'https://www.marriott.com/sitemap-index.xml'],
  ['hilton', 'https://www.hilton.com/sitemap/en/sitemap-en.xml'],
  ['ihg', 'https://www.ihg.com/robots.txt'],
  ['hyatt', 'https://www.hyatt.com/robots.txt'],
  ['gha', 'https://www.ghadiscovery.com/sitemap.xml'],
];
const directory = process.env.SCRAPER_DATA_DIR || path.resolve('data');
await mkdir(directory, { recursive: true });
const catalog = { updatedAt: new Date().toISOString(), coverage: 'unverified', sources: [], hotels: [] };
const checkpointFile = path.join(directory, 'catalog-checkpoint.json');
let checkpoint = { hotels: [], groups: {} };
try { checkpoint = JSON.parse(await readFile(checkpointFile, 'utf8')); } catch (error) { if (error.code !== 'ENOENT') throw error; }
const hotels = new Map(checkpoint.hotels.map(h => [h.id, h]));
const pageBudget = Math.max(1, Math.min(1000, Number(process.env.CATALOG_PAGES_PER_GROUP) || 25));
const locations = (xml) => [...xml.matchAll(/<loc>\s*(.*?)\s*<\/loc>/gs)].map(m => m[1].replaceAll('&amp;', '&'));
for (const [group, seed] of sources) {
  const report = { group, status: 'running', pages: 0, hotels: 0, failures: [] };
  catalog.sources.push(report);
  const previous = checkpoint.groups[group];
  const queue = previous?.queue?.length ? [...previous.queue] : [seed];
  const seen = new Set(previous?.queue?.length ? previous.seen : []);
  let attempts = 0;
  while (queue.length && attempts < pageBudget) {
    const url = queue.shift();
    if (seen.has(url)) continue;
    seen.add(url);
    attempts++;
    try {
      if (new URL(url).origin !== new URL(seed).origin) throw new Error('Unexpected sitemap origin');
      const response = await fetch(url, { signal: AbortSignal.timeout(30000), redirect: 'error' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const body = await response.text();
      report.pages++;
      if (url.endsWith('robots.txt')) {
        const maps = [...body.matchAll(/^Sitemap:\s*(\S+)/gmi)].map(m => m[1]);
        if (!maps.length) throw new Error('No sitemap published in robots.txt');
        queue.push(...maps);
      } else if (/<sitemapindex\b/i.test(body)) {
        queue.push(...locations(body).filter(u => (group !== 'marriott' || /us-sitemap-hws-\d+\.xml/.test(u)) && (group !== 'hilton' || /sitemap-en-prop-/.test(u))));
      } else if (/<urlset\b/i.test(body)) {
        for (const hotelUrl of locations(body)) {
          const pathname = new URL(hotelUrl).pathname;
          let match;
          if (group === 'marriott') match = pathname.match(/^\/en-us\/hotels\/([a-z0-9]+)-([^/]+)\/overview\/?$/i);
          if (group === 'hilton') match = pathname.match(/^\/en\/hotels\/([a-z0-9]+)-([^/]+)\/?$/i);
          if (!match) continue;
          const key = `${group}:${match[1].toLowerCase()}`;
          hotels.set(key, { id: key, group, code: match[1], nameFromUrl: match[2].replaceAll('-', ' '), officialUrl: hotelUrl, sourceSitemap: url, rateStatus: 'unverified' });
        }
      } else throw new Error('Not an XML sitemap');
    } catch (error) {
      report.failures.push({ url, error: error.message });
      seen.delete(url);
      queue.push(url);
      if (/HTTP (403|429)/.test(error.message)) break;
    }
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  report.hotels = [...hotels.values()].filter(h => h.group === group).length;
  report.pendingPages = queue.length;
  report.status = report.failures.length ? 'partial_or_blocked' : queue.length ? 'partial_directory' : report.hotels ? 'directory_only' : 'needs_adapter';
  checkpoint.groups[group] = { queue, seen: [...seen] };
  checkpoint.hotels = [...hotels.values()];
  await writeFile(`${checkpointFile}.tmp`, JSON.stringify(checkpoint));
  await rename(`${checkpointFile}.tmp`, checkpointFile);
  console.log(JSON.stringify(report));
}
catalog.hotels = [...hotels.values()];
const file = path.join(directory, 'official-catalog.json');
await writeFile(`${file}.tmp`, JSON.stringify(catalog, null, 2));
await rename(`${file}.tmp`, file);
console.log(`Saved ${catalog.hotels.length} directory entries to ${file}; room rates are not verified.`);

