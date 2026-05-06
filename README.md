# MonshineLapse

Timelapse of Earth photos from the Artemis `ART002` sequence published by NASA EOL.

![Artemis Earth timelapse preview](assets/artemis-earth-preview-social.gif)

## What is in the repo

- `scripts/artemis_timelapse.py`: download, sort, expand held frames, and render outputs
- `data/artemis/manifests/photo_manifest.csv`: one row per downloaded photo with capture time and repeat count
- `data/artemis/manifests/frame_manifest.csv`: one row per rendered frame with source file, capture time, delta to previous photo, and delta to previous frame
- `assets/artemis-earth-preview-social.gif`: lightweight preview GIF intended for README/social sharing

## What is intentionally not tracked

To avoid repository bloat, the repo ignores:

- raw NASA JPEGs in `data/artemis/raw/`
- expanded frame symlinks in `data/artemis/frames/`
- heavy rendered outputs in `output/` such as the full-resolution GIF and the lossless MP4/MKV files

## Reproduce

Requirements:

- `uv`
- `curl`
- `exiftool`
- `ffmpeg`
- `ffprobe`
- ImageMagick (`magick`) if you want to regenerate the optimized social GIF variant

Run the full pipeline:

```bash
uv run python scripts/artemis_timelapse.py all
```

The default render settings generate:

- lossless MP4 at `output/artemis/artemis-earth-timelapse-lossless.mp4`
- lossless MKV at `output/artemis/artemis-earth-timelapse-lossless.mkv`
- full-resolution GIF at `output/artemis/artemis-earth-timelapse-full.gif`
- social-friendly preview GIF at `output/artemis/artemis-earth-timelapse-preview.gif`

The script sorts by EXIF capture time, detects the modal capture interval, and holds frames when a longer gap is close to an integer multiple of that cadence.

## Source

Image range used:

- `https://eol.jsc.nasa.gov/DatabaseImages/ESC/large/ART002/ART002-E-25472.JPG`
- through `https://eol.jsc.nasa.gov/DatabaseImages/ESC/large/ART002/ART002-E-25550.JPG`
