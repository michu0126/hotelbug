export function countLabel(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '—';
}

export function priceCoveragePercent(coverage) {
  if (!coverage || !Number.isSafeInteger(coverage.required_stays) || coverage.required_stays <= 0 ||
      !Number.isSafeInteger(coverage.fresh_stays) || coverage.fresh_stays < 0 ||
      coverage.fresh_stays > coverage.required_stays) return '—';
  const percent = coverage.fresh_stays / coverage.required_stays * 100;
  if (percent > 0 && percent < 0.1) return '<0.1%';
  // Do not round almost-complete coverage into a false 100%.
  return percent === 100 ? '100%' : `${Math.floor(percent * 10) / 10}%`;
}
