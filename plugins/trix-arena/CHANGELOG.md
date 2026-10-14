# Changelog — Trix Arena

Формат: Keep a Changelog (упрощённо). Версии плагина совпадают с
`plugin.yaml` и `__version__` в `plugins/trix-arena/__init__.py`.

## [0.1.0] — 2026-10-14

Первый релиз плагина Trix Arena: `/arena` + детерминированный турнирный
движок (single-elimination: spawn → attack → defend → judge → чемпион).

### Этап 0 — plugin skeleton
- Каркас `plugins/trix-arena/`: `plugin.yaml`, регистрация `register(context)`,
  команда `/arena ping` (ровно `pong`), инструмент `arena_ping`.

### Этап 1 — tournament core
- Ядро `arena/core.py`, извлечённое из upstream `skills/arena/bracket.py`:
  раздача карт (`deal`, стратегии из `data/`), сетка и баи (`bracket_sizes`,
  нечётные раунды — один бай), счёт (`weighted_total`, веса 30/25/15/20/10),
  JSON-состояние с версией схемы и миграциями, планировщик стоимости
  (`plan_totals`: 100 → 7 раундов/595 вызовов/70 волн; 16 → 4/91/16).

### Этап 2 — plugin API
- Инструменты `arena_plan`, `arena_start`, `arena_status`, `arena_cancel`,
  `arena_result` поверх ядра; строгий парсер `/arena` (6 подкоманд, флаги
  `--name value`, ошибки без угадываний); правки движка по итогам
  выравнивания контракта.

### Этап 3 — orchestrator
- `arena/orchestrator.py`: пошаговый (`execute_job`) и волновой
  (`wave_batches`) прогон, идемпотентные ключи задач (32 hex), паузы,
  политика повторов `MAX_RETRIES + 1`; выравнивание тестов с контрактом
  (test-contract alignment).

### Этап 4 — skill & UX docs
- `skills/trix-arena/SKILL.md`: UX-документация `/arena` для агента — режимы,
  стоимость, подкоманды, ограничения.

### Этап 5 — test suite & night-watch fixes
- Набор тестов: инварианты сетки, E2E против `plan_totals`, восстановление
  после сбоев, безопасность (path traversal, verbatim-хранение, карантин).
- Ночные правки движка: `save_state` создаёт директорию перед записью
  (mkdir-before-write); после карантина нечитаемого вывода (`.unreadable`)
  оркестратор переходит в контролируемую паузу.

### Docs (этап 6)
- `README.md` плагина (что делает, установка, режимы и стоимость, гарантии,
  ограничения, происхождение/лицензия/attribution).
- Выравнивание версии: `arena/__version__` 0.2.0 → 0.1.0 (плагин — 0.1.0).

### Происхождение
- Форк [Jakeschincariol/arena-skill](https://github.com/Jakeschincariol/arena-skill)
  (MIT, корневой `LICENSE`). Upstream-файлы не модифицируются.
