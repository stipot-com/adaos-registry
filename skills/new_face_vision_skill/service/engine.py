import os
import json
import zipfile
import shutil
import io
import base64
import tempfile
import logging
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

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
        self._frames = {}
        self._masks = {}
        self._metadata = {}
        self._current_frame_idx = 0
        self._threshold = 0.35
        self._warning_threshold = 0.05
        self._alarm_threshold = 0.15
        self._prediction_cache = {}
        self._model_path = None

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

        if frames_path:
            load_result = self.load_frames(frames_path)
            result.update(load_result)

        if masks_path:
            load_result = self.load_masks(masks_path)
            result.update(load_result)

        if metadata_path:
            load_result = self.load_metadata(metadata_path)
            result.update(load_result)

        return result

    def load_model(self, path: str) -> dict[str, Any]:
        try:
            _log.info(f"Loading model from {path}")

            if torch is None or nn is None or torchvision is None or TF is None:
                return {"ok": False, "error": "torch/torchvision are not installed"}

            if not os.path.exists(path):
                return {"ok": False, "error": f"Model file not found: {path}"}

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

            size_mb = os.path.getsize(path) / 1024 / 1024
            _log.info(f"Model loaded: {size_mb:.1f} MB on {self._device}")

            return {"ok": True, "model_loaded": True, "device": self._device, "size_mb": round(size_mb, 1)}

        except Exception as e:
            _log.error(f"Failed to load model: {e}")
            return {"ok": False, "error": str(e)}

    def load_frames(self, path: str) -> dict[str, Any]:
        try:
            _log.info(f"Loading frames from {path}")

            if not os.path.exists(path):
                return {"ok": False, "error": f"Frames path not found: {path}"}

            if os.path.isfile(path) and path.endswith('.zip'):
                if self.frames_dir.exists():
                    shutil.rmtree(self.frames_dir)
                self.frames_dir.mkdir(exist_ok=True)

                with zipfile.ZipFile(path, 'r') as zip_ref:
                    zip_ref.extractall(self.frames_dir)

            self._frames = self._load_images_from_folder(str(self.frames_dir))

            if len(self._frames) == 0:
                return {"ok": False, "error": "No images found"}

            _log.info(f"Loaded {len(self._frames)} frames")
            return {"ok": True, "total_frames": len(self._frames)}

        except Exception as e:
            _log.error(f"Failed to load frames: {e}")
            return {"ok": False, "error": str(e)}

    def load_masks(self, path: str) -> dict[str, Any]:
        try:
            _log.info(f"Loading masks from {path}")

            if not os.path.exists(path):
                return {"ok": False, "error": f"Masks path not found: {path}"}

            if os.path.isfile(path) and path.endswith('.zip'):
                if self.masks_dir.exists():
                    shutil.rmtree(self.masks_dir)
                self.masks_dir.mkdir(exist_ok=True)

                with zipfile.ZipFile(path, 'r') as zip_ref:
                    zip_ref.extractall(self.masks_dir)

            self._masks = self._load_images_from_folder(str(self.masks_dir))

            _log.info(f"Loaded {len(self._masks)} masks")
            return {"ok": True, "loaded_masks": len(self._masks)}

        except Exception as e:
            _log.error(f"Failed to load masks: {e}")
            return {"ok": False, "error": str(e)}

    def load_metadata(self, path: str) -> dict[str, Any]:
        try:
            _log.info(f"Loading metadata from {path}")

            if not os.path.exists(path):
                return {"ok": False, "error": f"Metadata file not found: {path}"}

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
            return {"ok": True, "loaded_metadata": len(self._metadata)}

        except Exception as e:
            _log.error(f"Failed to load metadata: {e}")
            return {"ok": False, "error": str(e)}

    def process_frame(self, frame_idx: int | None = None) -> dict[str, Any]:
        try:
            if not self._frames:
                return {"ok": False, "error": "No frames loaded"}

            frame_keys = sorted(self._frames.keys())

            if frame_idx is None:
                frame_idx = self._current_frame_idx

            if frame_idx >= len(frame_keys):
                frame_idx = 0

            self._current_frame_idx = frame_idx
            frame_key = frame_keys[frame_idx]
            frame = self._frames[frame_key]

            cache_key = str(frame_idx)
            if cache_key in self._prediction_cache:
                return self._prediction_cache[cache_key]

            gt_mask = None
            for key in self._masks:
                if frame_key in key or key in frame_key:
                    gt_mask = self._masks[key]
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
                status, status_color = "Тревога", "red"
            elif pred_ratio >= self._warning_threshold:
                status, status_color = "Предупреждение", "yellow"
            else:
                status, status_color = "Норма", "green"

            metrics = {"dice": 0, "iou": 0}
            if gt_mask is not None:
                dice_val, iou_val = self._calculate_metrics(predicted_mask, gt_mask)
                metrics = {"dice": round(dice_val, 4), "iou": round(iou_val, 4)}

            result = {
                "ok": True,
                "frame_idx": frame_idx,
                "preview_base64": preview_base64,
                "pred_ratio": round(pred_ratio, 4),
                "true_ratio": round(true_ratio, 4) if true_ratio else None,
                "status": status,
                "status_color": status_color,
                "metrics": metrics,
            }

            if len(self._prediction_cache) > 100:
                self._prediction_cache.pop(next(iter(self._prediction_cache)))
            self._prediction_cache[cache_key] = result

            return result

        except Exception as e:
            _log.error(f"Failed to process frame: {e}")
            return {"ok": False, "error": str(e)}

    def reset(self) -> dict[str, Any]:
        self._current_frame_idx = 0
        self._prediction_cache = {}
        return {"ok": True, "message": "Reset completed"}

    def clear(self) -> dict[str, Any]:
        self._model = None
        self._model_path = None
        self._frames = {}
        self._masks = {}
        self._metadata = {}
        self._current_frame_idx = 0
        self._prediction_cache = {}

        for dir_path in [self.frames_dir, self.masks_dir]:
            if dir_path.exists():
                shutil.rmtree(dir_path)
                dir_path.mkdir(exist_ok=True)

        _log.info("Engine cleared")
        return {"ok": True, "message": "All data cleared"}

    def snapshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "stats": {
                "total_frames": len(self._frames),
                "loaded_masks": len(self._masks),
                "loaded_metadata": len(self._metadata),
                "model_loaded": self._model is not None,
                "current_frame": self._current_frame_idx,
            },
            "status": "ready" if self._frames else "init",
            "model_path": self._model_path,
            "thresholds": {
                "warning": self._warning_threshold,
                "alarm": self._alarm_threshold,
                "prediction": self._threshold,
            },
        }

    def _load_images_from_folder(self, folder_path: str) -> dict[str, Image.Image]:
        images = {}
        folder = Path(folder_path)

        if not folder.exists():
            return images

        image_extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif'}

        for img_path in sorted(folder.rglob('*')):
            if img_path.suffix.lower() in image_extensions:
                try:
                    with Image.open(img_path) as img:
                        images[img_path.stem] = img.copy()
                except Exception:
                    continue

        return images

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
