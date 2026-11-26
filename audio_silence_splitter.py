#!/usr/bin/env python3
"""Split a local recording at silence gaps using FFmpeg."""

import argparse
import bisect
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path


NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
EVENT = re.compile(r"\bsilence_(start|end):\s*(" + NUMBER + r")")
PROGRESS = re.compile(r"^out_time_us=(\d+)$")
DURATION = re.compile(r"\bDuration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)\b")

AUDIO_EXTENSIONS = {
    ".aac", ".ac3", ".aiff", ".alac", ".eac3", ".flac", ".m4a",
    ".m4b", ".mka", ".mp2", ".mp3", ".mp4", ".oga", ".ogg",
    ".opus", ".wav", ".weba", ".wma",
}


class ToolError(Exception):
    """An expected operational failure."""


def finite(value):
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a number") from None
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("expected a finite number")
    return result


def positive(value):
    result = finite(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def cli():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("source", type=Path, help="local input audio file")
    parser.add_argument(
        "--out-dir", type=Path, default=Path("chunks"),
        help="destination directory",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="scan and print the planned cuts without writing files",
    )
    parser.add_argument(
        "--min-silence", type=positive, default=1.0, metavar="SECONDS",
        help="minimum continuous silence to be considered a split point",
    )
    parser.add_argument(
        "--threshold", type=finite, default=-40.0, metavar="DB",
        help="silence threshold in dB",
    )
    parser.add_argument(
        "--target-chunk", type=positive, default=10.0, metavar="MINUTES",
        help="preferred chunk length",
    )
    return parser


def run_ffmpeg(command, on_line=None):
    """Run ffmpeg, streaming its combined output line by line."""
    tail = deque(maxlen=30)
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        try:
            for raw in process.stdout:
                line = raw.strip()
                if line:
                    tail.append(line)
                if on_line:
                    on_line(line)
            status = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
    if status:
        raise ToolError(
            "FFmpeg exited with status {}:\n{}".format(
                status, "\n".join(tail) or "(no diagnostic output)"
            )
        )


def scan(base, source, threshold, minimum):
    """Run silencedetect and return (duration, [(start, end), ...])."""
    gaps = []
    pending_start = None
    duration = 0.0

    def consume(line):
        nonlocal pending_start, duration

        progress = PROGRESS.fullmatch(line)
        if progress:
            duration = max(duration, int(progress[1]) / 1_000_000)
            return

        duration_match = DURATION.search(line)
        if duration_match:
            hours, minutes, seconds = duration_match.groups()
            duration = max(
                duration,
                int(hours) * 3600 + int(minutes) * 60 + float(seconds),
            )
            return

        if "silencedetect" not in line:
            return

        for event in EVENT.finditer(line):
            value = float(event[2])
            if event[1] == "start":
                pending_start = value
            elif pending_start is not None:
                if value > pending_start:
                    gaps.append((pending_start, value))
                pending_start = None

    filt = "asetpts=PTS-STARTPTS,silencedetect=noise={}dB:duration={}".format(
        threshold, minimum
    )
    run_ffmpeg(
        base
        + [
            "-loglevel", "info",
            "-nostats",
            "-progress", "pipe:1",
            "-i", str(source),
            "-map", "0:a:0",
            "-af", filt,
            "-c:a", "pcm_s16le",
            "-f", "null",
            "-",
        ],
        consume,
    )

    if duration <= 0:
        raise ToolError(
            "no measurable audio duration (empty stream, unsupported format, "
            "or missing FFmpeg progress output)"
        )

    if pending_start is not None and pending_start < duration:
        gaps.append((pending_start, duration))

    return duration, gaps


def plan(duration, gaps, target):
    """Pick cut points nearest to the target chunk length."""
    midpoints = (
        (max(0.0, start) + min(duration, end)) / 2.0 for start, end in gaps
    )
    candidates = sorted({m for m in midpoints if 0.0 < m < duration}) + [duration]
    bounds = [0.0]
    first = 0
    while first < len(candidates):
        wanted = bounds[-1] + target
        i = bisect.bisect_left(candidates, wanted, lo=first)
        low = max(first, i - 1)
        high = min(len(candidates), i + 1)
        best = min(range(low, high), key=lambda j: abs(candidates[j] - wanted))
        bounds.append(candidates[best])
        first = best + 1
    return bounds


def publish(src, dst):
    """Publish src to dst without overwriting.

    On POSIX this is atomic via hardlink + unlink.  On platforms where
    hardlinks are unavailable we fall back to a same-filesystem rename
    after an existence check.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise ToolError("refusing to overwrite: {}".format(dst))
    except OSError:
        if os.path.lexists(dst):
            raise ToolError("refusing to overwrite: {}".format(dst))
        os.rename(src, dst)
    else:
        os.unlink(src)


def extract(base, source, output_dir, bounds):
    """Write chunk files for the computed bounds."""
    output_dir.mkdir(parents=True, exist_ok=True)

    suffix = source.suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        suffix = ".mka"

    num_chunks = len(bounds) - 1
    width = max(3, len(str(num_chunks)))

    with tempfile.TemporaryDirectory(
        prefix=".silence-split-", dir=output_dir
    ) as temp_dir:
        temp = Path(temp_dir)
        staged = []
        for index, (left, right) in enumerate(zip(bounds, bounds[1:]), 1):
            stem = "chunk-{:0{width}d}".format(index, width=width)
            path = temp / (stem + suffix)
            common_args = base + [
                "-loglevel", "error",
                "-n",
                "-ss", "{:.9f}".format(left),
                "-i", str(source),
                "-t", "{:.9f}".format(right - left),
                "-map", "0:a:0",
                "-map_metadata", "-1",
                "-map_chapters", "-1",
            ]

            try:
                run_ffmpeg(common_args + ["-c:a", "copy", str(path)])
            except ToolError as exc:
                if path.exists():
                    path.unlink()
                path = path.with_suffix(".wav")
                print(
                    "warning: stream copy failed for {}; re-encoding to PCM WAV:\n{}"
                    .format(stem, exc),
                    file=sys.stderr,
                )
                run_ffmpeg(
                    common_args
                    + ["-c:a", "pcm_s24le", "-rf64", "auto", str(path)]
                )

            staged.append((path, output_dir / path.name))

        for _, destination in staged:
            if os.path.lexists(destination):
                raise ToolError("refusing to overwrite: {}".format(destination))

        for path, destination in staged:
            publish(path, destination)
            print("wrote {}".format(destination))


def main():
    parser = cli()
    args = parser.parse_args()

    target_seconds = args.target_chunk * 60.0
    if not math.isfinite(target_seconds):
        raise ToolError("--target-chunk is too large")

    source = args.source.resolve(strict=True)
    if not source.is_file():
        raise ToolError("not a regular file: {}".format(source))

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ToolError(
            "ffmpeg not found on PATH; install FFmpeg and try again"
        )

    base = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-xerror",
        "-protocol_whitelist", "file,pipe",
    ]

    duration, gaps = scan(base, source, args.threshold, args.min_silence)
    bounds = plan(duration, gaps, target_seconds)

    if args.dry_run:
        num_chunks = len(bounds) - 1
        width = max(3, len(str(num_chunks)))
        for index, (left, right) in enumerate(zip(bounds, bounds[1:]), 1):
            print(
                "chunk-{:0{width}d}  {:.6f} -> {:.6f} seconds".format(
                    index, left, right, width=width
                )
            )
    else:
        extract(base, source, args.out_dir.resolve(), bounds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
    except (ToolError, OSError) as error:
        print("error: {}".format(error), file=sys.stderr)
        sys.exit(1)
