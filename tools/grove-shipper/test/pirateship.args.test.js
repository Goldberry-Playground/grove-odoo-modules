// Pure unit test for chromiumArgsFromEnv — no browser required, so it runs in
// the browserless suite and locks the rootless/headless launch contract (§B2).
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { chromiumArgsFromEnv } from '../src/pirateship.js';

test('chromiumArgsFromEnv is empty when PW_CHROMIUM_ARGS is unset or blank', () => {
  assert.deepEqual(chromiumArgsFromEnv({}), []);
  assert.deepEqual(chromiumArgsFromEnv({ PW_CHROMIUM_ARGS: '' }), []);
  assert.deepEqual(chromiumArgsFromEnv({ PW_CHROMIUM_ARGS: '   ' }), []);
});

test('chromiumArgsFromEnv splits on any whitespace and drops empties', () => {
  assert.deepEqual(
    chromiumArgsFromEnv({ PW_CHROMIUM_ARGS: '--no-sandbox --disable-dev-shm-usage --disable-gpu' }),
    ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  );
  assert.deepEqual(
    chromiumArgsFromEnv({ PW_CHROMIUM_ARGS: '  --no-sandbox\t--disable-gpu \n' }),
    ['--no-sandbox', '--disable-gpu'],
  );
});
