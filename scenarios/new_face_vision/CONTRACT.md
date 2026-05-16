# new_face_vision UI/Data Contract

## Yjs State

`data/new_face_vision/current` is the compact shared state for the scenario.
It stores only values that are useful for first paint, recovery, and command
feedback:

- `status`: `init`, `ready`, `loading`, `analyzing`, or `error`.
- `operation`: current operation id, label, progress, and error.
- `files`: artifact refs for `model`, `frames`, `masks`, and `metadata`.
- `model`: loaded flag, source ref, and short runtime metadata.
- `stats`: total frames, loaded masks/metadata, current and next frame index.
- `thresholds`: warning, alarm, and prediction thresholds.
- `latest`: last lightweight result summary without image/base64 payload.
- `error`: last normalized error object.

`data/new_face_vision/history` is reserved for bounded lightweight history.
Frame images and high-frequency metric points must not be stored in Yjs.

## Artifact Refs

File upload widgets call the core upload endpoint and pass the returned
`artifact_ref` into skill tools. The current MVP ref shape is:

```json
{
  "kind": "skill_file",
  "skill": "new_face_vision_skill",
  "purpose": "frames",
  "name": "frames.zip",
  "relative_path": "uploads/frames/frames.zip",
  "path": "C:/.../.runtime/new_face_vision_skill/v0.0.0/data/files/uploads/frames/frames.zip",
  "uri": "file:///C:/...",
  "size_bytes": 123,
  "sha256": "..."
}
```

The skill accepts this ref through `artifact_ref`, `file`, `artifact`, `ref`, or
`value` and resolves it to a skill-addressable local path.

The core upload endpoint accepts local development datasets up to 1 GiB by
default. Operators can override this with `ADAOS_SKILL_UPLOAD_MAX_BYTES`.
The desktop scenario mirrors this limit for model/frame/mask uploads, while
metadata uploads are capped at 64 MiB in the UI.

## Streams

`newface_vision_frame` uses replace semantics and carries the latest preview:

- `image.src`: browser-ready `data:` URL.
- `image.data`, `image.mime`, `image.encoding`: raw image payload metadata.
- `frame_idx`, `frame_key`, `total_frames`.
- `prediction.pred_ratio`, `prediction.true_ratio`.
- `metrics.dice`, `metrics.iou`.
- `status.label`, `status.color`.

`newface_vision_metrics` uses append semantics with `collectionKey: points` and
`dedupeBy: frame_idx`. Each event is one point:

- `frame_idx`
- `ts`
- `series.pred_ratio`
- `series.true_ratio`
- `series.dice`
- `series.iou`

`newface_vision_progress` is replace-mode progress for long operations and
command feedback. It should stay compact and use the same normalized error
shape as Yjs state.

## Errors And Progress

Errors are objects, not plain strings:

```json
{
  "code": "frames_missing",
  "message": "No frames loaded",
  "retryable": false,
  "ts": 1778934418.0,
  "details": {}
}
```

`details` is optional and must stay small. Large payloads, previews, traces, and
datasets stay out of Yjs and out of progress events.

Progress events use this MVP shape:

```json
{
  "ok": false,
  "status": "error",
  "operation": {
    "id": "process_frame",
    "label": "Process frame",
    "progress": 0.0,
    "error": {
      "code": "frames_missing",
      "message": "No frames loaded",
      "retryable": false,
      "ts": 1778934418.0
    }
  },
  "error": {
    "code": "frames_missing",
    "message": "No frames loaded",
    "retryable": false,
    "ts": 1778934418.0
  },
  "ts": 1778934418.0
}
```

## Client Widgets

The scenario currently relies on these reusable renderer types:

- `visual.frameViewer` for stream-backed image preview.
- `visual.timeseriesChart` for a single MVP timeseries selected by `xKey` and
  `yKey`.
- `input.fileUpload` for core-managed skill file upload.
- Existing `input.commandBar`, `visual.metricTile`, and `ui.jsonViewer` for
  controls/status/debug state.
