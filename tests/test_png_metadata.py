from pathlib import Path

from PIL import Image

from rustmap_parser.png import PNG_SOURCE_KEY, PROJECT_URL, save_png


def test_save_png_embeds_project_source_without_changing_pixels(tmp_path: Path) -> None:
    path = tmp_path / "output.png"
    source = Image.new("RGBA", (3, 2), (12, 34, 56, 78))

    save_png(source, path)

    with Image.open(path) as exported:
        assert exported.info[PNG_SOURCE_KEY] == PROJECT_URL
        assert exported.mode == source.mode
        assert exported.size == source.size
        assert exported.tobytes() == source.tobytes()
