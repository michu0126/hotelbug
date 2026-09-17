import { mkdir, writeFile, rename } from 'node:fs/promises';
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
const hotels = new Map();
const locations = (xml) => [...xml.matchAll(/<loc>\s*(.*?)\s*<\/loc>/gs)].map(m => m[1].replaceAll('&amp;', '&'));
for (const [group, seed] of sources) {
  const report = { group, status: 'running', pages: 0, hotels: 0, failures: [] };
  catalog.sources.push(report);
  const queue = [seed];
  const seen = new Set();
  while (queue.length) {
    const url = queue.shift();
    if (seen.has(url)) continue;
    seen.add(url);
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
        queue.push(...locations(body).filter(u => group !== 'marriott' || /us-sitemap-hws-\d+\.xml/.test(u)));
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
      if (/HTTP (403|429)/.test(error.message)) break;
    }
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  report.hotels = [...hotels.values()].filter(h => h.group === group).length;
  report.status = report.failures.length ? 'partial_or_blocked' : report.hotels ? 'directory_only' : 'needs_adapter';
  console.log(JSON.stringify(report));
}
catalog.hotels = [...hotels.values()];
const file = path.join(directory, 'official-catalog.json');
await writeFile(`${file}.tmp`, JSON.stringify(catalog, null, 2));
await rename(`${file}.tmp`, file);
console.log(`Saved ${catalog.hotels.length} directory entries to ${file}; room rates are not verified.`);

