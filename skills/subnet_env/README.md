## subnet_env

`subnet_env` - операторский skill для `web_desktop`, который показывает локальное subnet/runtime-окружение ноды и позволяет безопасно править небольшой allowlist runtime-переменных через SDK-first слой.

По роли он ближе к `infrastate_skill`, чем к произвольному dotenv-редактору:

- появляется в `web_desktop` как app-кнопка;
- открывает schema-driven modal;
- работает через skill tools (`callSkill`), а не через широкие host/API action endpoints;
- держит внешний surface маленьким и безопасным для LLM-authored skills.

## Что skill делает

`subnet_env` собирает локальный snapshot из трех источников:

- node/config identity (`node_id`, `subnet_id`, `role`, `zone_id`, имя ноды);
- runtime/control-plane проекции (`runtime`, reliability, routing/connectivity hints);
- effective и persisted environment (`process env` и `.env`).

В UI skill показывает:

- `ENV_TYPE`;
- effective `zone_id` и `ADAOS_ZONE_ID`;
- Git identity (`GIT_USER`, `GIT_EMAIL`);
- `ADAOS_SUBNET_YJS_REPLICATION`;
- диагностические toggle/log-level флаги;
- разницу между effective env и persisted `.env`;
- operator notes о drift и restart-sensitive ключах.

## Принципы

- SDK-first. Если нужной безопасной проекции нет, расширяем SDK/control-plane helper, а не открываем лишний внешний API.
- Allowlist-only. Редактируются только заранее одобренные ключи.
- Local-first. Мутации идут только в локальный dotenv/source of truth текущей ноды.
- Explicit drift view. UI должен явно показывать, когда `process env` отличается от `.env`.
- Safe for LLM skills. Секреты, токены, сертификаты и bootstrap-параметры не попадают в editable surface.

## Editable allowlist

Сейчас skill разрешает править:

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

Read-only, но важные для оператора:

- `ADAOS_ZONE_ID`
- effective `zone_id`
- `node_id`
- `subnet_id`
- `role`
- `primary_node_name`
- путь к активному `.env`

## Что не входит в scope

- `ADAOS_TOKEN`, root token, сертификаты, приватные ключи;
- hub bootstrap secrets и подобные чувствительные параметры;
- удаленная мутация других нод;
- произвольное редактирование любых ключей в `.env`.

## Модель данных

Skill владеет проекцией:

- scope: `subnet`
- slot: `subnet_env.snapshot`
- target: `data/subnet_env`

Текущий snapshot содержит:

- `summary`
- `overview`
- `notices`
- `forms`
- `actions`
- `effective_env`
- `persisted_env`
- `diagnostics`
- `drift`
- `env`
- `node`
- `state`
- `safety`

Это дает UI не только “кнопки и поля”, но и диагностическую картину того, откуда реально взялось текущее значение.

## UI-контракт

`webui.json` вносит:

- app `subnet_env_app`;
- modal `subnet_env_modal`;
- optional desktop widget `subnet_env_widget`.

Модалка показывает:

- summary tile;
- operator notes;
- overview списка среды и ноды;
- поля `GIT_USER` / `GIT_EMAIL`;
- command bar для `ENV_TYPE`;
- command bar для subnet replication;
- command bar для diagnostic actions;
- отдельные списки effective env, persisted `.env` и drift.

Все действия уходят через:

- `subnet_env.get_snapshot`
- `subnet_env.refresh_snapshot`
- `subnet_env.set_env_value`
- `subnet_env.apply_action`

## Семантика записи

Запись должна оставаться консервативной:

- только allowlisted keys;
- нормализация значений (`1`/`0`, upper-case log levels, trimmed text);
- базовая валидация (`ENV_TYPE`, `GIT_EMAIL`, multiline guard);
- blank value для clearable text keys означает удаление ключа из persisted `.env`;
- после записи skill обновляет `os.environ` текущего процесса и сразу репроецирует snapshot.

Важно: часть изменений видна сразу, но часть вступает в полную силу только после restart runtime/services. Это намеренно подсвечивается в `notices`.

## Зрелая реализация MVP

Для “зрелого” состояния MVP считаем важным, что skill:

- не скрывает drift между `.env` и process env;
- показывает `ENV_TYPE` и `zone_id` в summary/overview;
- дает быстро править Git identity;
- дает быстро переключать YJS replication и diagnostic flags;
- не пишет в чувствительные ключи;
- устойчив к отсутствию части runtime-проекций;
- остается маленьким, понятным и безопасным surface для LLM.

## Дальше

Следующие аккуратные шаги для развития:

- вынести dotenv read/write в reusable helper на service/SDK-слое;
- добавить typed projection/helper для editable runtime flags;
- добавить restart-needed индикатор на уровне отдельных ключей;
- расширить read-only часть по subnet/member diagnostics без добавления remote mutation.
