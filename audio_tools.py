"""Kitap Seslendir - FFmpeg yardımcıları (birleştirme ve M4B dönüşümü)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FFmpegError(Exception):
    """FFmpeg çağrısı başarısız olduğunda fırlatılır."""


def find_ffmpeg() -> str | None:
    """ffmpeg çalıştırılabilir dosyasını PATH ve yaygın kurulum yerlerinde arar."""
    found = shutil.which("ffmpeg")
    if found:
        return found

    candidates = [
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def find_ffprobe() -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if sibling.is_file():
            return str(sibling)
    return None


def _run(command: list[str], timeout: float = 1800) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FFmpegError("FFmpeg bulunamadı. Lütfen kurup PATH'e ekleyin.") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("FFmpeg işlemi zaman aşımına uğradı.") from exc


def ffmpeg_version(ffmpeg_path: str | None = None) -> str | None:
    executable = ffmpeg_path or find_ffmpeg()
    if not executable:
        return None
    result = _run([executable, "-version"], timeout=20)
    if result.returncode != 0:
        return None
    first_line = (result.stdout or "").splitlines()[0] if result.stdout else ""
    marker = "ffmpeg version "
    if marker in first_line:
        return first_line.split(marker, 1)[1].split()[0]
    return first_line.strip() or None


def _concat_line(path: Path) -> str:
    # concat demuxer biçimi: file '...' ; tek tırnak kaçışı '\''
    uri = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{uri}'"


def concat_mp3(parts: list[str | Path], output: str | Path) -> Path:
    """MP3 parçalarını sırayla birleştirir; gerekirse yeniden kodlar."""
    if not parts:
        raise FFmpegError("Birleştirilecek ses parçası yok.")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FFmpegError("FFmpeg bulunamadı. Lütfen kurup PATH'e ekleyin.")

    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_path = out_path.with_name(out_path.stem + "_concat.txt")
    list_path.write_text(
        "\n".join(_concat_line(Path(part)) for part in parts) + "\n",
        encoding="utf-8",
    )

    try:
        base = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
        ]
        result = _run(base + ["-c", "copy", str(out_path)])

        if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            # Farklı kodlayıcı ayarlarına sahip parçalar için yeniden kodla.
            result = _run(
                base
                + [
                    "-c:a", "libmp3lame",
                    "-b:a", "64k",
                    "-ar", "24000",
                    "-ac", "1",
                    str(out_path),
                ]
            )
            if result.returncode != 0 or not out_path.exists():
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                message = detail[-1] if detail else "bilinmeyen hata"
                raise FFmpegError(f"Ses parçaları birleştirilemedi: {message}")
    finally:
        try:
            list_path.unlink()
        except OSError:
            pass

    return out_path


def to_m4b(
    source: str | Path,
    output: str | Path,
    title: str,
    author: str = "",
) -> Path:
    """Birleştirilmiş sesi M4B (AAC) dosyasına dönüştürür."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FFmpegError("FFmpeg bulunamadı. Lütfen kurup PATH'e ekleyin.")

    source_path = Path(source)
    out_path = Path(output)
    if not source_path.exists():
        raise FFmpegError("M4B dönüşümü için kaynak ses dosyası bulunamadı.")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source_path),
        "-c:a", "aac",
        "-b:a", "64k",
        "-ar", "24000",
        "-ac", "1",
        "-metadata", f"title={title}",
        "-metadata", "album=Kitap Seslendir",
        "-metadata", "genre=Audiobook",
    ]
    if author:
        command += ["-metadata", f"artist={author}", "-metadata", f"album_artist={author}"]
    command += ["-movflags", "+faststart", "-f", "ipod", str(out_path)]

    result = _run(command)
    if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        message = detail[-1] if detail else "bilinmeyen hata"
        raise FFmpegError(f"M4B dosyası oluşturulamadı: {message}")

    return out_path


def media_duration(path: str | Path) -> float | None:
    """Ses/video süresini saniye cinsinden döndürür (ffprobe yoksa None)."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    result = _run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None
