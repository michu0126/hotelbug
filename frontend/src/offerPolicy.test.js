import test from 'node:test';
import assert from 'node:assert/strict';
import {offerTags,offerPolicyText} from './offerPolicy.js';

test('special quotes explain exclusion, not failed capture or deleted history',()=>{
  const classification={alert_eligible:false,tags:['ADVANCE_PURCHASE','PACKAGE']};
  assert.equal(offerTags(classification),'提前购买价、套餐');
  assert.match(offerPolicyText(classification),/报价和历史仍保存/);
  assert.match(offerPolicyText(classification),/不发送普通公开现金价异常提醒/);
});
test('ordinary unknown labels and legacy metadata do not claim verified eligibility',()=>{
  assert.match(offerPolicyText({alert_eligible:true,tags:[]}),/未识别到/);
  assert.match(offerPolicyText({alert_eligible:true,tags:[]}),/独立复查/);
  assert.equal(offerPolicyText(null),'方案类型尚未识别');
});
test('mixed points cash and new provider tags remain distinguishable',()=>{
  assert.equal(offerTags({tags:['POINTS_PLUS_CASH']}),'积分＋现金');
  assert.equal(offerTags({tags:['FUTURE_TAG']}),'FUTURE_TAG');
});
test('resident-only quotes remain visible with explicit restriction',()=>{
  const classification={alert_eligible:false,tags:['RESIDENT_RATE']};
  assert.equal(offerTags(classification),'居民限定价');
  assert.match(offerPolicyText(classification),/报价和历史仍保存/);
});
