#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_START = 25472
DEFAULT_END = 25550
DEFAULT_BASE_URL = "https://eol.jsc.nasa.gov/DatabaseImages/ESC/large/ART002"
DEFAULT_RAW_DIR = Path("data/artemis/raw")
DEFAULT_FRAMES_DIR = Path("data/artemis/frames")
DEFAULT_MANIFESTS_DIR = Path("data/artemis/manifests")
DEFAULT_OUTPUT_DIR = Path("output/artemis")


@dataclass(frozen=True)
class PhotoRecord:
    sequence_number: int
    file_name: str
    file_path: Path
    capture_time: datetime
    delta_prev_photo_seconds: float | None
    repeat_count: int = 1


def run_command(args: list[str], *, capture: bool = False) -> str | None:
    result = subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if capture:
        return result.stdout
    return None


def require_tools() -> None:
    for tool in ("curl", "exiftool", "ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"Missing required tool: {tool}")


def photo_name(number: int) -> str:
    return f"ART002-E-{number}.JPG"


def photo_url(base_url: str, number: int) -> str:
    return f"{base_url}/{urllib.parse.quote(photo_name(number))}"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def download_images(args: argparse.Namespace) -> list[Path]:
    raw_dir = args.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    for number in range(args.start, args.end + 1):
        destination = raw_dir / photo_name(number)
        if destination.exists() and destination.stat().st_size > 0:
            downloaded.append(destination)
            print(f"[download] skip {destination.name}")
            continue

        ensure_parent(destination)
        with tempfile.NamedTemporaryFile(
            dir=raw_dir,
            prefix=f".{destination.stem}_",
            suffix=".part",
            delete=False,
        ) as tmp_file:
            temp_path = Path(tmp_file.name)

        url = photo_url(args.base_url, number)
        print(f"[download] fetch {destination.name}")
        try:
            run_command(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--retry",
                    "4",
                    "--retry-all-errors",
                    "--retry-delay",
                    "2",
                    "--silent",
                    "--show-error",
                    "--output",
                    str(temp_path),
                    url,
                ]
            )
            if temp_path.stat().st_size == 0:
                raise RuntimeError(f"Downloaded empty file for {destination.name}")
            temp_path.replace(destination)
            downloaded.append(destination)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    return downloaded


def _parse_offset(raw_offset: str | None) -> timezone:
    if not raw_offset:
        return timezone.utc
    sign = 1 if raw_offset[0] == "+" else -1
    hours_str, minutes_str = raw_offset[1:].split(":")
    return timezone(sign * timedelta(hours=int(hours_str), minutes=int(minutes_str)))


def _compose_capture_time(meta: dict[str, object], file_path: Path) -> datetime:
    raw_dt = meta.get("DateTimeOriginal") or meta.get("CreateDate")
    if not raw_dt:
        raise ValueError(f"Missing capture time in {file_path.name}")

    raw_subsec = (
        meta.get("SubSecTimeOriginal")
        or meta.get("SubSecTimeDigitized")
        or meta.get("SubSecTime")
        or "0"
    )
    raw_offset = (
        meta.get("OffsetTimeOriginal")
        or meta.get("OffsetTimeDigitized")
        or meta.get("OffsetTime")
        or "+00:00"
    )

    base_dt = datetime.strptime(str(raw_dt), "%Y:%m:%d %H:%M:%S")
    fraction = str(raw_subsec).strip()
    if fraction:
        fraction = "".join(ch for ch in fraction if ch.isdigit())[:6]
        microseconds = int(fraction.ljust(6, "0")) if fraction else 0
    else:
        microseconds = 0
    return base_dt.replace(microsecond=microseconds, tzinfo=_parse_offset(str(raw_offset)))


def collect_photo_records(args: argparse.Namespace) -> list[PhotoRecord]:
    files = [args.raw_dir / photo_name(number) for number in range(args.start, args.end + 1)]
    missing = [file_path.name for file_path in files if not file_path.exists()]
    if missing:
        raise SystemExit(f"Missing downloaded files: {', '.join(missing[:5])}")

    output = run_command(
        [
            "exiftool",
            "-j",
            "-DateTimeOriginal",
            "-SubSecTimeOriginal",
            "-OffsetTimeOriginal",
            "-OffsetTime",
            "-CreateDate",
            "-SubSecTimeDigitized",
            "-OffsetTimeDigitized",
            *[str(file_path) for file_path in files],
        ],
        capture=True,
    )
    assert output is not None
    metadata = json.loads(output)

    unsorted_records: list[tuple[int, Path, datetime]] = []
    for file_path, meta in zip(files, metadata, strict=True):
        capture_time = _compose_capture_time(meta, file_path)
        number = int(file_path.stem.split("-")[-1])
        unsorted_records.append((number, file_path, capture_time))

    unsorted_records.sort(key=lambda item: (item[2], item[0]))

    records: list[PhotoRecord] = []
    previous_time: datetime | None = None
    for number, file_path, capture_time in unsorted_records:
        delta_prev_photo = None
        if previous_time is not None:
            delta_prev_photo = (capture_time - previous_time).total_seconds()
        records.append(
            PhotoRecord(
                sequence_number=number,
                file_name=file_path.name,
                file_path=file_path,
                capture_time=capture_time,
                delta_prev_photo_seconds=delta_prev_photo,
            )
        )
        previous_time = capture_time

    return records


def choose_base_interval_seconds(records: list[PhotoRecord]) -> int:
    rounded_intervals = [
        int(round(record.delta_prev_photo_seconds))
        for record in records[1:]
        if record.delta_prev_photo_seconds and record.delta_prev_photo_seconds > 0
    ]
    if not rounded_intervals:
        raise SystemExit("Unable to determine base interval from capture timestamps")

    counts = Counter(rounded_intervals)
    base_interval, _ = min(counts.items(), key=lambda item: (-item[1], item[0]))
    if base_interval <= 0:
        raise SystemExit(f"Invalid base interval: {base_interval}")
    return base_interval


def apply_repeat_counts(records: list[PhotoRecord], base_interval_seconds: int) -> list[PhotoRecord]:
    tolerance = max(1.0, base_interval_seconds * 0.10)
    expanded_records: list[PhotoRecord] = []

    for index, record in enumerate(records):
        repeat_count = 1
        if index < len(records) - 1 and record.delta_prev_photo_seconds is not None:
            pass

        if index < len(records) - 1:
            next_delta = records[index + 1].delta_prev_photo_seconds
            if next_delta is not None and next_delta > 0:
                multiple = max(1, int(round(next_delta / base_interval_seconds)))
                expected_gap = multiple * base_interval_seconds
                if abs(next_delta - expected_gap) <= tolerance:
                    repeat_count = multiple

        expanded_records.append(
            PhotoRecord(
                sequence_number=record.sequence_number,
                file_name=record.file_name,
                file_path=record.file_path,
                capture_time=record.capture_time,
                delta_prev_photo_seconds=record.delta_prev_photo_seconds,
                repeat_count=max(1, repeat_count),
            )
        )

    return expanded_records


def write_photo_manifest(
    records: list[PhotoRecord],
    base_interval_seconds: int,
    manifests_dir: Path,
) -> Path:
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifests_dir / "photo_manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "sequence_number",
                "source_file",
                "capture_time",
                "delta_prev_photo_seconds",
                "repeat_count",
                "base_interval_seconds",
            ]
        )
        for record in records:
            writer.writerow(
                [
                    record.sequence_number,
                    record.file_name,
                    record.capture_time.isoformat(),
                    "" if record.delta_prev_photo_seconds is None else f"{record.delta_prev_photo_seconds:.2f}",
                    record.repeat_count,
                    base_interval_seconds,
                ]
            )
    return manifest_path


def rebuild_frames_dir(frames_dir: Path) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    for existing in frames_dir.iterdir():
        if existing.is_symlink() or existing.is_file():
            existing.unlink()
        elif existing.is_dir():
            shutil.rmtree(existing)


def build_frame_sequence(
    records: list[PhotoRecord],
    base_interval_seconds: int,
    frames_dir: Path,
    manifests_dir: Path,
) -> tuple[int, Path]:
    rebuild_frames_dir(frames_dir)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifests_dir / "frame_manifest.csv"

    frame_number = 0
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "frame_number",
                "source_file",
                "capture_time",
                "delta_prev_photo_seconds",
                "delta_prev_frame_seconds",
                "is_hold",
            ]
        )

        for record in records:
            for repeat_index in range(record.repeat_count):
                frame_number += 1
                link_path = frames_dir / f"frame_{frame_number:06d}.jpg"
                link_path.symlink_to(record.file_path.resolve())
                is_hold = repeat_index > 0
                writer.writerow(
                    [
                        frame_number,
                        record.file_name,
                        record.capture_time.isoformat(),
                        "0.00"
                        if is_hold
                        else (
                            ""
                            if record.delta_prev_photo_seconds is None
                            else f"{record.delta_prev_photo_seconds:.2f}"
                        ),
                        "" if frame_number == 1 else f"{base_interval_seconds:.2f}",
                        "true" if is_hold else "false",
                    ]
                )

    return frame_number, manifest_path


def palette_render(input_pattern: str, framerate: str, vf_chain: str, output_path: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as palette_file:
        palette_path = Path(palette_file.name)
    try:
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                framerate,
                "-i",
                input_pattern,
                "-vf",
                f"{vf_chain},palettegen=stats_mode=full",
                str(palette_path),
            ]
        )
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                framerate,
                "-i",
                input_pattern,
                "-i",
                str(palette_path),
                "-lavfi",
                f"{vf_chain}[x];[x][1:v]paletteuse=dither=sierra2_4a",
                str(output_path),
            ]
        )
    finally:
        palette_path.unlink(missing_ok=True)


def render_outputs(
    args: argparse.Namespace,
    base_interval_seconds: int,
    frame_count: int,
) -> list[Path]:
    if frame_count <= 0:
        raise SystemExit("No frames available to render")

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    input_pattern = str(args.frames_dir / "frame_%06d.jpg")
    playback_fps = str(args.output_fps)

    mp4_path = output_dir / "artemis-earth-timelapse-lossless.mp4"
    mkv_path = output_dir / "artemis-earth-timelapse-lossless.mkv"
    full_gif_path = output_dir / "artemis-earth-timelapse-full.gif"
    preview_gif_path = output_dir / "artemis-earth-timelapse-preview.gif"

    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            playback_fps,
            "-i",
            input_pattern,
            "-frames:v",
            str(frame_count),
            "-vf",
            "format=rgb24",
            "-c:v",
            "libx264rgb",
            "-crf",
            "0",
            "-preset",
            "veryslow",
            "-pix_fmt",
            "rgb24",
            str(mp4_path),
        ]
    )

    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            playback_fps,
            "-i",
            input_pattern,
            "-frames:v",
            str(frame_count),
            "-vf",
            "format=bgr0",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-slices",
            "16",
            "-slicecrc",
            "1",
            "-pix_fmt",
            "bgr0",
            str(mkv_path),
        ]
    )

    palette_render(input_pattern, str(args.full_gif_fps), "null", full_gif_path)
    palette_render(
        input_pattern,
        str(args.preview_gif_fps),
        f"scale={args.preview_width}:-1:flags=lanczos",
        preview_gif_path,
    )

    return [mp4_path, mkv_path, full_gif_path, preview_gif_path]


def manifest_stage(args: argparse.Namespace) -> tuple[list[PhotoRecord], int, int, Path]:
    records = collect_photo_records(args)
    base_interval_seconds = choose_base_interval_seconds(records)
    records = apply_repeat_counts(records, base_interval_seconds)
    write_photo_manifest(records, base_interval_seconds, args.manifests_dir)
    frame_count, manifest_path = build_frame_sequence(
        records,
        base_interval_seconds,
        args.frames_dir,
        args.manifests_dir,
    )
    return records, base_interval_seconds, frame_count, manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download and render the Artemis Earth timelapse")
    parser.add_argument("command", choices=("download", "manifest", "render", "all"))
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--end", type=int, default=DEFAULT_END)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--frames-dir", type=Path, default=DEFAULT_FRAMES_DIR)
    parser.add_argument("--manifests-dir", type=Path, default=DEFAULT_MANIFESTS_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output-fps", type=int, default=24)
    parser.add_argument("--full-gif-fps", type=int, default=24)
    parser.add_argument("--preview-gif-fps", type=int, default=24)
    parser.add_argument("--preview-width", type=int, default=540)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    require_tools()

    if args.command in {"download", "all"}:
        download_images(args)
        if args.command == "download":
            return 0

    if args.command in {"manifest", "all"}:
        records, base_interval_seconds, frame_count, manifest_path = manifest_stage(args)
        print(f"[manifest] photos={len(records)} base_interval={base_interval_seconds}s")
        print(f"[manifest] frames={frame_count} csv={manifest_path}")
        if args.command == "manifest":
            return 0

    if args.command == "render":
        records, base_interval_seconds, frame_count, manifest_path = manifest_stage(args)
        print(f"[manifest] photos={len(records)} base_interval={base_interval_seconds}s")
        print(f"[manifest] frames={frame_count} csv={manifest_path}")
        outputs = render_outputs(args, base_interval_seconds, frame_count)
        for output in outputs:
            print(f"[render] wrote {output}")
        return 0

    outputs = render_outputs(args, base_interval_seconds, frame_count)
    for output in outputs:
        print(f"[render] wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
