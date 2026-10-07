import {test} from 'node:test';
import assert from 'node:assert/strict';
import {historyChart} from './historyChart.js';

const trend=points=>({window_start:'2026-09-04T12:00:00Z',window_end:'2026-10-04T12:00:00Z',points});
test('empty windows never render a zero price',()=>{
  assert.equal(historyChart(trend([])),null);
  assert.equal(historyChart(null),null);
});
test('same daily amount has finite coordinates and one point, not a made-up curve',()=>{
  const chart=historyChart(trend([{date:'2026-10-03',low:'100.1234',high:'100.1234',samples:2}]));
  assert.equal(chart.points.length,1);
  assert.equal(chart.segments.length,0);
  assert.ok(Number.isFinite(chart.points[0].yLow));
  assert.equal(chart.points[0].low,'100.1234');
});
test('missing days break both lines and do not create samples',()=>{
  const chart=historyChart(trend([
    {date:'2026-10-04',low:'70',high:'80'}, {date:'2026-10-01',low:'100',high:'120'},
    {date:'2026-10-02',low:'90',high:'110'},
  ]));
  assert.equal(chart.points.length,3);
  assert.equal(chart.segments.length,1);
  assert.equal(chart.segments[0].low.split(' ').length,2);
  assert.deepEqual(chart.points.map(p=>p.date),['2026-10-01','2026-10-02','2026-10-04']);
  assert.ok(chart.points[2].x-chart.points[1].x>chart.points[1].x-chart.points[0].x);
});
test('invalid or missing cash amounts are excluded',()=>{
  assert.equal(historyChart(trend([
    {date:'2026-10-01',low:null,high:null}, {date:'2026-10-02',low:'0',high:'0'},
    {date:'invalid',low:'100',high:'100'}, {date:'2026-10-03',low:'100',high:'90'},
  ])),null);
});
