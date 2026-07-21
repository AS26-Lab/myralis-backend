from __future__ import annotations

import argparse
from pathlib import Path


SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a multiresolution .ico for MyralisBackend from a PNG."
    )
    parser.add_argument("--png", required=True, help="Source PNG path")
    parser.add_argument("--out", default="assets/icons/myralis_backend.ico")
    args = parser.parse_args()

    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required for this helper script. Install it separately and rerun."
        ) from exc

    png_path = Path(args.png).resolve()
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    image = Image.open(png_path).convert("RGBA")
    icon_sizes = []
    for size in SIZES:
        icon_sizes.append(image.resize((size, size), Image.Resampling.LANCZOS))
    icon_sizes[0].save(out_path, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"Wrote icon: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

