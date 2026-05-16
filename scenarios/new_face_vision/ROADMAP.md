# Roadmap: new_face_vision

## Текущий статус

- Прогресс: 95%.
- Выполненный пакет: core upload-to-skill artifact refs, первые универсальные
  клиентские widgets (`visual.frameViewer`, `visual.image`, `input.fileUpload`,
  `visual.timeseriesChart`), публикация типов в `webui.v1.schema.json` и перевод
  сценария с временных JSON/path-панелей на целевые компоненты, нормализация
  ошибок/progress payload, focused contract tests и вывод upload widgets на
  первый экран desktop-сценария и Flask-inspired рабочая композиция без
  прямого копирования прототипа.
- Клиент запущен локально: `http://127.0.0.1:4200/`.
- API перезапущен локально: `http://127.0.0.1:8777/`.
- Следующий пакет: проверка model/masks/metadata upload на целевых файлах и
  решение по отдельному `input.playbackControls` после проверки MVP.

## Цель

Перенести полезные идеи интерфейса и обработки из `example/new_face` в сценарий
`new_face_vision` и навык `new_face_vision_skill`, не перенося Flask-прототип
как есть.

Целевая архитектура:

- навык отвечает за загрузку модели, загрузку данных, инференс и проекцию
  состояния;
- сценарий описывает desktop UI только через типы виджетов, поддерживаемые
  клиентом;
- крупные и часто меняющиеся значения идут через потоковые каналы, а не через
  Yjs;
- в Yjs хранится только компактное состояние: статусы, счетчики, ссылки на
  выбранные файлы, thresholds, последние легкие метрики и состояние операции;
- клиентские компоненты проектируются как переиспользуемые для других навыков.

## Зафиксированные решения

- [x] Считать `example/new_face` черновиком UX, а не кодом для точного переноса.
- [x] Оставить специфичную model/inference-логику внутри
      `new_face_vision_skill`.
- [x] Не хранить поток кадров и высокочастотные значения в Yjs.
- [x] Для preview и точек графиков использовать stream-backed переменные.
- [x] В опубликованном сценарии использовать только типы, поддерживаемые
      клиентом.
- [x] Новые widget-типы сначала заводить в клиенте, затем публиковать в
      `src/adaos/abi/webui.v1.schema.json` и документации.
- [x] Переиспользовать существующий подход ядра, где файл сохраняется
      платформой, а в навык передается ссылка на сохраненный файл.

## Этап 1: контракт данных

- [x] Описать компактный Yjs-контракт для `data/new_face_vision/current`.
- [x] Разделить runtime-состояние и состояние результата анализа.
- [x] Описать artifact refs для модели, архива кадров, архива масок и JSONL.
- [x] Описать stream payload для `newface_vision_frame`.
- [x] Описать минимальные stream payloads для MVP-графиков.
- [x] Зафиксировать, какие значения хранятся в Yjs, а какие являются
      stream-only.
- [x] Вынести контракт в этот roadmap или отдельный документ рядом со сценарием.

Предлагаемое компактное состояние в Yjs:

- `status`: общий статус навыка/приложения, например `init`, `ready`,
  `loading`, `analyzing`, `error`;
- `operation`: id текущей операции, подпись, прогресс и ошибка;
- `files`: artifact refs загруженных файлов и флаги загрузки;
- `model`: флаг загрузки, ref источника и короткая metadata;
- `stats`: количество кадров, текущий индекс, fps, флаг наличия масок;
- `thresholds`: настроенные пороги анализа;
- `latest`: последний легкий summary без тяжелого payload изображения.

Предлагаемые stream-каналы:

- `newface_vision_frame`: текущий preview frame и результат по кадру;
- `newface_vision_metrics`: точки временных рядов для графиков;
- `newface_vision_progress`: опциональные progress events для долгих операций.

## Этап 2: навык

- [x] Обновить `process_frame`, чтобы результат отражался в публичном
      контракте состояния.
- [x] Хранить распакованные frames/masks как file refs и открывать изображения
      лениво при обработке, чтобы большой архив не занимал память целиком.
- [x] Публиковать preview frames через stream, а не хранить base64 preview в
      Yjs.
- [x] Публиковать точки графиков через stream.
- [x] В Yjs оставлять только последний легкий summary.
- [x] Добавить handlers, принимающие artifact refs вместо локальных путей.
- [x] Временно оставить path-based handlers для локальной разработки, если это
      удобно.
- [x] Нормализовать ошибки, чтобы UI мог показывать их существующими feedback
      компонентами.
- [x] Добавить focused tests на проекцию состояния и форму stream payload.

## Этап 3: ядро

- [x] Найти и переиспользовать существующий upload-to-skill artifact flow,
      сделанный для данных из Telegram.
- [x] Обобщить artifact contract, если сейчас он завязан на Telegram.
- [x] Убедиться, что загруженные файлы сохраняются в skill-owned или
      skill-addressable location.
- [x] Убедиться, что tool calls могут получать artifact refs обычными JSON
      params.
- [x] Добавить guidance для публикации прогресса долгих skill operations.
- [x] Не добавлять широкий async job system, пока MVP может обойтись без него.
- [x] Поднять default upload limit core до 1 GiB для локальных skill-owned
      датасетов, сохранив override через `ADAOS_SKILL_UPLOAD_MAX_BYTES`.

## Этап 4: клиентские компоненты

- [x] Добавить `visual.image` или `visual.frameViewer`.
- [x] Добавить `input.fileUpload`.
- [x] Добавить `visual.timeseriesChart` только с MVP API графиков.
- [x] Добавить или адаптировать компактный metrics view, если
      `visual.metricTile` окажется слишком узким.
- [x] Для статуса использовать существующие feedback/status компоненты, если
      функционального соответствия достаточно.
- [ ] Добавить `input.playbackControls` для play, pause, stop, step и replay,
      если после smoke test `input.commandBar` окажется неудобен.
- [x] Зарегистрировать каждый новый widget type в client widget host.
- [x] Добавить каждый новый опубликованный widget type в `webui.v1.schema.json`.
- [x] Добавить короткую документацию и примеры для новых widget types.

## Этап 5: сценарий

- [x] Убрать из опубликованного сценария неподдерживаемые widget-типы до того,
      как клиент начнет их поддерживать.
- [x] После появления `input.fileUpload` заменить текстовые поля путей на file
      upload widgets.
- [x] Вывести upload widgets на первый экран, а debug `Compact state` убрать из
      основной рабочей колонки.
- [x] Перестроить первый экран по мотивам Flask-прототипа: preview как главный
      рабочий блок, controls под preview, upload/KPI/charts справа.
- [x] Направить preview UI на stream-backed frame data.
- [x] Направить графики на stream-backed metrics data.
- [x] Держать command actions в поддерживаемом клиентом формате target
      `skill.method`.
- [x] Первый экран оставить функциональным и компактным: preview, controls,
      uploads, status и charts.
- [ ] Не переносить prototype-only детали из `example/new_face`.

## Этап 6: проверка

- [x] Провалидировать `scenario.json` через `scenario.schema.json` и
      `webui.json` через `webui.v1.schema.json`.
- [x] Прогнать backend/skill tests для artifact loading и frame processing.
      Частично: добавлен focused contract test на компактный snapshot, stream
      payloads и нормализованные ошибки; полный runtime smoke остается ручным.
- [x] Прогнать client tests для регистрации widget types и dispatch actions.
- [x] Smoke test desktop-сценария на `example/assets/frames.zip`: core upload,
      `new_face_vision_load_frames`, skill-owned artifact ref и `Next frame`.
- [x] Убедиться, что Yjs остается компактным во время playback.
- [x] Убедиться, что кадры и графики идут через streams.

## MVP exit criteria

- [ ] Пользователь может загрузить/выбрать модель и входные данные через
      desktop UI.
- [x] Навык получает сохраненные file refs и загружает frames dataset.
- [x] Playback controls запускают обработку кадров.
- [x] Preview frames обновляются через stream.
- [x] Метрики и графики обновляются через streams.
- [x] В Yjs лежит только компактное состояние и последние summaries.
- [x] Сценарий использует только widget-типы, поддерживаемые клиентом и схемой.
