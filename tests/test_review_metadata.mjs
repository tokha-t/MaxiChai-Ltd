import test from 'node:test';
import assert from 'node:assert/strict';
import { apiErrorMessage } from '../web/js/api.js';
import { checksSummary, profileId, selectedProfile } from '../web/js/evaluation-meta.js';
import { dialogListHash, readDialogFilters } from '../web/js/dialog-list-model.js';
import { audioSource } from '../web/js/audio-source.js';

test('incomplete checks are reported against requested checks, not received network responses', () => {
  const result = checksSummary({ requested_votes: 3, received_votes: 3, valid_votes: 1, votes_complete: false });
  assert.equal(result.text, 'Получена 1 из 3 проверок');
  assert.equal(result.received, 3);
  assert.equal(result.incomplete, true);
  assert.equal(checksSummary({ requested_votes: 3, valid_votes: 3, votes_complete: true }).incomplete, false);
});

test('legacy requested votes and saved responses are not reported as verified complete checks', () => {
  const legacy = checksSummary({ votes: [{ verdict: 'yes' }] }, 3);
  assert.equal(legacy.valid, null);
  assert.equal(legacy.text, 'Число валидных проверок не сохранено; запрошено 3');
  assert.equal(checksSummary({ votes: 3 }).valid, null);
});

test('export validation errors are readable and preserve model provenance diagnostics', () => {
  const message = apiErrorMessage({ detail: { errors: ['Выберите checklist_id', 'В выгрузке есть оценки заглушки'], warnings: [] } }, 400);
  assert.equal(message, 'Выберите checklist_id\nВ выгрузке есть оценки заглушки');
  assert.equal(apiErrorMessage({ detail: { message: 'Оценка уже выполняется', job_id: 'abc' } }, 409), 'Оценка уже выполняется');
  assert.doesNotMatch(apiErrorMessage({ detail: 'token=privatevalue' }, 400), /privatevalue/);
});

test('selected immutable checklist ID survives a list URL round trip without an active-profile fallback', () => {
  const filters = readDialogFilters(new URLSearchParams('group_id=DEV&checklist_id=1'));
  const hash = dialogListHash(filters);
  const restored = readDialogFilters(new URLSearchParams(hash.split('?')[1]));
  const profiles = [{ id: 4, version: 4, active: true }, { id: 1, version: 1, active: false }];
  assert.equal(restored.checklist_id, '1');
  assert.equal(selectedProfile(profiles, restored.checklist_id).version, 1);
  assert.equal(selectedProfile(profiles, ''), null);
  assert.equal(selectedProfile(profiles, '999'), null);
  for (const invalid of [null, '', 'x', 0, -1, 1.5, true]) assert.equal(profileId(invalid), null);
});

test('known organizer HTML player opens externally while media and unsafe URLs remain distinct', () => {
  const route = '/powerautomate/automations/direct/cu/27/workflows/abcd1234/triggers/manual/paths/invoke/Conversation/abcd-1234';
  const base = 'https://cqa.example/';
  assert.equal(audioSource(`https://demo.environment.api.powerplatform.com${route}?sig=test`, base).mode, 'external');
  assert.equal(audioSource('https://demo.environment.api.powerplatform.com/audio.mp3', base).mode, 'native');
  assert.equal(audioSource(`https://environment.api.powerplatform.com.evil.example${route}`, base).mode, 'native');
  assert.equal(audioSource('/audio/known.mp3', base).mode, 'native');
  assert.equal(audioSource('javascript:alert(1)', base), null);
  assert.equal(audioSource('', base), null);
});
