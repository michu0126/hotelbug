export const urlProviders = ['ihg', 'hilton', 'hyatt'];

export function discoveryPayload(provider, input) {
  if (!urlProviders.includes(provider)) {
    const code = input.trim().toUpperCase();
    const pattern = {accor:/^[A-Z0-9]{4}$/,marriott:/^[A-Z0-9]{5}$/,gha:/^[1-9][0-9]{0,9}$/}[provider];
    if (!pattern?.test(code)) throw new Error('雅高请输入 4 位代码，万豪 5 位代码，GHA 输入预订链接中的数字 hotelId');
    return {provider_hotel_id:code};
  }
  let url;
  try { url = new URL(input.trim()); } catch { throw new Error('请粘贴酒店官网的酒店详情链接'); }
  const valid = {
    ihg: url.hostname === 'www.ihg.com' && /^\/[a-z0-9-]+\/hotels\/[a-z]{2}\/en\/[a-z0-9-]+\/[a-z0-9]{5}\/hoteldetail\/?$/.test(url.pathname),
    hilton: url.hostname === 'www.hilton.com' && /^\/en\/hotels\/[a-z0-9-]+\/$/.test(url.pathname),
    hyatt: url.hostname === 'www.hyatt.com' && (/^\/[a-z0-9-]+\/(?:zh-CN|en-US)\/[a-zA-Z0-9]{5}-[a-zA-Z0-9-]+\/?$/.test(url.pathname) || /^\/mr-and-mrs-smith\/[a-zA-Z0-9]{5}-[a-zA-Z0-9-]+\/?$/.test(url.pathname) || /^\/(?:hotel\/(?:zh-CN|en-US)|(?:zh-CN|en-US)\/hotel)\/[a-zA-Z0-9-]+\/[a-zA-Z0-9-]+\/[a-zA-Z0-9]{5}\/?$/.test(url.pathname)),
  }[provider];
  if (url.protocol !== 'https:' || url.username || url.password || url.port || !valid || url.search || url.hash) {
    throw new Error(provider === 'hyatt' ? '请使用凯悦官网酒店详情页，不要粘贴带日期的选房或搜索链接' : '请使用官网英文酒店详情页，不要粘贴带日期的搜索或预订链接');
  }
  return {official_url:url.href};
}
