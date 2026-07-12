"""Shared PNG encoding with project provenance metadata."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, PngImagePlugin


PROJECT_URL = "https://github.com/Cooperkit/Rustmap-Parser"
PNG_SOURCE_KEY = "Source"


def save_png(image: Image.Image, path: str | Path, **save_options: object) -> None:
    """Save a PNG with a non-visible link back to this project's source."""
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text(PNG_SOURCE_KEY, PROJECT_URL)
    image.save(path, "PNG", pnginfo=metadata, **save_options)
