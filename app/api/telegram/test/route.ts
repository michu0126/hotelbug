type TestAlert = { hotel?: string; checkIn?: string; nights?: number; price?: number; drop?: number };

function escapeHtml(value: string) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

export async function POST(request: Request) {
  const botToken = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;
  if (!botToken || !chatId) {
    return Response.json({ error: "Telegram 机器人尚未配置" }, { status: 424 });
  }
  const alert = (await request.json().catch(() => ({}))) as TestAlert;
  if (!alert.hotel || !alert.checkIn || !Number.isFinite(alert.price) || !Number.isFinite(alert.drop)) {
    return Response.json({ error: "提醒内容不完整" }, { status: 400 });
  }
  const text = [
    "🚨 <b>RateDrop 酒店降价提醒</b>",
    "",
    `🏨 <b>${escapeHtml(alert.hotel)}</b>`,
    `📅 入住：${escapeHtml(alert.checkIn)} · ${alert.nights ?? 1} 晚`,
    `💰 当前含税价：¥${Math.round(alert.price!).toLocaleString("zh-CN")}`,
    `📉 相比基准下降：${Math.round(alert.drop!)}%`,
    "",
    "请在预订前核对房型、取消政策与最终税费。",
  ].join("\n");
  const response = await fetch(`https://api.telegram.org/bot${botToken}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
  if (!response.ok) {
    console.error("Telegram send failed", response.status, await response.text());
    return Response.json({ error: "Telegram 发送失败，请检查机器人配置" }, { status: 502 });
  }
  return Response.json({ ok: true });
}
