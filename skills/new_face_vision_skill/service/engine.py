from __future__ import annotations

import os
import json
import zipfile
import shutil
import io
import base64
import tempfile
import logging
import time
from pathlib import Path
from typing import Any, Mapping

try:
    import numpy as np
except Exception:
    np = None

try:
    from PIL import Image
except Exception:
    Image = None

try:
    import torch
    import torch.nn as nn
    import torchvision
    from torchvision.transforms import functional as TF
except Exception:
    torch = None
    nn = None
    torchvision = None
    TF = None

_log = logging.getLogger("new_face_vision.engine")


class NewFaceVisionEngine:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.frames_dir = self.state_dir / "frames"
        self.masks_dir = self.state_dir / "masks"
        self.frames_dir.mkdir(exist_ok=True)
        self.masks_dir.mkdir(exist_ok=True)

        self._device = "cuda" if (torch is not None and torch.cuda.is_available()) else "cpu"
        self._model = None
        self._frames: dict[str, Path] = {}
        self._masks: dict[str, Path] = {}
        self._metadata = {}
        self._current_frame_idx = 0
        self._threshold = 0.35
        self._warning_threshold = 0.05
        self._alarm_threshold = 0.15
        self._prediction_cache = {}
        self._model_path = None
        self._files: dict[str, dict[str, Any] | None] = {
            "model": None,
            "frames": None,
            "masks": None,
            "metadata": None,
        }
        self._operation: dict[str, Any] = {
            "id": None,
            "label": "",
            "progress": None,
            "error": None,
        }
        self._latest: dict[str, Any] | None = None
        self.last_error: dict[str, Any] | None = None

        _log.info(f"NewFaceVisionEngine initialized. Device: {self._device}")

    def configure(
        self,
        model_path: str | None = None,
        frames_path: str | None = None,
        masks_path: str | None = None,
        metadata_path: str | None = None,
        threshold: float | None = None,
        warning_threshold: float | None = None,
        alarm_threshold: float | None = None,
    ) -> dict[str, Any]:
        result = {"ok": True, "actions": []}

        if threshold is not None:
            self._threshold = threshold
            result["actions"].append(f"threshold={threshold}")
        if warning_threshold is not None:
            self._warning_threshold = warning_threshold
            result["actions"].append(f"warning_threshold={warning_threshold}")
        if alarm_threshold is not None:
            self._alarm_threshold = alarm_threshold
            result["actions"].append(f"alarm_threshold={alarm_threshold}")

        if model_path:
            load_result = self.load_model(model_path)
            result.update(load_result)
            if not load_result.get("ok", True):
                result["ok"] = False
                return result

        if frames_path:
            load_result = self.load_frames(frames_path)
            result.update(load_result)
            if not load_result.get("ok", True):
                result["ok"] = False
                return result

        if masks_path:
            load_result = self.load_masks(masks_path)
            result.update(load_result)
            if not load_result.get("ok", True):
                result["ok"] = False
                return result

        if metadata_path:
            load_result = self.load_metadata(metadata_path)
            result.update(load_result)
            if not load_result.get("ok", True):
                result["ok"] = False
                return result

        return result

    def load_model(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("load_model", "Load model")
            _log.info(f"Loading model from {path}")

            if torch is None or nn is None or torchvision is None or TF is None:
                return self._fail_operation("torch/torchvision are not installed", code="dependency_missing")

            if not os.path.exists(path):
                return self._fail_operation(f"Model file not found: {path}", code="file_not_found")

            checkpoint = torch.load(path, map_location=self._device)

            model = torchvision.models.segmentation.deeplabv3_resnet50(
                weights=None,
                weights_backbone=None
            )
            model.classifier[-1] = nn.Conv2d(256, 1, kernel_size=1)

            if 'model_state' in checkpoint:
                model.load_state_dict(checkpoint['model_state'], strict=False)
                _log.info(f"Loaded checkpoint epoch: {checkpoint.get('epoch', '?')}")
            else:
                model.load_state_dict(checkpoint, strict=False)

            model.to(self._device)
            model.eval()
            self._model = model
            self._model_path = path
            self._files["model"] = self._file_ref(path, source_ref=source_ref)

            size_mb = os.path.getsize(path) / 1024 / 1024
            _log.info(f"Model loaded: {size_mb:.1f} MB on {self._device}")

            self._end_operation()
            return {"ok": True, "model_loaded": True, "device": self._device, "size_mb": round(size_mb, 1)}

        except Exception as e:
            _log.error(f"Failed to load model: {e}")
            return self._fail_operation(str(e), code="load_model_failed")

    def load_frames(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("load_frames", "Load frames")
            _log.info(f"Loading frames from {path}")

            if Image is None or np is None:
                return self._fail_operation("Pillow/numpy are not installed", code="dependency_missing")

            if not os.path.exists(path):
                return self._fail_operation(f"Frames path not found: {path}", code="file_not_found")

            if os.path.isfile(path) and path.endswith('.zip'):
                if self.frames_dir.exists():
                    shutil.rmtree(self.frames_dir)
                self.frames_dir.mkdir(exist_ok=True)

                with zipfile.ZipFile(path, 'r') as zip_ref:
                    zip_ref.extractall(self.frames_dir)

            self._frames = self._load_images_from_folder(str(self.frames_dir))
            self._current_frame_idx = 0
            self._prediction_cache = {}
            self._latest = None

            if len(self._frames) == 0:
                return self._fail_operation("No images found", code="empty_dataset")

            _log.info(f"Loaded {len(self._frames)} frames")
            self._files["frames"] = self._file_ref(path, source_ref=source_ref)
            self._end_operation()
            return {"ok": True, "total_frames": len(self._frames)}

        except Exception as e:
            _log.error(f"Failed to load frames: {e}")
            return self._fail_operation(str(e), code="load_frames_failed")

    def load_masks(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("load_masks", "Load masks")
            _log.info(f"Loading masks from {path}")

            if Image is None or np is None:
                return self._fail_operation("Pillow/numpy are not installed", code="dependency_missing")

            if not os.path.exists(path):
                return self._fail_operation(f"Masks path not found: {path}", code="file_not_found")

            if os.path.isfile(path) and path.endswith('.zip'):
                if self.masks_dir.exists():
                    shutil.rmtree(self.masks_dir)
                self.masks_dir.mkdir(exist_ok=True)

                with zipfile.ZipFile(path, 'r') as zip_ref:
                    zip_ref.extractall(self.masks_dir)

            self._masks = self._load_images_from_folder(str(self.masks_dir))

            _log.info(f"Loaded {len(self._masks)} masks")
            self._files["masks"] = self._file_ref(path, source_ref=source_ref)
            self._end_operation()
            return {"ok": True, "loaded_masks": len(self._masks)}

        except Exception as e:
            _log.error(f"Failed to load masks: {e}")
            return self._fail_operation(str(e), code="load_masks_failed")

    def load_metadata(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("load_metadata", "Load metadata")
            _log.info(f"Loading metadata from {path}")

            if not os.path.exists(path):
                return self._fail_operation(f"Metadata file not found: {path}", code="file_not_found")

            self._metadata = {}
            with open(path, 'r', encoding='utf-8') as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if line:
                        try:
                            data = json.loads(line)
                            frame_idx = data.get('frame_idx', i)
                            self._metadata[int(frame_idx)] = data
                        except json.JSONDecodeError:
                            continue

            _log.info(f"Loaded {len(self._metadata)} metadata entries")
            self._files["metadata"] = self._file_ref(path, source_ref=source_ref)
            self._end_operation()
            return {"ok": True, "loaded_metadata": len(self._metadata)}

        except Exception as e:
            _log.error(f"Failed to load metadata: {e}")
            return self._fail_operation(str(e), code="load_metadata_failed")

    def process_frame(self, frame_idx: int | None = None) -> dict[str, Any]:
        try:
            self._begin_operation("process_frame", "Process frame")
            if Image is None or np is None:
                return self._fail_operation("Pillow/numpy are not installed", code="dependency_missing")

            if not self._frames:
                return self._fail_operation("No frames loaded", code="frames_missing")

            frame_keys = sorted(self._frames.keys())

            if frame_idx is None:
                frame_idx = self._current_frame_idx

            if frame_idx >= len(frame_keys):
                frame_idx = 0

            frame_key = frame_keys[frame_idx]

            cache_key = str(frame_idx)
            if cache_key in self._prediction_cache:
                result = self._prediction_cache[cache_key]
                self._record_frame_result(result, total_frames=len(frame_keys))
                self._end_operation()
                return result

            frame = self._load_image_ref(self._frames[frame_key])

            gt_mask = None
            for key in self._masks:
                if frame_key in key or key in frame_key:
                    gt_mask = self._load_image_ref(self._masks[key])
                    break

            if self._model is not None:
                predicted_mask, _ = self._predict_with_model(frame)
                predicted_mask = Image.fromarray(predicted_mask)
            else:
                predicted_mask = self._create_dummy_prediction(frame)

            side_by_side = self._create_side_by_side_image(frame, gt_mask, predicted_mask)

            buffered = io.BytesIO()
            side_by_side.save(buffered, format="JPEG", quality=85, optimize=True)
            preview_base64 = base64.b64encode(buffered.getvalue()).decode()

            pred_ratio = float(np.mean(np.array(predicted_mask) > 0))

            true_ratio = None
            if frame_idx in self._metadata:
                true_ratio = self._metadata[frame_idx].get('ratio_bad_true')

            if pred_ratio >= self._alarm_threshold:
                status, status_color = "Alarm", "red"
            elif pred_ratio >= self._warning_threshold:
                status, status_color = "Warning", "yellow"
            else:
                status, status_color = "OK", "green"

            metrics = {"dice": 0, "iou": 0}
            if gt_mask is not None:
                dice_val, iou_val = self._calculate_metrics(predicted_mask, gt_mask)
                metrics = {"dice": round(dice_val, 4), "iou": round(iou_val, 4)}

            result = {
                "ok": True,
                "frame_idx": frame_idx,
                "frame_key": frame_key,
                "total_frames": len(frame_keys),
                "preview_base64": preview_base64,
                "pred_ratio": round(pred_ratio, 4),
                "true_ratio": round(true_ratio, 4) if true_ratio is not None else None,
                "status": status,
                "status_color": status_color,
                "metrics": metrics,
            }

            if len(self._prediction_cache) > 100:
                self._prediction_cache.pop(next(iter(self._prediction_cache)))
            self._prediction_cache[cache_key] = result
            self._record_frame_result(result, total_frames=len(frame_keys))
            self._end_operation()

            return result

        except Exception as e:
            _log.error(f"Failed to process frame: {e}")
            return self._fail_operation(str(e), code="frame_processing_failed")

    def reset(self) -> dict[str, Any]:
        self._begin_operation("reset", "Reset")
        self._current_frame_idx = 0
        self._prediction_cache = {}
        self._latest = None
        self._end_operation()
        return {"ok": True, "message": "Reset completed"}

    def clear(self) -> dict[str, Any]:
        self._model = None
        self._model_path = None
        self._frames = {}
        self._masks = {}
        self._metadata = {}
        self._current_frame_idx = 0
        self._prediction_cache = {}
        self._latest = None
        self._files = {
            "model": None,
            "frames": None,
            "masks": None,
            "metadata": None,
        }
        self._operation = {
            "id": None,
            "label": "",
            "progress": None,
            "error": None,
        }
        self.last_error = None

        for dir_path in [self.frames_dir, self.masks_dir]:
            if dir_path.exists():
                shutil.rmtree(dir_path)
                dir_path.mkdir(exist_ok=True)

        _log.info("Engine cleared")
        return {"ok": True, "message": "All data cleared"}

    def snapshot(self) -> dict[str, Any]:
        status = "error" if self.last_error else ("ready" if self._frames else "init")
        return {
            "ok": True,
            "status": status,
            "operation": dict(self._operation),
            "files": dict(self._files),
            "model": {
                "loaded": self._model is not None,
                "path": self._model_path,
                "device": self._device,
            },
            "stats": {
                "total_frames": len(self._frames),
                "loaded_masks": len(self._masks),
                "loaded_metadata": len(self._metadata),
                "model_loaded": self._model is not None,
                "current_frame": self._latest.get("frame_idx") if self._latest else None,
                "next_frame": self._current_frame_idx,
            },
            "thresholds": {
                "warning": self._warning_threshold,
                "alarm": self._alarm_threshold,
                "prediction": self._threshold,
            },
            "latest": self._latest or self._empty_latest(),
            "error": self.last_error,
            "history": [],
        }

    def frame_stream_payload(self, result: Mapping[str, Any]) -> dict[str, Any]:
        preview = str(result.get("preview_base64") or "")
        return {
            "ok": bool(result.get("ok", True)),
            "frame_idx": result.get("frame_idx"),
            "frame_key": result.get("frame_key"),
            "total_frames": result.get("total_frames") or len(self._frames),
            "image": {
                "mime": "image/jpeg",
                "encoding": "base64",
                "data": preview,
                "src": f"data:image/jpeg;base64,{preview}" if preview else "",
            },
            "prediction": {
                "pred_ratio": result.get("pred_ratio"),
                "true_ratio": result.get("true_ratio"),
            },
            "status": {
                "label": result.get("status"),
                "color": result.get("status_color"),
            },
            "metrics": dict(result.get("metrics") or {}),
            "ts": time.time(),
        }

    def metrics_stream_payload(self, result: Mapping[str, Any]) -> dict[str, Any]:
        metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
        return {
            "frame_idx": result.get("frame_idx"),
            "ts": time.time(),
            "series": {
                "pred_ratio": result.get("pred_ratio"),
                "true_ratio": result.get("true_ratio"),
                "dice": metrics.get("dice"),
                "iou": metrics.get("iou"),
            },
        }

    def _record_frame_result(self, result: Mapping[str, Any], *, total_frames: int) -> None:
        if not result.get("ok"):
            return
        frame_idx = int(result.get("frame_idx") or 0)
        metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
        pred_ratio = result.get("pred_ratio")
        true_ratio = result.get("true_ratio")
        description_parts = []
        if pred_ratio is not None:
            description_parts.append(f"pred={pred_ratio}")
        if true_ratio is not None:
            description_parts.append(f"true={true_ratio}")
        if metrics:
            description_parts.append(f"dice={metrics.get('dice', 0)}")
            description_parts.append(f"iou={metrics.get('iou', 0)}")
        self._latest = {
            "value": result.get("status") or "ok",
            "label": f"frame {frame_idx + 1}/{total_frames}" if total_frames else f"frame {frame_idx}",
            "description": " ".join(description_parts),
            "frame_idx": frame_idx,
            "frame_key": result.get("frame_key"),
            "total_frames": total_frames,
            "pred_ratio": pred_ratio,
            "true_ratio": true_ratio,
            "metrics": dict(metrics),
            "status": {
                "label": result.get("status"),
                "color": result.get("status_color"),
            },
            "ts": time.time(),
        }
        self.last_error = None
        self._current_frame_idx = (frame_idx + 1) % total_frames if total_frames > 0 else 0

    def _empty_latest(self) -> dict[str, Any]:
        return {
            "value": "--",
            "label": "",
            "description": "",
            "frame_idx": None,
            "frame_key": None,
            "total_frames": len(self._frames),
            "pred_ratio": None,
            "true_ratio": None,
            "metrics": {"dice": 0, "iou": 0},
            "status": {"label": "", "color": ""},
            "ts": None,
        }

    def _begin_operation(self, operation_id: str, label: str) -> None:
        self._operation = {
            "id": operation_id,
            "label": label,
            "progress": 0.0,
            "error": None,
        }

    def _end_operation(self) -> None:
        self._operation = {
            **self._operation,
            "progress": 1.0,
            "error": None,
        }
        self.last_error = None

    def _fail_operation(
        self,
        error: Any,
        *,
        code: str = "operation_failed",
        retryable: bool = False,
    ) -> dict[str, Any]:
        normalized = self._normalize_error(error, code=code, retryable=retryable)
        self.last_error = normalized
        self._operation = {
            **self._operation,
            "error": normalized,
        }
        return {"ok": False, "error": normalized}

    def _normalize_error(
        self,
        error: Any,
        *,
        code: str = "operation_failed",
        retryable: bool = False,
    ) -> dict[str, Any]:
        if isinstance(error, Mapping):
            message = str(error.get("message") or error.get("error") or error.get("code") or code)
            out: dict[str, Any] = {
                "code": str(error.get("code") or code),
                "message": message,
                "retryable": bool(error.get("retryable", retryable)),
                "ts": float(error.get("ts")) if isinstance(error.get("ts"), (int, float)) else time.time(),
            }
            if "details" in error:
                out["details"] = error.get("details")
            return out
        return {
            "code": code,
            "message": str(error or code),
            "retryable": retryable,
            "ts": time.time(),
        }

    def _file_ref(self, path: str, *, source_ref: Mapping[str, Any] | None = None) -> dict[str, Any]:
        file_path = Path(path)
        out = {
            "path": str(file_path),
            "name": file_path.name,
            "exists": file_path.exists(),
            "size_bytes": file_path.stat().st_size if file_path.exists() and file_path.is_file() else None,
        }
        if source_ref:
            out["source"] = dict(source_ref)
        return out

    def _load_images_from_folder(self, folder_path: str) -> dict[str, Path]:
        images: dict[str, Path] = {}
        folder = Path(folder_path)

        if Image is None:
            return images

        if not folder.exists():
            return images

        image_extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif'}

        for img_path in sorted(folder.rglob('*')):
            if img_path.suffix.lower() in image_extensions:
                images[img_path.stem] = img_path

        return images

    def _load_image_ref(self, img_path: Path) -> Image.Image:
        with Image.open(img_path) as img:
            return img.copy()

    def _create_dummy_prediction(self, frame: Image.Image) -> Image.Image:
        img_array = np.array(frame.convert('L'))
        threshold = np.mean(img_array) * 0.8
        pred_mask = (img_array < threshold).astype(np.uint8) * 255
        return Image.fromarray(pred_mask)

    def _predict_with_model(self, frame: Image.Image):
        img_tensor = TF.to_tensor(frame).unsqueeze(0).to(self._device)

        with torch.no_grad():
            if self._device == 'cuda':
                with torch.amp.autocast("cuda"):
                    logits = self._model(img_tensor)["out"]
            else:
                logits = self._model(img_tensor)["out"]

            prob = torch.sigmoid(logits)[0, 0].cpu().numpy()
            pred = (prob > self._threshold).astype(np.uint8) * 255

        return pred, prob

    def _create_side_by_side_image(self, original: Image.Image, gt_mask: Image.Image | None = None, pred_mask: Image.Image | None = None) -> Image.Image:
        if original.mode != 'RGB':
            original = original.convert('RGB')
        original_arr = np.array(original)

        h, w = original_arr.shape[:2]

        panel1 = original_arr.copy()

        panel2 = np.zeros((h, w, 3), dtype=np.uint8)
        if gt_mask is not None:
            gt_arr = np.array(gt_mask)
            if len(gt_arr.shape) == 3:
                gt_arr = gt_arr[:, :, 0]
            if gt_arr.max() > 0:
                gt_arr = (gt_arr > 30).astype(np.uint8) * 255
            panel2[gt_arr > 128] = [255, 255, 255]

        panel3 = np.zeros((h, w, 3), dtype=np.uint8)
        if pred_mask is not None:
            pred_arr = np.array(pred_mask)
            if len(pred_arr.shape) == 3:
                pred_arr = pred_arr[:, :, 0]
            if pred_arr.max() > 0:
                pred_arr = (pred_arr > 30).astype(np.uint8) * 255
            panel3[pred_arr > 128] = [255, 255, 255]

        panel4 = original_arr.copy()
        if pred_mask is not None:
            pred_arr = np.array(pred_mask)
            if len(pred_arr.shape) == 3:
                pred_arr = pred_arr[:, :, 0]
            if pred_arr.max() > 0:
                mask = pred_arr > 30
                if mask.any():
                    panel4[mask] = [255, 0, 0]
                    alpha = 0.6
                    panel4[mask] = (alpha * panel4[mask] + (1 - alpha) * original_arr[mask]).astype(np.uint8)

        combined = np.concatenate([panel1, panel2, panel3, panel4], axis=1)
        return Image.fromarray(combined)

    def _calculate_metrics(self, pred_mask: Image.Image, gt_mask: Image.Image) -> tuple[float, float]:
        pred = (np.array(pred_mask) > 128).astype(np.uint8)
        gt = (np.array(gt_mask) > 128).astype(np.uint8)

        if len(pred.shape) == 3:
            pred = pred[:, :, 0]
        if len(gt.shape) == 3:
            gt = gt[:, :, 0]

        intersection = (pred & gt).sum()
        pred_sum = pred.sum()
        gt_sum = gt.sum()

        eps = 1e-6
        dice = (2.0 * intersection + eps) / (pred_sum + gt_sum + eps)
        iou = (intersection + eps) / (pred_sum + gt_sum - intersection + eps)

        return float(dice), float(iou)
