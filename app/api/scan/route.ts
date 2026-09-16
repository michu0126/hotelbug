export async function POST() {
  const rateApiUrl = process.env.HOTEL_RATE_API_URL;
  const rateApiKey = process.env.HOTEL_RATE_API_KEY;
  if (!rateApiUrl || !rateApiKey) {
    return Response.json({ configured: false, error: "尚未接入授权房价数据源" }, { status: 424 });
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
