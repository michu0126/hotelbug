const labels={MEMBER_RATE:'会员价',CORPORATE_RATE:'公司协议价',RESIDENT_RATE:'居民限定价',ADVANCE_PURCHASE:'提前购买价',PACKAGE:'套餐',POINTS_PLUS_CASH:'积分＋现金',POINTS_ONLY:'积分价',UNIDENTIFIED_OFFER:'房型或方案未确认'};
export const offerTags=classification=>(classification?.tags||[]).map(tag=>labels[tag]||tag).join('、');
export function offerPolicyText(classification){
  if(!classification)return '方案类型尚未识别';
  if(classification.alert_eligible)return '未识别到需排除的特殊方案；满足历史降幅并独立复查后可提醒。';
  return `已识别：${offerTags(classification)}。报价和历史仍保存，仅不发送普通公开现金价异常提醒。`;
}
