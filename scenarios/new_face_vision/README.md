# new_face_vision

Сценарий desktop-интерфейса для анализа кадров с помощью навыка
`new_face_vision_skill`.

`example/new_face` используется как черновик целевого UX: из него берем общую
идею экрана, поток обработки и набор пользовательских действий. Flask-прототип
не переносится напрямую.

## Целевая архитектура

- `new_face_vision_skill` загружает модель, входные данные и выполняет
  инференс.
- `scenario.json` описывает desktop UI декларативно.
- Yjs хранит только компактное состояние: статусы, счетчики, refs файлов,
  thresholds и последние легкие summaries.
- Кадры, preview и точки графиков передаются через stream-каналы.
- Новые UI-компоненты сначала реализуются в клиенте, затем добавляются в
  `src/adaos/abi/webui.v1.schema.json` и документацию.
- Опубликованный сценарий должен использовать только widget-типы, которые уже
  поддержаны клиентом и схемой.

## Основные потоки данных

Компактное состояние:

- `data/new_face_vision/current/status`
- `data/new_face_vision/current/operation`
- `data/new_face_vision/current/files`
- `data/new_face_vision/current/model`
- `data/new_face_vision/current/stats`
- `data/new_face_vision/current/thresholds`
- `data/new_face_vision/current/latest`

Потоковые данные:

- `newface_vision_frame` - preview frame и результат анализа кадра;
- `newface_vision_metrics` - точки временных рядов для графиков;
- `newface_vision_progress` - progress events для долгих операций, если нужны.

## Планируемые компоненты клиента

- `visual.image` или `visual.frameViewer` для preview кадров.
- `input.fileUpload` для загрузки модели, архива кадров, архива масок и JSONL.
- `visual.timeseriesChart` для MVP-графиков.
- `input.playbackControls` для play, pause, stop, step и replay.
- При необходимости компактный metrics view поверх существующих visual
  компонентов.

## Работа с файлами

Сценарий должен использовать общий platform flow загрузки файлов: клиент
загружает файл в ядро, ядро сохраняет его в область, доступную навыку, а в
tool call передается artifact ref. Навык не должен требовать от браузера
локальный путь к файлу пользователя.

Path-based загрузку можно временно оставить только для локальной разработки.

## Roadmap

Подробный чеклист работ находится в [ROADMAP.md](./ROADMAP.md).

Текущий прогресс: 55%.

Выполнено в первом пакете:

- навык публикует preview frame через `newface_vision_frame`;
- навык публикует точки метрик через `newface_vision_metrics`;
- Yjs snapshot оставлен компактным и содержит `latest` без base64 preview;
- handlers принимают как path-based параметры, так и artifact-like refs;
- `scenario.json` и `webui.json` временно используют только поддерживаемые
  клиентом widget-типы.

Ближайшие этапы:

- реализовать недостающие универсальные client widgets;
- переиспользовать или обобщить artifact upload flow;
- после появления новых widgets обновить `scenario.json` с JSON-viewer
  временных панелей на целевые компоненты.

## MVP

MVP считается готовым, когда пользователь может загрузить модель и входные
данные через desktop UI, запустить обработку кадров, видеть preview через
stream, получать stream-обновления графиков, а Yjs при этом остается компактным.
