# Audio Silence Splitter

Split a local recording without modifying the original. The tool scans the first audio stream using FFmpeg's `silencedetect` filter. Each qualifying silence gap contributes one candidate cut at its midpoint; silence is not removed.

## Install

Save the script as `audio_silence_splitter.py`. Use Python 3.7+ and install a recent FFmpeg with `silencedetect` support. Check that `ffmpeg -version` works in the same terminal. No Python packages or `ffprobe` are required.

## Usage

    python audio_silence_splitter.py --help
    python audio_silence_splitter.py "meeting.m4a" --dry-run
    python audio_silence_splitter.py "meeting.m4a" --target-chunk 15 --min-silence 1.5 --threshold -38 --out-dir "meeting parts"

Defaults: 10-minute target, 1-second minimum gap, −40 dB threshold, and `chunks` as the output directory. Dry-run performs the scan and prints planned ranges in seconds without creating the output directory.

## Behavior and limits

- From each chunk start, the tool chooses the remaining midpoint or end of file closest to one target length ahead. The target is not a maximum: a recording without qualifying gaps stays in one piece.
- Scanning decodes the full audio stream. Extraction uses stream copy first, so actual boundaries may differ from the plan due to seeking, packet boundaries, or codec delay.
- Common audio filename extensions are retained when stream copy succeeds; other inputs use a Matroska audio container (`.mka`). If copying fails, the tool tries 24-bit PCM WAV for that chunk, which may require considerably more space. Video, cover art, chapters, and other streams are not copied.
- Use a fresh output directory for each run. Chunks are staged on the destination filesystem and published using hard links so existing files are never overwritten. The filesystem must support hard links. Publication is atomic per file, not per recording: an interruption during publication can leave some completed chunks.
- Intended for ordinary seekable local files with continuous timestamps. Network input protocols are disabled.
