"""Kitap Seslendir - edge-tts ses motoru ve metin parçalama."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Any

import edge_tts

from extractors import normalize_text

DEFAULT_VOICE = "tr-TR-AhmetNeural"

# Metin ~3000 karakterlik parçalara ayrılır; tek bir cümle bile bu sınırı
# aşarsa kelime sınırından bölünür.
MAX_CHUNK_CHARS = 3000

# edge-tts, metni cümle cümle işlediği için doğal bir örnek yeterli.
PREVIEW_TEXT = (
    "Merhaba! Bu, seçtiğiniz sesin kısa bir örneğidir. "
    "Kitaplarınızı dinlemeye hazır olduğunuzda dönüştürme düğmesine "
    "basabilirsiniz. İyi dinlemeler!"
)

FALLBACK_VOICES: list[tuple[str, str]] = [
    ("🇹🇷 Ahmet · tr-TR · erkek", "tr-TR-AhmetNeural"),
    ("🇹🇷 Emel · tr-TR · kadın", "tr-TR-EmelNeural"),
    ("🌍 Andrew · en-US · çok dilli · erkek", "en-US-AndrewMultilingualNeural"),
    ("🌍 Ava · en-US · çok dilli · kadın", "en-US-AvaMultilingualNeural"),
    ("🌍 Brian · en-US · çok dilli · erkek", "en-US-BrianMultilingualNeural"),
    ("🌍 Emma · en-US · çok dilli · kadın", "en-US-EmmaMultilingualNeural"),
    ("🌍 Vivienne · fr-FR · çok dilli · kadın", "fr-FR-VivienneMultilingualNeural"),
    ("🌍 Remy · fr-FR · çok dilli · erkek", "fr-FR-RemyMultilingualNeural"),
    ("🌍 Giuseppe · it-IT · çok dilli · erkek", "it-IT-GiuseppeMultilingualNeural"),
    ("🌍 Thalita · pt-BR · çok dilli · kadın", "pt-BR-ThalitaMultilingualNeural"),
]

_voice_cache: tuple[list[tuple[str, str]], bool, str] | None = None


# ---------------------------------------------------------------------------
# Asenkron köprü (Gradio işleyicileri normal iş parçacıklarında çalışır)
# ---------------------------------------------------------------------------

def _run_async(coro: Any, timeout: float | None = None) -> Any:
    """Verilen coroutine'i ayrı bir iş parçacığında kendi event loop'unda çalıştırır."""
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(
                asyncio.wait_for(coro, timeout) if timeout else coro
            )
        except BaseException as exc:  # noqa: BLE001 - çağırana aktarılacak
            box["error"] = exc

    thread = threading.Thread(target=runner, name="edge-tts-worker", daemon=True)
    thread.start()
    thread.join(None if timeout is None else timeout + 15)

    if thread.is_alive():
        raise TimeoutError(f"Ses işlemi {timeout:.0f} saniyede tamamlanmadı.")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# Ses listesi
# ---------------------------------------------------------------------------

def _voice_label(voice: dict[str, Any]) -> str:
    short_name = voice.get("ShortName") or ""
    locale = voice.get("Locale") or ""

    parts = short_name.split("-")
    name = parts[2] if len(parts) >= 3 else short_name
    name = re.sub(r"Neural$", "", name)

    multilingual = "Multilingual" in name
    name = name.replace("Multilingual", "").strip() or short_name

    gender = {"Male": "erkek", "Female": "kadın"}.get(voice.get("Gender", ""), "")

    extras = [locale] if locale else []
    if multilingual:
        extras.append("çok dilli")
    if gender:
        extras.append(gender)

    flag = "🇹🇷" if locale.lower().startswith("tr") else "🌍"
    return f"{flag} {name} · {' · '.join(extras)}" if extras else f"{flag} {name}"


def _is_relevant_voice(voice: dict[str, Any]) -> bool:
    short_name = (voice.get("ShortName") or "").lower()
    locale = (voice.get("Locale") or "").lower()
    return locale.startswith("tr") or "multilingual" in short_name


async def _fetch_voices_async() -> list[dict[str, Any]]:
    return await edge_tts.list_voices()


def get_voice_choices(
    force: bool = False,
    timeout: float = 15.0,
) -> tuple[list[tuple[str, str]], bool, str]:
    """(etiket, değer) çiftleri, başarı durumu ve hata mesajı döndürür.

    ``tr-TR`` ve ``Multilingual`` sesleri listelenir; ağ hatasında
    yerleşik yedek liste kullanılır.
    """
    global _voice_cache
    if _voice_cache is not None and not force:
        choices, ok, error = _voice_cache
        return list(choices), ok, error

    try:
        raw_voices = _run_async(_fetch_voices_async(), timeout=timeout)
        choices = [
            (_voice_label(voice), voice["ShortName"])
            for voice in raw_voices
            if voice.get("ShortName") and _is_relevant_voice(voice)
        ]
        # Türkçe sesler önce, sonra çok dilli sesler; kendi içinde alfabetik.
        choices.sort(key=lambda item: (0 if "🇹🇷" in item[0] else 1, item[0]))

        if not choices:
            raise RuntimeError("Uygun ses bulunamadı.")

        _voice_cache = (choices, True, "")
        return list(choices), True, ""
    except Exception as exc:  # noqa: BLE001
        error = f"Ses listesi alınamadı ({exc}). Yerleşik liste kullanılıyor."
        _voice_cache = (list(FALLBACK_VOICES), False, error)
        return list(FALLBACK_VOICES), False, error


# ---------------------------------------------------------------------------
# Hız / ton biçimlendirme
# ---------------------------------------------------------------------------

def format_rate(value: float) -> str:
    rate = max(-50, min(50, int(round(float(value)))))
    return f"{rate:+d}%"


def format_pitch(value: float) -> str:
    pitch = max(-50, min(50, int(round(float(value)))))
    return f"{pitch:+d}Hz"


# ---------------------------------------------------------------------------
# Metni parçalara ayırma
# ---------------------------------------------------------------------------

def _hard_split(text: str, limit: int) -> list[str]:
    """Sınırı aşan tek bir cümleyi kelime sınırlarından böler."""
    pieces: list[str] = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:  # çok uzun tek bir kelime
            cut = limit
        piece = text[:cut].strip()
        if piece:
            pieces.append(piece)
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def _split_sentences(paragraph: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", paragraph)
    sentences: list[str] = []
    for part in parts:
        part = part.strip()
        if part:
            sentences.extend(_hard_split(part, MAX_CHUNK_CHARS))
    return sentences


def split_into_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Metni cümle sonlarından ~3000 karakterlik parçalara böler."""
    text = normalize_text(text)
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        paragraph = " ".join(paragraph.split())
        if paragraph:
            units.extend(_split_sentences(paragraph))

    chunks: list[str] = []
    buffer = ""
    for unit in units:
        if not buffer:
            buffer = unit
        elif len(buffer) + 1 + len(unit) <= max_chars:
            buffer += " " + unit
        else:
            chunks.append(buffer)
            buffer = unit
    if buffer:
        chunks.append(buffer)

    return chunks


# ---------------------------------------------------------------------------
# Sentez
# ---------------------------------------------------------------------------

async def _synthesize_async(
    text: str,
    voice: str,
    rate: str,
    pitch: str,
    out_path: Path,
    timeout: float,
) -> None:
    communicator = edge_tts.Communicate(
        text,
        voice,
        rate=rate,
        pitch=pitch,
    )
    await asyncio.wait_for(communicator.save(str(out_path)), timeout=timeout)


def synthesize(
    text: str,
    voice: str,
    rate: float,
    pitch: float,
    out_path: str | Path,
    timeout: float = 240.0,
) -> Path:
    """Metni seçili ses/ayarlarla MP3 olarak kaydeder (tek deneme)."""
    if not text or not text.strip():
        raise ValueError("Seslendirilecek metin boş.")

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Yarım kalmış eski dosya varsa karışmasın.
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass

    _run_async(
        _synthesize_async(
            text,
            voice,
            format_rate(rate),
            format_pitch(pitch),
            target,
            timeout,
        ),
        timeout=timeout + 20,
    )

    if not target.exists() or target.stat().st_size < 512:
        raise RuntimeError("Ses motoru geçerli bir ses dosyası üretmedi.")

    return target


def cleanup_previews(directory: str | Path, max_age_seconds: int = 86400) -> None:
    """Bir günden eski önizleme dosyalarını siler."""
    folder = Path(directory)
    if not folder.exists():
        return
    now = time.time()
    for file_path in folder.glob("*.mp3"):
        try:
            if now - file_path.stat().st_mtime > max_age_seconds:
                file_path.unlink()
        except OSError:
            pass
