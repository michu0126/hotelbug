import { readFile } from "node:fs/promises";
import path from "node:path";
import { readSettings } from "@/scripts/settings.mjs";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

async function read(name: string) {
  try { return JSON.parse(await readFile(/* turbopackIgnore: true */ path.join(/* turbopackIgnore: true */ process.env.SCRAPER_DATA_DIR || path.resolve("data"), name), "utf8")); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return null; throw error; }
}

export async function GET() {
  const settings=await readSettings();
  const [monitor, catalog] = await Promise.all([read("monitor-status.json"),read("official-catalog.json")]);
  if (monitor) monitor.workerRunning = Boolean(monitor.workerRunning && Date.now()-Date.parse(monitor.updatedAt)<120000);
  return Response.json({
    telegramConfigured: Boolean(settings.TELEGRAM_BOT_TOKEN && settings.TELEGRAM_CHAT_ID),
    rateSourceConfigured: Boolean(
      (process.env.HOTEL_RATE_API_URL && process.env.HOTEL_RATE_API_KEY) ||
      process.env.SCRAPER_TARGETS_FILE
    ),
    monitoringWindowDays: Number(settings.SCAN_DAYS),
    threshold: Number(settings.DROP_THRESHOLD),
    monitor,
    catalog: catalog ? { updatedAt: catalog.updatedAt, count: catalog.hotels.length, sources: catalog.sources } : null,
  }, { headers: { "Cache-Control": "no-store" } });
}
