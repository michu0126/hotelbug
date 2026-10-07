import test from 'node:test';
import assert from 'node:assert/strict';
import {discoveryPayload, urlProviders} from './hotelDiscovery.js';

const hyatt = 'https://www.hyatt.com/caption-by-hyatt/zh-CN/shacp-caption-by-hyatt-zhongshan-park-shanghai';
test('Hyatt Chinese official detail page is accepted without retaining booking parameters', () => {
  assert.ok(urlProviders.includes('hyatt'));
  assert.deepEqual(discoveryPayload('hyatt', '  ' + hyatt + '  '), {official_url:hyatt});
});
for (const invalid of [hyatt.replace('zh-CN', 'de-DE'), hyatt.replace('www.hyatt.com', 'example.com'), hyatt + '?token=private', hyatt + '#rooms', hyatt.replace('https:', 'http:'), hyatt.replace('www.hyatt.com', 'user:pass@www.hyatt.com'), hyatt.replace('www.hyatt.com', 'www.hyatt.com:8443'), 'https://www.hyatt.com/zh-CN/shop/rooms/shacp?checkinDate=2026-10-06']) {
  test('reject unsupported detail URL: ' + invalid, () => assert.throws(() => discoveryPayload('hyatt', invalid), /凯悦官网/));
}
test('Worldwide directory modern, legacy and partner detail paths are accepted',()=>{
  for(const url of [hyatt.replace('zh-CN','en-US'),'https://www.hyatt.com/hotel/zh-CN/china/grand-hyatt-kunming/kmggh','https://www.hyatt.com/zh-CN/hotel/florida/the-standard-spa-miami-beach/miasm','https://www.hyatt.com/mr-and-mrs-smith/m1885-pemako-punakha']) assert.deepEqual(discoveryPayload('hyatt',url),{official_url:url});
});
test('Existing provider detail and code payloads remain compatible', () => {
  assert.deepEqual(discoveryPayload('ihg', 'https://www.ihg.com/hotelindigo/hotels/us/en/london/lonls/hoteldetail'), {official_url:'https://www.ihg.com/hotelindigo/hotels/us/en/london/lonls/hoteldetail'});
  assert.deepEqual(discoveryPayload('hilton', 'https://www.hilton.com/en/hotels/mlehici-conrad-maldives-rangali-island/'), {official_url:'https://www.hilton.com/en/hotels/mlehici-conrad-maldives-rangali-island/'});
  assert.deepEqual(discoveryPayload('accor', '0338'), {provider_hotel_id:'0338'});
  assert.deepEqual(discoveryPayload('marriott', 'nycmq'), {provider_hotel_id:'NYCMQ'});
  assert.deepEqual(discoveryPayload('gha', '10624'), {provider_hotel_id:'10624'});
});
