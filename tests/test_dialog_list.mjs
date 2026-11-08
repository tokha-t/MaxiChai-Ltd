import test from 'node:test';
import assert from 'node:assert/strict';
import { readDialogFilters, dialogListHash, sortDialogRows, summarizeDialogRows, visibleDialogRows, dialogScopeCount } from '../web/js/dialog-list-model.js';
const row = (id, score, date = '2026-11-08T01:00:00Z', status = 'ok') => ({ conversation_id: id, evaluation: { status, final_score: score, evaluated_at: date } });

test('numeric sorting preserves decimal/zero scores and keeps missing/error scores last in either direction', () => {
  const rows = [row('hundred',100), row('decimal',93.75), row('ten',10), row('zero',0), row('na',null), row('error',99,null,'error'), {conversation_id:'unscored'}];
  assert.deepEqual(sortDialogRows(rows,'score_asc').slice(0,4).map(r=>r.conversation_id), ['zero','ten','decimal','hundred']);
  assert.deepEqual(sortDialogRows(rows,'score_desc').slice(0,4).map(r=>r.conversation_id), ['hundred','decimal','ten','zero']);
  assert.equal(rows[0].conversation_id,'hundred');
});
test('dates sort by timestamp, not date string, and missing dates stay last', () => {
  const rows = [row('later',1,'2026-11-08T01:00:00Z'), row('earlier',2,'2026-11-08T04:00:00+05:00'), row('missing',3,null)];
  assert.deepEqual(sortDialogRows(rows,'date_asc').map(r=>r.conversation_id), ['earlier','later','missing']);
  assert.deepEqual(sortDialogRows(rows,'date_desc').map(r=>r.conversation_id), ['later','earlier','missing']);
});
test('average uses numeric successful scores, counts missing/error/all-N-A separately', () => {
  assert.deepEqual(summarizeDialogRows([row('a',100),row('b',null),row('c',0),row('d',50,null,'error'),{conversation_id:'e'}]), { loaded:5,evaluated:3,scored:2,failed:1,unevaluated:1,average:50 });
  assert.equal(summarizeDialogRows([row('na',null)]).average,null);
});
test('explicit drilldown clears stale filters; canonical hash round trips identifiers safely', () => {
  const filters = readDialogFilters(new URLSearchParams('group_id=A%26B&criterion_id=contact&criterion_violation=1&sort=score_asc'), {q:'old',critical:true});
  assert.equal(filters.q,''); assert.equal(filters.critical,false); assert.equal(filters.criterion_violation,true);
  assert.deepEqual(readDialogFilters(new URLSearchParams(dialogListHash(filters).split('?')[1])),filters);
  assert.equal(readDialogFilters(new URLSearchParams(),filters).group_id,'A&B');
});
test('open-appeal view and total scope are independent from search-result size', () => {
  const rows=[{...row('a',20),open_appeals:0},{...row('b',90),open_appeals:2}];
  assert.deepEqual(visibleDialogRows(rows,{open_appeals:true,sort:'score_asc'}).map(r=>r.conversation_id),['b']);
  assert.equal(dialogScopeCount([{group_id:'DEV',count:65},{group_id:'MANUAL',count:2}],'DEV'),65);
  assert.equal(dialogScopeCount([{group_id:'DEV',count:65},{group_id:'MANUAL',count:2}],''),67);
});
