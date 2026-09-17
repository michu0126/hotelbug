import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";

export const runtime = "nodejs";

let activeScan: ChildProcess | undefined;
let lastScanStartedAt = 0;

export async function POST() {
  const rateApiUrl = process.env.HOTEL_RATE_API_URL;
  const rateApiKey = process.env.HOTEL_RATE_API_KEY;
  if (!rateApiUrl || !rateApiKey) {
    const targetsFile = process.env.SCRAPER_TARGETS_FILE;
    if (!targetsFile) {
      return Response.json({ configured: false, error: "尚未配置官网价格抓取目标" }, { status: 424 });
    }
    if (activeScan && activeScan.exitCode === null) {
      return Response.json({ configured: true, mode: "official-scraper", accepted: false, error: "扫描任务正在运行" }, { status: 409 });
    }
    if (Date.now() - lastScanStartedAt < 5 * 60 * 1000) {
      return Response.json({ configured: true, mode: "official-scraper", accepted: false, error: "为保护官网，请至少间隔 5 分钟再次扫描" }, { status: 429 });
    }
    const script = path.join(process.cwd(), "scripts", "scrape-official-rates.mjs");
    activeScan = spawn(process.execPath, [script], {
      cwd: process.cwd(),
      env: process.env,
      stdio: "inherit",
    });
    lastScanStartedAt = Date.now();
    activeScan.once("exit", () => { activeScan = undefined; });
    activeScan.once("error", () => { activeScan = undefined; });
    return Response.json({ configured: true, mode: "official-scraper", accepted: true }, { status: 202 });
  }
  const today = new Date();
  const end = new Date(today);
  end.setUTCDate(end.getUTCDate() + 365);
  const response = await fetch(rateApiUrl, {
    method: "POST",
    headers: { "Authorization": `Bearer ${rateApiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      groups: ["marriott", "ihg", "hilton", "hyatt", "gha"],
      dateFrom: today.toISOString().slice(0, 10),
      dateTo: end.toISOString().slice(0, 10),
      refundableOnly: true,
      includeTaxes: true,
    }),
  });
  if (!response.ok) {
    console.error("Rate source scan failed", response.status);
    return Response.json({ configured: true, error: "房价数据源请求失败" }, { status: 502 });
  }
  const result = await response.json();
  return Response.json({ configured: true, result });
}
