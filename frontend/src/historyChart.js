// Chart-only numeric conversion; all financial amounts stay decimal strings in the API/table.
export function historyChart(trend) {
  const dayMs = 86400000;
  const start = Date.parse(trend?.window_start?.slice(0, 10));
  const end = Date.parse(trend?.window_end?.slice(0, 10));
  const rows = (trend?.points || []).map(p => ({...p, time: Date.parse(p.date), lowValue: Number(p.low), highValue: Number(p.high)}))
    .filter(p => p.low != null && p.high != null && Number.isFinite(p.time) && Number.isFinite(p.lowValue) && Number.isFinite(p.highValue) && p.lowValue > 0 && p.highValue >= p.lowValue)
    .sort((a, b) => a.time - b.time);
  if (!rows.length || !Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  const minimum = Math.min(...rows.map(p => p.lowValue));
  const maximum = Math.max(...rows.map(p => p.highValue));
  const pad = Math.max((maximum - minimum) * .12, maximum * .02, .01);
  const floor = Math.max(0, minimum - pad), ceiling = maximum + pad;
  const x = time => 65 + (time - start) / Math.max(end - start, dayMs) * 555;
  const y = value => 18 + (ceiling - value) / (ceiling - floor) * 174;
  const points = rows.map(p => ({...p, x: x(p.time), yLow: y(p.lowValue), yHigh: y(p.highValue)}));
  const segments = [];
  for (const point of points) {
    if (!segments.length || point.time - segments.at(-1).at(-1).time > dayMs) segments.push([]);
    segments.at(-1).push(point);
  }
  return {
    points, segments: segments.filter(s => s.length > 1).map(s => ({
      low: s.map(p => `${p.x},${p.yLow}`).join(' '), high: s.map(p => `${p.x},${p.yHigh}`).join(' '),
    })),
    ticks: [ceiling, (ceiling + floor) / 2, floor].map(value => ({value, y: y(value)})),
    startLabel: trend.window_start.slice(0, 10), endLabel: trend.window_end.slice(0, 10),
  };
}
