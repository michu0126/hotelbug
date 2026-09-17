export async function GET() {
  return Response.json({
    telegramConfigured: Boolean(process.env.TELEGRAM_BOT_TOKEN && process.env.TELEGRAM_CHAT_ID),
    rateSourceConfigured: Boolean(
      (process.env.HOTEL_RATE_API_URL && process.env.HOTEL_RATE_API_KEY) ||
      process.env.SCRAPER_TARGETS_FILE
    ),
    monitoringWindowDays: 365,
  }, { headers: { "Cache-Control": "no-store" } });
}
