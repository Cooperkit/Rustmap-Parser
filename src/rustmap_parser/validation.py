"""Phase 5 comparator for Rust+ compatible map_data.json heatmaps."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .png import save_png


TRANSFORMS = {
    "identity": lambda x: x,
    "flip_x": lambda x: x[:, ::-1],
    "flip_z": lambda x: x[::-1, :],
    "rotate_180": lambda x: x[::-1, ::-1],
    "transpose": lambda x: x.T,
    "transpose_flip_x": lambda x: x.T[:, ::-1],
    "transpose_flip_z": lambda x: x.T[::-1, :],
    "transpose_rotate_180": lambda x: x.T[::-1, ::-1],
}


def load_heatmaps(path: str | Path, resolution: int = 512) -> dict[str, np.ndarray]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    encoded = payload.get("heatmaps", payload)
    if not isinstance(encoded, dict):
        raise ValueError(f"{source}: heatmaps must be a JSON object")
    result = {}
    expected = resolution * resolution
    for name, value in encoded.items():
        if not isinstance(value, str):
            continue
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ValueError(f"{source}: {name!r} is not valid Base64") from exc
        if len(raw) != expected:
            raise ValueError(
                f"{source}: {name!r} decoded to {len(raw):,} bytes; expected {expected:,}"
            )
        result[str(name)] = np.frombuffer(raw, dtype=np.uint8).reshape(resolution, resolution)
    return result


def _metrics(candidate: np.ndarray, reference: np.ndarray) -> dict:
    delta = candidate.astype(np.int16) - reference.astype(np.int16)
    absolute = np.abs(delta)
    return {
        "exact": bool(np.array_equal(candidate, reference)),
        "equal_pixels": int(np.count_nonzero(delta == 0)),
        "different_pixels": int(np.count_nonzero(delta)),
        "different_percent": float(np.count_nonzero(delta) * 100 / delta.size),
        "mean_absolute_error": float(absolute.mean()),
        "root_mean_square_error": float(np.sqrt(np.mean(delta.astype(np.float64) ** 2))),
        "maximum_absolute_error": int(absolute.max()),
        "candidate_nonzero": int(np.count_nonzero(candidate)),
        "reference_nonzero": int(np.count_nonzero(reference)),
    }


def compare_files(candidate_path: str | Path, reference_path: str | Path,
                  output_dir: str | Path, resolution: int = 512) -> dict:
    candidate = load_heatmaps(candidate_path, resolution)
    reference = load_heatmaps(reference_path, resolution)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    common = sorted(candidate.keys() & reference.keys())
    report = {
        "candidate": str(Path(candidate_path).resolve()),
        "reference": str(Path(reference_path).resolve()),
        "resolution": resolution,
        "candidate_categories": sorted(candidate),
        "reference_categories": sorted(reference),
        "missing_from_candidate": sorted(reference.keys() - candidate.keys()),
        "extra_in_candidate": sorted(candidate.keys() - reference.keys()),
        "categories": {},
    }
    exact = 0
    for name in common:
        variants = {key: _metrics(fn(candidate[name]), reference[name])
                    for key, fn in TRANSFORMS.items()}
        best_name = min(variants, key=lambda key: (
            variants[key]["mean_absolute_error"], variants[key]["different_pixels"]
        ))
        best = TRANSFORMS[best_name](candidate[name])
        if variants["identity"]["exact"]:
            exact += 1
        diff = np.abs(best.astype(np.int16) - reference[name].astype(np.int16)).astype(np.uint8)
        save_png(Image.fromarray(diff, mode="L"), output / f"{name}_diff.png")
        report["categories"][name] = {
            "identity": variants["identity"], "best_transform": best_name,
            "best_transform_metrics": variants[best_name],
        }
    report["summary"] = {
        "common_category_count": len(common), "exact_category_count": exact,
        "all_common_exact": exact == len(common) and bool(common),
    }
    (output / "validation_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report
