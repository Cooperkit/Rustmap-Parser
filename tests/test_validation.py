import base64
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from rustmap.validation import compare_files


def write_heatmap(path: Path, array: np.ndarray) -> None:
    path.write_text(json.dumps({"heatmaps": {
        "test": base64.b64encode(array.tobytes()).decode("ascii")
    }}), encoding="utf-8")


class ValidationTests(unittest.TestCase):
    def test_exact_and_flipped_comparisons(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = np.arange(64, dtype=np.uint8).reshape(8,8)
            write_heatmap(root/"reference.json", reference)
            write_heatmap(root/"exact.json", reference)
            exact = compare_files(root/"exact.json", root/"reference.json", root/"exact", 8)
            self.assertTrue(exact["summary"]["all_common_exact"])
            write_heatmap(root/"flipped.json", reference[:,::-1])
            flipped = compare_files(root/"flipped.json", root/"reference.json", root/"flipped", 8)
            result = flipped["categories"]["test"]
            self.assertEqual(result["best_transform"], "flip_x")
            self.assertTrue(result["best_transform_metrics"]["exact"])


if __name__ == "__main__":
    unittest.main()
