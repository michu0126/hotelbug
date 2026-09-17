import { chromium } from "playwright-core";
import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const root = process.cwd();
const targetsFile = process.env.SCRAPER_TARGETS_FILE || path.join(root, "config", "official-hotels.json");
const dataDir = process.env.SCRAPER_DATA_DIR || path.join(root, "data");
const debugDir = process.env.SCRAPER_DEBUG_DIR;
const historyFile = path.join(dataDir, "rate-history.json");
const scanDays = clampInt(process.env.SCAN_DAYS, 7, 1, 365);
const delayMs = clampInt(process.env.SCRAPER_DELAY_MS, 5000, 1500, 60000);
const threshold = clampInt(process.env.DROP_THRESHOLD, 35, 5, 95);
const headless = process.env.SCRAPER_HEADLESS !== "false";
const jsonOnly = process.argv.includes("--json");

function clampInt(raw, fallback, min, max) {
  const parsed = Number.parseInt(raw || "", 10);
  return Number.isFinite(parsed) ? Math.min(max, Math.max(min, parsed)) : fallback;
}

function isoDate(date) {
  return date.toISOString().slice(0, 10);
}

function addDays(date, days) {
  const next = new Date(date);
  next.setUTCDate(next.getUTCDate() + days);
  return next;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function firstExisting(paths) {
  for (const candidate of paths.filter(Boolean)) {
    try {
      await access(candidate);
      return candidate;
    } catch {}
  }
  return undefined;
}

async function resolveBrowser() {
  return firstExisting([
    process.env.CHROMIUM_PATH,
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ]);
}

function normalizeNumber(value) {
  const cleaned = value.replace(/\s/g, "").replace(/[^\d.,]/g, "");
  if (!cleaned) return null;
  const lastComma = cleaned.lastIndexOf(",");
  const lastDot = cleaned.lastIndexOf(".");
  let normalized = cleaned;
  if (lastComma > lastDot && cleaned.length - lastComma <= 3) {
    normalized = cleaned.replace(/\./g, "").replace(",", ".");
  } else {
    normalized = cleaned.replace(/,/g, "");
  }
  const amount = Number.parseFloat(normalized);
  return Number.isFinite(amount) && amount >= 20 && amount <= 100000 ? amount : null;
}

function findPrices(text, expectedCurrency) {
  const patterns = [
    { currency: "USD", regex: /(?:US\$|USD|\$)\s*([\d,.]+)/gi },
    { currency: "AUD", regex: /(?:A\$|AUD)\s*([\d,.]+)/gi },
    { currency: "EUR", regex: /(?:EUR|€)\s*([\d,.]+)/gi },
    { currency: "GBP", regex: /(?:GBP|£)\s*([\d,.]+)/gi },
    { currency: "CNY", regex: /(?:CNY|RMB|¥|￥)\s*([\d,.]+)/gi },
    { currency: expectedCurrency, regex: /([\d,.]+)\s*(?:per night|\/night|每晚)/gi },
  ];
  const matches = [];
  for (const { currency, regex } of patterns) {
    if (!currency) continue;
    for (const match of text.matchAll(regex)) {
      const amount = normalizeNumber(match[1]);
      if (amount !== null) matches.push({ amount, currency });
    }
  }
  return matches.sort((a, b) => a.amount - b.amount);
}

async function dismissCookies(page) {
  const names = [/accept all/i, /accept cookies/i, /agree/i, /同意全部/, /接受全部/];
  for (const name of names) {
    const button = page.getByRole("button", { name }).first();
    if (await button.isVisible().catch(() => false)) {
      await button.click({ timeout: 2000 }).catch(() => {});
      return;
    }
  }
}

function formatUrl(template, checkIn, checkOut) {
  return template.replaceAll("{checkIn}", checkIn).replaceAll("{checkOut}", checkOut);
}

async function scrapeTarget(page, target, checkIn, checkOut) {
  const url = formatUrl(target.urlTemplate, checkIn, checkOut);
  const startedAt = Date.now();
  const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  if (!response || response.status() >= 400) throw new Error(`HTTP ${response?.status() ?? "no-response"}`);
  await dismissCookies(page);
  await page.waitForTimeout(6000);
  const body = await page.locator("body").innerText({ timeout: 10000 });
  if (/captcha|verify you are human|access denied|unusual traffic|机器人验证/i.test(body)) {
    throw new Error("官方页面要求人机验证，已停止该目标");
  }
  if (/sold out|no rooms available|无可用客房|暂无空房/i.test(body)) {
    return { available: false, url, elapsedMs: Date.now() - startedAt };
  }
  const prices = findPrices(body, target.currency);
  if (!prices.length) {
    if (debugDir) {
      await mkdir(debugDir, { recursive: true });
      const prefix = path.join(debugDir, `${target.id}-${checkIn}`);
      await writeFile(`${prefix}.txt`, body, "utf8");
      await page.screenshot({ path: `${prefix}.png`, fullPage: true }).catch(() => {});
    }
    throw new Error("页面已打开，但未识别到可见现金价格");
  }
  const selected = prices.find((item) => item.currency === target.currency) || prices[0];
  return {
    available: true,
    price: selected.amount,
    currency: selected.currency,
    url,
    elapsedMs: Date.now() - startedAt,
  };
}

async function loadJson(file, fallback) {
  try {
    return JSON.parse(await readFile(file, "utf8"));
  } catch {
    return fallback;
  }
}

async function sendTelegram(alert) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!token || !chatId) return false;
  const text = [
    "🚨 酒店官网价格大幅下降",
    `🏨 ${alert.hotel}`,
    `📅 入住：${alert.checkIn}（1晚）`,
    `💰 当前价格：${alert.currency} ${alert.price}`,
    `📉 上次价格：${alert.previousPrice}（下降 ${alert.drop}%）`,
    alert.url,
    "请在付款前核对税费、房型与取消政策。",
  ].join("\n");
  const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
  });
  return response.ok;
}

const targets = await loadJson(targetsFile, []);
if (!Array.isArray(targets) || !targets.length) throw new Error(`没有可用目标：${targetsFile}`);
await mkdir(dataDir, { recursive: true });
const history = await loadJson(historyFile, { samples: [] });
history.samples = Array.isArray(history.samples) ? history.samples : [];
const executablePath = await resolveBrowser();
if (!executablePath) throw new Error("没有找到 Chrome/Chromium；请设置 CHROMIUM_PATH");

const browser = await chromium.launch({ executablePath, headless, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const context = await browser.newContext({
  locale: "en-US",
  userAgent: "HotelBugRateMonitor/1.0 (personal low-frequency price monitoring)",
  viewport: { width: 1365, height: 900 },
});
const page = await context.newPage();
const results = [];
const alerts = [];
const today = new Date();

try {
  for (const target of targets) {
    for (let offset = 1; offset <= scanDays; offset += 1) {
      const checkIn = isoDate(addDays(today, offset));
      const checkOut = isoDate(addDays(today, offset + 1));
      try {
        const scraped = await scrapeTarget(page, target, checkIn, checkOut);
        const record = { hotelId: target.id, hotel: target.name, group: target.group, checkIn, checkedAt: new Date().toISOString(), ...scraped };
        results.push(record);
        if (record.available) {
          const previous = [...history.samples].reverse().find((item) => item.hotelId === target.id && item.checkIn === checkIn && item.currency === record.currency && item.available);
          if (previous?.price > record.price) {
            const drop = Math.round((1 - record.price / previous.price) * 100);
            if (drop >= threshold) {
              const alert = { ...record, previousPrice: previous.price, drop };
              alert.telegramSent = await sendTelegram(alert);
              alerts.push(alert);
            }
          }
        }
        history.samples.push(record);
      } catch (error) {
        results.push({ hotelId: target.id, hotel: target.name, group: target.group, checkIn, checkedAt: new Date().toISOString(), error: error instanceof Error ? error.message : String(error) });
      }
      await sleep(delayMs);
    }
  }
} finally {
  await browser.close();
}

history.samples = history.samples.slice(-20000);
await writeFile(historyFile, `${JSON.stringify(history, null, 2)}\n`, "utf8");
const summary = { scannedAt: new Date().toISOString(), scanDays, targets: targets.length, results, alerts };
if (jsonOnly) process.stdout.write(JSON.stringify(summary));
else console.log(JSON.stringify(summary, null, 2));
