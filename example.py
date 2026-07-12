"""Example for an installed package: `python -m pip install -e .` first."""

from pathlib import Path

from rustmap_parser import ExportConfig, ExportOptions, RustMapExporter


# --- User configuration ----------------------------------------------------
MAP_PATH = Path(r"C:\path\to\your\procedural.map")
OUTPUT_DIR = Path("output/ExampleOutput")
# ---------------------------------------------------------------------------

def main() -> None:

    config = ExportConfig(
        map_path=MAP_PATH,
        output_dir=OUTPUT_DIR,
        exports=ExportOptions.all(),
        timing_debug=True,
    )

    result = RustMapExporter(config).run()

    print(f"World size: {result.world_size}")
    print(f"Full-size map: {result.full_map_image}")
    print(f"Map tiles: {result.map_tiles_dir} ({result.map_tile_count})")
    print(f"Completed in {result.elapsed_seconds:.2f} seconds")


if __name__ == "__main__":
    main()
