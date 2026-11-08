import test from 'node:test';
import assert from 'node:assert/strict';
import { parseJudgeSpec, serializeJudges } from '../web/js/views/settings.js';

test('model rows round-trip local tags, slash names and server-provided providers', () => {
  const rows = [
    { provider: ' ollama ', model: 'qwen:32b' },
    { provider: 'Custom_Provider', model: 'organization/model-v2' },
  ];
  const wire = serializeJudges(rows);
  assert.equal(wire, 'ollama:qwen:32b,custom_provider:organization/model-v2');
  assert.deepEqual(parseJudgeSpec(wire), [
    { provider: 'ollama', model: 'qwen:32b' },
    { provider: 'custom_provider', model: 'organization/model-v2' },
  ]);
});

test('invalid raw configuration cannot silently produce a different model set', () => {
  for (const invalid of ['', 'mock', ':model', 'mock:', 'bad provider:model', 'mock:a,,mock:b', 'mock:model\nSECRET=value', 'mock:model#comment']) {
    assert.throws(() => parseJudgeSpec(invalid), undefined, invalid);
  }
});

test('empty or unsafe model rows are rejected before saving', () => {
  assert.throws(() => serializeJudges([]));
  for (const model of ['', 'two models', 'x,y', 'x=y', 'x\ny']) {
    assert.throws(() => serializeJudges([{ provider: 'mock', model }]));
  }
});
