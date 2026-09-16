export async function GET() {
  return Response.json({ status: "ok", service: "ratedrop-hotel-watch" }, {
    headers: { "Cache-Control": "no-store" },
  });
}
