# PLAN — Trix Arena plugin (автономная разработка заводом deer-flow)

> Источник правды: Obsidian `coding/arena/Trix Arena — план разработки.md` (владелец: Rafail).
> Этот файл — рабочая копия плана для исполнителей-воркеров. Менять только по решению владельца.

## Контекст для исполнителя

- Репозиторий: `/home/rafail/projects/trix-arena` на хосте 81.222.148.62 (локальный git; origin пока нет — пушить некуда, коммить локально).
- Основа: fork `Jakeschincariol/arena-skill` (remote `upstream`). НЕ удалять и не переписывать upstream-файлы: LICENSE, README.md (п-marker), skills/, tests/ — их можно только читать и расширять совместимо.
- Цель: plugin **Trix Arena** для Trix Agent — команда `/arena`, встроенный skill, детерминированный Python-движок турнира. Пользовательское имя — Trix Arena, технический id — `trix-arena`.
- Модель вызовов: safe MVP — участники = независимые LLM-вызовы, без shell/файлов у участников.

## Обязательные правила (нарушение = остановка работы)

1. Работать ТОЛЬКО внутри `/home/rafail/projects/trix-arena` на .62.
2. НЕ трогать: `.env`, ключи, другие каталоги, системные конфиги.
3. НЕ пушить никуда (нет прав и не нужно). Все коммиты — локальные, в ветке `feat/trix-arena-plugin`.
4. Каждый этап завершать коммитом с сообщением по шаблону `feat(arena): ...` / `test(arena): ...`.
5. Гейт этапа не пройден → остановиться, записать блокер в `PROGRESS.md`, перейти к следующему пункту нельзя.
6. Утром владелец смотрит `PROGRESS.md` + `git log` + тесты. Честность важнее объёма.

## Этапы (по порядку, каждый — отдельный запуск/сессия воркера)

### Этап 0 — базовый plugin-каркас
- [ ] Создать ветку `feat/trix-arena-plugin` от текущего upstream-HEAD.
- [ ] Структура `plugins/trix-arena/` по плану (plugin.yaml, __init__.py, schemas.py, commands.py, tools.py, arena/, data/, skills/trix-arena/SKILL.md, tests/).
- [ ] Минимальный plugin с командой `/arena ping` (ответ «pong»).
- [ ] `python3 -m compileall plugins` проходит.
- Гейт: compileall зелёный; структура совпадает с планом.

### Этап 1 — общее турнирное ядро
- [ ] Зафиксировать поведение upstream `skills/arena/` тестами ДО рефакторинга (characterization tests).
- [ ] Вынести карточки/пары/bye/scoring/JSON state в общий модуль `plugins/trix-arena/arena/`.
- [ ] `state_schema_version` + миграционный интерфейс.
- [ ] Никаких абсолютных путей; не писать в cwd по умолчанию.
- Гейт: upstream-тесты + новые unit-тесты зелёные; симуляция 100 участников = ровно 1 победитель.

### Этап 2 — plugin API и команды
- [ ] plugin.yaml с точным перечнем tools/команд.
- [ ] Регистрация `/arena` через публичный plugin context.
- [ ] Tools: `arena_plan`, `arena_start`, `arena_status`, `arena_cancel`, `arena_result`.
- [ ] Строгий parser аргументов, понятные ошибки.
- Гейт: doctor/валидация без ошибок; все tools доступны.

### Этап 3 — orchestrator
- [ ] JSON-схемы solve/attack/defense/judge.
- [ ] Волны с ограничением параллельности, retry только transport/schema.
- [ ] Idempotency key на каждый job; сохранение после каждого успешного job.
- [ ] pause/cancel/resume.
- Гейт: симуляция с падениями не теряет готовые jobs и не создаёт двойных победителей.

### Этап 4 — skill и UX
- [ ] `skills/trix-arena/SKILL.md`: триггеры, выбор режима, canonical task package.
- [ ] Preflight перед дорогим запуском (число участников + ожидаемые вызовы).
- [ ] Финальный ответ: победитель, счёт, пережитые атаки, ограничения.
- Гейт: новичок понимает запуск без чтения исходников.

### Этап 5 — тесты
- [ ] Unit: card dealer, bracket, bye, scoring, migrations.
- [ ] Invariant: нет дубликатов карточек, один победитель, eliminated не возвращается.
- [ ] Simulated E2E: 1, 2, 7, 8, 16, 100 участников (fake backend).
- [ ] Failure: timeout, malformed JSON, provider error, cancel, рестарт.
- [ ] Security: path traversal, oversized task, malicious output, state tampering.
- Гейт: всё зелёное.

### Этап 6 — документация
- [ ] README: что делает, установка, режимы, стоимость в вызовах, ограничения.
- [ ] Происхождение fork, лицензия, attribution.
- [ ] CHANGELOG.md, версия 0.1.0.
- Гейт: владелец читает README и понимает всё без вопросов.

## PROGRESS.md — обязательный журнал

После КАЖДОГО этапа дописывать в `PROGRESS.md` (в корне репо):
- дата/время, этап, статус (done/blocked), что сделано (кратко), какие тесты прошли, какой блокер.

## Остановка для владельца

- Права на GitHub-пуш отсутствуют → работа локальная. Перенос в XDataPlusX — отдельное утреннее решение.
- Никаких внешних API с ключами; участники турнира в тестах — fake backend, не реальные вызовы.
