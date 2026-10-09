// Запуск тестов фронта настольной админки: npm run test:desk
//
// Node не умеет читать .jsx, а модули под тестом лежат рядом с экранами и имеют это
// расширение. Поэтому сначала собираем каждый тест в обычный .mjs тем же esbuild, что
// собирает саму админку, и только потом отдаём встроенному тест-раннеру Node.
// Список файлов собираем сами: esbuild глобы не раскрывает, а cmd на Windows — тем более.
import { spawnSync } from 'node:child_process';
import { mkdirSync, readdirSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../../../..');
// Кладём НЕ в node_modules: встроенный раннер Node такие пути пропускает целиком,
// и тесты просто не находятся. Папка в .gitignore.
const out = resolve(root, '.desk-tests');
const esbuild = resolve(root, 'node_modules/@esbuild/win32-x64/esbuild.exe');

const tests = readdirSync(here).filter((f) => f.endsWith('.test.js'));
if (!tests.length) {
  console.error('тестов не найдено в', here);
  process.exit(1);
}

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });

const build = spawnSync(esbuild, [
  ...tests.map((f) => join(here, f)),
  '--bundle', '--platform=node', '--format=esm', '--packages=external',
  // .mjs, а не .js: иначе Node ругается, что тип модуля не указан в package.json,
  // и предупреждение забивает вывод тестов
  '--out-extension:.js=.mjs',
  `--outdir=${out}`, '--log-level=warning',
], { stdio: 'inherit' });
if (build.status !== 0) process.exit(build.status ?? 1);

// Передаём МАСКУ, а не папку: `node --test <папка>` пытается загрузить её как модуль
// и падает с MODULE_NOT_FOUND, а маску раскрывает сам раннер.
const run = spawnSync(process.execPath, ['--test', join(out, '*.test.mjs')], { stdio: 'inherit' });
process.exit(run.status ?? 1);
