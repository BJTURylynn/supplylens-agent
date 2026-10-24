"""Assemble unmodified real UI screenshots into clearly labelled frame demos."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEMOS = ROOT / "docs/demos"
SEQUENCES = {
    "01-overdue": ["00-home.png", "01-overdue.png", "01-tools.png"],
    "02-followup": ["01-overdue.png", "02-followup.png"],
    "03-missing-data": ["00-home.png", "03-missing-data.png", "03-gap.png"],
}


def main():
    for name, filenames in SEQUENCES.items():
        images = [Image.open(DEMOS / filename).convert("RGB") for filename in filenames]
        try:
            if len({image.size for image in images}) != 1:
                raise ValueError("Source screenshots must have identical dimensions; do not resize UI evidence.")
            images[0].save(DEMOS / f"{name}.gif", save_all=True, append_images=images[1:],
                           duration=2500, loop=0, optimize=False)
        finally:
            for image in images:
                image.close()
        print(f"Created {name}.gif from {len(filenames)} real screenshots")


if __name__ == "__main__":
    main()

