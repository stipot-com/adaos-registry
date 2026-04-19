## subnet_env

`subnet_env` — локальный операторский навык для просмотра и правки runtime-настроек подсети из `web_desktop`.

По роли он близок к `infrastate_skill`, но заметно уже по фокусу:

- показывает effective environment и identity ноды, важные для subnet/runtime;
- даёт маленький и безопасный набор редактируемых локальных настроек;
- проецирует свой snapshot в Yjs, чтобы появляться как desktop app и открываться в модальном окне;
- работает преимущественно через SDK и локальные сервисы runtime, а не через широкие публичные HTTP admin endpoints.

## Зачем нужен навык

Задача навыка простая: если поведение ноды меняется из-за `.env`, runtime-флагов или локальной Git identity, этот дрейф должен быть виден и исправим из одного места, без ручного редактирования файлов.

В MVP навык специально делаем local-first:

- он смотрит на локальную ноду и локальное runtime-окружение;
- читает активный `.env`, который реально используется runtime;
- проецирует snapshot в `data/subnet_env`;
- позволяет менять только заранее одобренный allowlist ключей.

Это не универсальный редактор remote config и не публичное subnet admin API.

## Принципы дизайна

- SDK-first. Навык должен предпочитать SDK/control-plane helper’ы и локальные service abstraction’ы вместо прямого использования широких HTTP admin surface.
- Safe for LLM-authored skills. Если навыкам не хватает проекции, её нужно сначала сделать безопасной на SDK-слое, а не расширять внешний API по умолчанию.
- Явный allowlist. Навык редактирует только те ключи, которые сознательно разрешены для локального операторского workflow.
- Разделение effective и persisted view. UI должен различать:
  - effective value, который видит запущенный процесс;
  - persisted value, который лежит в `.env`;
  - config-derived values вроде node/subnet/zone identity.
- Только локальные записи. В MVP запись идёт только в локальный dotenv/config source без cross-node mutation.

## Объём MVP

В первой версии навык должен показывать:

- `ENV_TYPE`
- `ADAOS_ZONE_ID` и effective `zone_id`
- `node_id`, `subnet_id`, `role`, `primary_node_name`
- путь к `.env`, который реально использует навык/runtime
- `GIT_USER`
- `GIT_EMAIL`
- `ADAOS_SUBNET_YJS_REPLICATION`
- диагностические флаги, которые полезно быстро переключать во время локальной диагностики

Первый редактируемый набор:

- `ENV_TYPE`
- `GIT_USER`
- `GIT_EMAIL`
- `ADAOS_SUBNET_YJS_REPLICATION`
- `ADAOS_CLI_DEBUG`
- `ADAOS_LOG_LEVEL`
- `ADAOS_SCENARIO_LOG_LEVEL`
- `HUB_NATS_VERBOSE`
- `HUB_NATS_TRACE`
- `HUB_NATS_TRACE_INPUT`
- `HUB_ROUTE_VERBOSE`
- `ROUTE_PROXY_VERBOSE`
- `HUB_TG_DEBUG`

## Что не входит в MVP

- правка секретов вроде `ADAOS_TOKEN`, `ROOT_TOKEN`, сертификатов и приватных ключей;
- правка широких bootstrap/network параметров вроде hub URL или token;
- удалённая мутация member-нод через hub transport;
- замена `infrastate_skill`;
- универсальный dotenv editor для произвольных ключей.

## Модель данных

Навык владеет logical projection slot:

- scope: `subnet`
- slot: `subnet_env.snapshot`

Default projection target:

- Yjs path: `data/subnet_env`

Snapshot должен как минимум содержать:

- `summary`
- `overview`
- `forms`
- `actions`
- `env`
- `node`
- `safety`

Где:

- `summary` оптимизирован под `visual.metricTile`
- `overview` оптимизирован под `ui.list`
- `forms` содержит текущие значения для текстовых полей
- `actions` содержит наборы кнопок для toggle/preset сценариев

## UI-контракт

`webui.json` должен вносить:

- одну desktop app кнопку, например `subnet_env_app`;
- одну модалку, например `subnet_env_modal`;
- schema-driven widgets, связанные с `data/subnet_env/...`.

Модалка должна быть ориентирована на быстрые операторские действия:

- текущий runtime mode и subnet context;
- поля правки Git identity;
- переключение Yjs replication;
- переключение диагностических флагов;
- refresh/reload действия.

UI должен оставаться декларативным и отправлять действия в skill tools через `callSkill`, а не требовать новых host-only action endpoints.

## Семантика записи

Запись должна быть консервативной:

- менять только allowlisted keys;
- максимально сохранять несвязанные `.env` строки и комментарии;
- писать нормализованные значения (`1`/`0`, `DEBUG`/`INFO`, trimmed text);
- безопасно обновлять in-process environment для текущего runtime;
- сразу refresh и re-project snapshot после изменения.

Нужно честно показывать, что часть изменений видна immediately, а часть может полностью вступать в силу только после restart runtime/services.

## Ожидаемые расширения SDK

Этот навык как раз помогает увидеть, каких SDK-safe helper’ов не хватает. Вероятные следующие шаги:

- reusable helper для чтения/записи runtime dotenv в SDK или services;
- безопасная SDK projection для local environment metadata;
- возможно, typed helper для editable runtime flags вместо ad-hoc key mutation.

Если такой helper появится, `subnet_env` должен перейти на него и перестать таскать собственную private parsing logic.

## Начальный tool surface

Первая реализация должна дать:

- `get_snapshot`
- `refresh_snapshot`
- `set_env_value`
- `apply_action`

Это локальные операторские инструменты, а не general remote management primitives.

## Дальнейший roadmap

После стабилизации MVP можно аккуратно расширять навык:

- добавить опциональную правку `ADAOS_ZONE_ID`;
- показывать restart-needed индикатор для каждого ключа;
- добавить hub/member awareness и selective remote read-only views;
- богаче показывать различия между file value, effective env и node-config-derived runtime state;
- вынести dotenv editing logic в reusable SDK/service helper;
- добавить тесты на projection shape и dotenv round-trip поведение.
