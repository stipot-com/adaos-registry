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
- `newface_vision_progress` - compact progress/error events для долгих операций
  и command feedback.

## Компоненты клиента

- `visual.frameViewer` для stream-backed preview кадров.
- `visual.image` как совместимый image-only alias поверх frame viewer.
- `input.fileUpload` для загрузки модели, архива кадров, архива масок и JSONL.
- `visual.timeseriesChart` для MVP-графиков поверх stream points.
- существующий `input.commandBar` для play, reset, clear и step в MVP.
- При необходимости компактный metrics view поверх существующих visual
  компонентов.

## Работа с файлами

Сценарий должен использовать общий platform flow загрузки файлов: клиент
загружает файл в ядро, ядро сохраняет его в область, доступную навыку, а в
tool call передается artifact ref. Навык не должен требовать от браузера
локальный путь к файлу пользователя.

Path-based загрузку можно временно оставить только для локальной разработки.

Для локального MVP core принимает skill-owned upload до 1 GiB по умолчанию
(`ADAOS_SKILL_UPLOAD_MAX_BYTES` можно переопределить окружением). Это покрывает
`example/assets/frames.zip`, который весит около 778 MiB. В навыке список кадров
хранится как refs на файлы, а изображения открываются лениво при обработке кадра,
чтобы не держать весь архив в памяти после распаковки.

## Roadmap

Подробный чеклист работ находится в [ROADMAP.md](./ROADMAP.md), текущий контракт
данных и UI - в [CONTRACT.md](./CONTRACT.md).

Текущий прогресс: 95%.

Выполнено:

- навык публикует preview frame через `newface_vision_frame`;
- навык публикует точки метрик через `newface_vision_metrics`;
- Yjs snapshot оставлен компактным и содержит `latest` без base64 preview;
- handlers принимают как path-based параметры, так и artifact-like refs;
- `scenario.json` и `webui.json` используют `visual.frameViewer`,
  `visual.timeseriesChart` и `input.fileUpload` вместо временных JSON/path
  панелей для основного UX;
- upload widgets выведены в рабочую левую колонку первого экрана, а compact
  state перенесен в нижнюю debug-зону;
- первый экран перестроен по мотивам Flask-прототипа: большой preview, controls
  под ним, upload/KPI/charts в правой рабочей колонке;
- `visual.metricTile` получил декларативные `valuePath`, `descriptionPath` и
  форматирование для переиспользуемых KPI;
- core upload limit поднят до 1 GiB, а `new_face_vision_skill` хранит refs на
  распакованные кадры и открывает изображения лениво при обработке;
- `example/assets/frames.zip` проверен через полный flow: core upload,
  `new_face_vision_load_frames` и `Next frame`;
- ядро получило compatibility helpers для projection/stream runtimes, node metadata,
  runtime refresh и Yjs pressure policy;
- ядро получило MVP endpoint загрузки skill-owned файлов с artifact refs;
- ошибки навыка нормализованы в `{code, message, retryable, ts}` и попадают в
  компактный `newface_vision_progress`;
- добавлен focused contract test на компактный snapshot, stream payloads,
  поддерживаемые widget types и нормализованные ошибки;
- локально подняты клиент `http://127.0.0.1:4200/` и API `http://127.0.0.1:8777/`.

Ближайшие этапы:

- проверить model/masks/metadata upload на реальных файлах, когда они будут в
  целевом наборе MVP;
- после smoke решить, нужен ли отдельный `input.playbackControls` или достаточно
  существующего `input.commandBar` для MVP.

## MVP

MVP считается готовым, когда пользователь может загрузить модель и входные
данные через desktop UI, запустить обработку кадров, видеть preview через
stream, получать stream-обновления графиков, а Yjs при этом остается компактным.
