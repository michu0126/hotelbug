export const sources = [
  ['marriott', 'https://www.marriott.com/sitemap-index.xml'],
  ['hilton', 'https://www.hilton.com/sitemap/en/sitemap-en.xml'],
  ['ihg', 'https://www.ihg.com/robots.txt'],
  ['hyatt', 'https://www.hyatt.com/robots.txt'],
  ['gha', 'https://www.ghadiscovery.com/sitemap.xml'],
];
export const decodeXml = text => text.replaceAll('&amp;', '&').replaceAll('&quot;', '"').replaceAll('&apos;', "'").replaceAll('&lt;', '<').replaceAll('&gt;', '>');
export const locations = xml => [...xml.matchAll(/<loc>\s*(.*?)\s*<\/loc>/gs)].map(m => decodeXml(m[1]));
export function acceptSitemap(group, url) {
  if (url.endsWith('robots.txt')) return true;
  if (group === 'marriott') return /sitemap-index\.xml|us-sitemap-hws-\d+\.xml/.test(url);
  if (group === 'hilton') return /sitemap-en\.xml|sitemap-en-prop-/.test(url);
  if (group === 'ihg') return /sitemap-?index\.xml|sitemap\.[\w-]+\.en-us\.hoteldetail\.xml/.test(url);
  return true;
}
export function hotelFromUrl(group, url, sourceSitemap) {
  if (new URL(url).hostname !== new URL(sources.find(s => s[0] === group)[1]).hostname) return null;
  const p = new URL(url).pathname;
  let match, name, code;
  if (group === 'marriott') { match = p.match(/^\/en-us\/hotels\/([a-z0-9]+)-([^/]+)\/overview\/?$/i); if (match) [, code, name] = match; }
  if (group === 'hilton') { match = p.match(/^\/en\/hotels\/([a-z0-9]+)-([^/]+)\/?$/i); if (match) [, code, name] = match; }
  if (group === 'ihg') { match = p.match(/^\/([^/]+)\/hotels\/us\/en\/([^/]+)\/([a-z0-9]{5})\/hoteldetail\/?$/i); if (match) {code = match[3]; name = `${match[1]} ${match[2]} (${code})`; } }
  if (group === 'hyatt') { match = p.match(/^\/(?:[a-z-]+)\/en-US\/([a-z0-9]{5})-([^/]+)\/?$/i); if (match) [, code, name] = match; }
  if (!code) return null;
  return { id: `${group}:${code.toLowerCase()}`, group, code: code.toUpperCase(), name: name.replaceAll('-', ' '), nameVerified: false, officialUrl: url, sourceSitemap, rateStatus: 'unverified' };
}
export function ghaCandidate(url) {
  const p = new URL(url).pathname;
  return /^\/[^/]+\/[^/]+\/?$/.test(p) && !/^\/(?:search|member|our-partners|support|press|destinations|member-survey|promotions|media|Media|terms-conditions|privacy-policy|images|var|content)\//.test(p);
}
export function ghaFromHtml(html, url) {
  const raw = html.match(/<script id="__NEXT_DATA__"[^>]*>(.*?)<\/script>/s)?.[1];
  if (!raw) return null;
  const page = JSON.parse(raw)?.props?.pageProps?.page;
  if (page?.type !== 'hotel') return null;
  return { id: `gha:${page._info.id}`, code: String(page._info.id), group: 'gha', name: page.name || page.title || new URL(url).pathname.split('/').at(-1), nameVerified: true, officialUrl: url, rateStatus: 'needs_booking_adapter' };
}
export function rateUrl(hotel, arrival, departure) {
  const code = encodeURIComponent(hotel.code);
  const us = iso => encodeURIComponent(`${iso.slice(5,7)}/${iso.slice(8,10)}/${iso.slice(0,4)}`);
  if (hotel.group === 'marriott') return `https://www.marriott.com/en-us/reservation/availability.mi?propertyCode=${code}&fromDate=${us(arrival)}&toDate=${us(departure)}&useRequestCriteria=true&isSearch=false&roomCount=1&numAdultsPerRoom=2&childrenCount=0&useRewardsPoints=false`;
  if (hotel.group === 'hilton') return `https://www.hilton.com/en/book/reservation/rooms/?ctyhocn=${code}&arrivalDate=${arrival}&departureDate=${departure}&room1NumAdults=2&room1NumChildren=0`;
  if (hotel.group === 'hyatt') return `https://www.hyatt.com/shop/rooms/${code.toLowerCase()}?checkinDate=${arrival}&checkoutDate=${departure}&rooms=1&adults=2&kids=0`;
  if (hotel.group === 'ihg') return `https://www.ihg.com/redirect?path=rates&brandCode=6c&localeCode=en&regionCode=1&hotelCode=${code}&checkInDate=${arrival.slice(8)}&checkInMonthYear=${arrival.slice(5,7)}${arrival.slice(0,4)}&checkOutDate=${departure.slice(8)}&checkOutMonthYear=${departure.slice(5,7)}${departure.slice(0,4)}&numberOfRooms=1&numberOfAdults=2&numberOfChildren=0`;
  return null;
}

export function nightlyPrices(text) {
  // Require the nightly unit next to an explicit ISO currency; do not guess a dollar currency.
  const currencies = new Set(Intl.supportedValuesOf('currency'));
  const pattern = /(?:\b([A-Z]{3})[ \t\r\n]+([\d]+(?:[,.][\d]+)*)|([\d]+(?:[,.][\d]+)*)[ \t\r\n]+([A-Z]{3}))[ \t\r\n]*(?:per night|\/night|每晚)\b/gi;
  const found = [];
  for (const m of text.matchAll(pattern)) {
    const currency = (m[1] || m[4]).toUpperCase();
    if (!currencies.has(currency)) continue;
    const value = m[2] || m[3];
    const commaDecimal = /,\d{1,2}$/.test(value) && value.lastIndexOf(',') > value.lastIndexOf('.');
    const price = Number(commaDecimal ? value.replaceAll('.', '').replace(',', '.') : value.replaceAll(',', ''));
    if (price > 0 && Number.isFinite(price)) found.push({ price, currency });
  }
  return found.sort((a,b) => a.price - b.price);
}

export function datesPresent(text, arrival, departure) {
  const has = iso => {
    const d = new Date(`${iso}T12:00:00Z`), month = d.toLocaleString('en-US', {month:'short',timeZone:'UTC'}), full = d.toLocaleString('en-US', {month:'long',timeZone:'UTC'}), day = d.getUTCDate(), year = d.getUTCFullYear();
    const variants = [iso, `${iso.slice(5,7)}/${iso.slice(8)}/${year}`, `${month} ${day}, ${year}`, `${month} ${day} ${year}`, `${day} ${month} ${year}`, `${full} ${day}, ${year}`, `${day} ${full} ${year}`];
    return variants.some(v => text.toLowerCase().includes(v.toLowerCase()));
  };
  return has(arrival) && has(departure);
}

