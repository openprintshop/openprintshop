"""Свежесть собранного бандла настольной админки.

app/webui/desk/desk.js коммитится готовым — бот и покупатель работают без Node. Отсюда живая
беда: правку в app/webui/desk/src закоммитили, а `npm run build:desk` забыли — в git уезжает
новый исходник и СТАРЫЙ бандл, и на бою ничего не меняется. Ловится это обычно только глазами
пользователя.

Тест пересобирает бандл во ВРЕМЕННЫЙ файл теми же флагами, что прописаны в package.json
(scripts.build:desk — флаги берём оттуда, а не дублируем: поменяется скрипт, поедет и тест),
и сравнивает СОДЕРЖИМОЕ с закоммиченным desk.js.

Почему не по времени изменения: git не хранит mtime — после клона/checkout у desk.js и исходников
времена произвольные, и такой тест соврал бы в обе стороны.

Почему сравнивается только сам бандл, без desk.js.map:
  1) карта в .gitignore — покупателю исходники не отдаём, сверять с репозиторием нечего;
  2) внутри карты пути к исходникам записаны ОТНОСИТЕЛЬНО места, куда её положили, поэтому
     сборка во временную папку даёт другой (и вполне законный) текст карты.
Сам бандл при этом сравнивается честно, вместе с хвостовым `//# sourceMappingURL=desk.js.map`:
имя временного файла берём то же (desk.js), так что комментарий совпадает байт в байт.

Node нужен только разработчику — без него skip.
"""
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = ROOT / "package.json"


def _esbuild_or_skip() -> Path:
    """Бинарник esbuild из node_modules или skip."""
    if not shutil.which("node"):
        pytest.skip("нет node — бандл настольной админки пересобирается только на машине разработчика")
    # esbuild ставится платформенным пакетом: на Windows — @esbuild/win32-x64/esbuild.exe,
    # на прочих — @esbuild/<platform>/bin/esbuild. Ищем оба варианта, чтобы тест не был
    # привязан к машине автора.
    pkgs = ROOT / "node_modules" / "@esbuild"
    for pattern in ("*/esbuild.exe", "*/bin/esbuild"):
        for path in sorted(pkgs.glob(pattern)):
            return path
    pytest.skip("нет бинарника esbuild в node_modules/@esbuild — нечем пересобрать бандл (npm install)")


def _build_argv() -> list[str]:
    """Разобранная команда из scripts.build:desk. Флаги — источник истины в package.json."""
    script = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"]["build:desk"]
    argv = shlex.split(script)
    assert argv and argv[0] == "esbuild", f"scripts.build:desk перестал быть вызовом esbuild: {script}"
    return argv


def test_committed_desk_bundle_matches_fresh_build(tmp_path):
    """Закоммиченный desk.js совпадает с пересобранным из текущих исходников.

    Красный тест означает ровно одно: `npm run build:desk` не прогнали после правки src.
    """
    esbuild = _esbuild_or_skip()
    argv = _build_argv()

    outs = [a for a in argv if a.startswith("--outfile=")]
    assert len(outs) == 1, f"ожидался ровно один --outfile в build:desk, найдено {len(outs)}"
    committed = ROOT / outs[0].split("=", 1)[1]
    assert committed.is_file(), f"собранный бандл не найден: {committed}"

    # Имя временного файла — то же самое: от него esbuild берёт хвостовой sourceMappingURL.
    fresh = tmp_path / committed.name
    rebuilt_argv = [str(esbuild)]
    rebuilt_argv += [f"--outfile={fresh}" if a.startswith("--outfile=") else a for a in argv[1:]]

    proc = subprocess.run(rebuilt_argv, cwd=ROOT, capture_output=True, timeout=600)
    log = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    assert proc.returncode == 0, f"пересборка бандла упала (код {proc.returncode}):\n{log}"

    # Переводы строк нормализуем. Основная защита — строка `desk.js -text` в .gitattributes,
    # но в уже существующих рабочих деревьях атрибут применится только после перечекаута,
    # а бандл всё это время лежал бы там с CRLF и давал ложный красный.
    def _lf(raw: bytes) -> bytes:
        return raw.replace(b"\r\n", b"\n")

    assert _lf(fresh.read_bytes()) == _lf(committed.read_bytes()), (
        f"{committed.relative_to(ROOT).as_posix()} отстал от исходников в app/webui/desk/src.\n"
        "Пересоберите бандл: npm run build:desk — и закоммитьте его вместе с правкой src."
    )
