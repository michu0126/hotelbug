export const scopeNames = {hotel:'单酒店',city:'城市',country:'国家',brand:'品牌',provider:'酒店集团'};
export function watchPayload(scope, target, provider, days, name = '') {
  if (!['city','country','brand','provider'].includes(scope)) throw new Error('请选择监控范围');
  const value = String(target || '').trim();
  if (!value) throw new Error('请填写或选择监控目标');
  if (value.length > 100) throw new Error('监控目标最长 100 字符');
  const horizon = Number(days);
  if (!Number.isInteger(horizon) || horizon < 1 || horizon > 365) throw new Error('监控天数必须在 1–365 之间');
  const result = {scope, [scope]:value, days_ahead:horizon};
  if (scope !== 'provider' && provider) result.provider = provider;
  if (name.trim()) result.name = name.trim();
  return result;
}
export function watchTarget(watch, names = {}) {
  const value = watch.filters?.[watch.scope === 'hotel' ? 'hotel_id' : watch.scope];
  return watch.scope === 'provider' ? (names[value] || value) : (value || '目标缺失');
}
