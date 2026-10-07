import test from 'node:test';
import assert from 'node:assert/strict';
import {watchPayload,watchTarget} from './watchForm.js';

test('wide scopes preserve official spelling and horizon, optional provider narrows only',()=>{
  assert.deepEqual(watchPayload('city',' Straße ','marriott',365,' My city '),{scope:'city',city:'Straße',provider:'marriott',days_ahead:365,name:'My city'});
  assert.deepEqual(watchPayload('provider','hyatt','marriott',90),{scope:'provider',provider:'hyatt',days_ahead:90});
  assert.deepEqual(watchPayload('country','CN','',30),{scope:'country',country:'CN',days_ahead:30});
  assert.deepEqual(watchPayload('brand','Park Hyatt','hyatt',365),{scope:'brand',brand:'Park Hyatt',provider:'hyatt',days_ahead:365});
});
for(const days of [0,366,1.5,'oops'])test('reject invalid monitoring horizon '+days,()=>assert.throws(()=>watchPayload('city','Shanghai','',days)));
test('missing target and invalid scopes rejected',()=>{
  assert.throws(()=>watchPayload('city',' ','',365));
  assert.throws(()=>watchPayload('unknown','Shanghai','',365));
  assert.throws(()=>watchPayload('city','x'.repeat(101),'',365));
});
test('list target labels retain explicit metadata or group name',()=>{
  assert.equal(watchTarget({scope:'provider',filters:{provider:'hyatt'}},{hyatt:'凯悦'}),'凯悦');
  assert.equal(watchTarget({scope:'city',filters:{city:'Shanghai'}}),'Shanghai');
  assert.equal(watchTarget({scope:'hotel',filters:{hotel_id:'hotel-id'}}),'hotel-id');
});
