"""Kitap Seslendir - Gradio arayüzü.

Çalıştırmak için:  python app.py
"""

from __future__ import annotations

import html
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
import gradio as gr

from audio_tools import (
    concat_mp3,
    ffmpeg_version,
    find_ffmpeg,
    media_duration,
    to_m4b,
)
from extractors import ExtractionError, extract_book
from tts_engine import (
    DEFAULT_VOICE,
    PREVIEW_TEXT,
    cleanup_previews,
    get_voice_choices,
    split_into_chunks,
    synthesize,
)

# pythonw.exe ile (konsol penceresi olmadan) başlatıldığında çıktıları dosyaya yaz.
if os.name == "nt" and (sys.stdout is None or sys.stderr is None):
    _log_dir = Path(__file__).resolve().parent / "outputs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_stream = open(_log_dir / "app.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _log_stream
    if sys.stderr is None:
        sys.stderr = _log_stream

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
PREVIEW_DIR = OUTPUT_DIR / "_onizlemeler"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

APP_URL = "http://127.0.0.1:7860"
SHUTDOWN_DELAY = 2.0      # sekme kapandıktan sonra beklenecek süre
REFRESH_GRACE = 3.0       # yenileme sırasında kapanmayı engelleyen pencere

CHARS_PER_SECOND = 15.0  # Türkçe edge-tts için yaklaşık okuma hızı
MAX_RETRIES = 3

FFMPEG_PATH = find_ffmpeg()
FFMPEG_VERSION = ffmpeg_version(FFMPEG_PATH) if FFMPEG_PATH else None
FFMPEG_READY = FFMPEG_PATH is not None

VOICE_CHOICES, VOICES_OK, VOICES_ERROR = get_voice_choices()
VOICE_VALUES = {value for _, value in VOICE_CHOICES}
DEFAULT_SELECTED_VOICE = DEFAULT_VOICE if DEFAULT_VOICE in VOICE_VALUES else VOICE_CHOICES[0][1]

cleanup_previews(PREVIEW_DIR)


# ---------------------------------------------------------------------------
# Sekme kapanınca uygulamayı kapatma
# ---------------------------------------------------------------------------

_shutdown_timer: threading.Timer | None = None
_shutdown_lock = threading.Lock()
_last_page_load = 0.0


def _cancel_scheduled_shutdown() -> None:
    global _shutdown_timer
    with _shutdown_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
            _shutdown_timer = None


def _exit_application() -> None:
    os._exit(0)


def _schedule_shutdown(delay: float = SHUTDOWN_DELAY) -> None:
    global _shutdown_timer
    with _shutdown_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
        timer = threading.Timer(delay, _exit_application)
        timer.daemon = True
        _shutdown_timer = timer
        timer.start()


def on_page_load() -> None:
    """Sayfa açıldığında/yenilendiğinde planlanmış kapanmayı iptal eder."""
    global _last_page_load
    _last_page_load = time.monotonic()
    _cancel_scheduled_shutdown()


def on_page_unload() -> None:
    """Yerel sürümde sekme kapanınca uygulamayı kapatır (web sürümünde kapatmaz)."""
    if os.environ.get("RENDER") == "true" or os.environ.get("PORT"):
        return  # Web sunucusunda her kullanıcı için kapanma olmaz
    if time.monotonic() - _last_page_load < REFRESH_GRACE:
        return
    _schedule_shutdown()


# ---------------------------------------------------------------------------
# Web sunucusunu uyanık tutma (Render ücretsiz katmanı 15 dk boşta kalınca uyur)
# ---------------------------------------------------------------------------

KEEP_ALIVE_INTERVAL = float(os.environ.get("KEEP_ALIVE_INTERVAL", "300"))


def _keep_awake_loop() -> None:
    """Web modunda kendi adresine periyodik olarak bakarak sunucuyu uyanık tutar."""
    url = os.environ.get("RENDER_EXTERNAL_URL") or "https://kitap-seslendir.onrender.com"
    request = urllib.request.Request(url, headers={"User-Agent": "KitapSeslendir-KeepAlive/1.0"})
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read(1024)
            print("[keep-alive] sunucu uyanık", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[keep-alive] ping başarısız: {exc}", flush=True)


def _start_keep_awake() -> None:
    """Yalnızca web (Render) modunda arka plan ping iş parçacığını başlatır."""
    thread = threading.Thread(target=_keep_awake_loop, name="keep-alive", daemon=True)
    thread.start()


def _cleanup_stale_temp_dirs(max_age_seconds: int = 86400) -> None:
    """Ani kapanmalardan kalan eski geçici ses klasörlerini temizler."""
    root = Path(tempfile.gettempdir())
    now = time.time()
    for folder in root.glob("kitap_seslendir_*"):
        try:
            if folder.is_dir() and now - folder.stat().st_mtime > max_age_seconds:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def _app_already_running() -> bool:
    """Aynı uygulama zaten açıksa yeni süreç başlatmak yerine sekmeyi açar."""
    try:
        with urllib.request.urlopen(APP_URL + "/", timeout=1.5) as response:
            if response.status != 200:
                return False
            head = response.read(60000).decode("utf-8", "ignore")
            return "Kitap Seslendir" in head
    except Exception:  # noqa: BLE001
        return False


_cleanup_stale_temp_dirs()


# ---------------------------------------------------------------------------
# Küçük yardımcılar
# ---------------------------------------------------------------------------

def _tr_num(value: int | float) -> str:
    return f"{int(value):,}".replace(",", ".")


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} sa {minutes} dk"
    if minutes:
        return f"{minutes} dk {secs} sn"
    return f"{secs} sn"


def _fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _fmt_size(path: Path | None) -> str:
    if path is None or not path.exists():
        return "—"
    size = float(path.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def _slugify(value: str) -> str:
    table = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    value = value.translate(table)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value[:60] or "kitap"


def _alert(message: str, kind: str = "warning") -> str:
    icon = {"warning": "⚠️", "error": "❌", "ok": "✅"}.get(kind, "ℹ️")
    return (
        f'<div class="ks-alert {kind}"><span class="ks-alert-icon">{icon}</span>'
        f"<span>{html.escape(message)}</span></div>"
    )


def _stats_html(text: str, rate: float = 0) -> str:
    text = text or ""
    chars = len(text)
    words = len(re.findall(r"\S+", text))
    chunks = len(split_into_chunks(text)) if chars else 0
    speed_factor = max(0.5, 1 + float(rate) / 100.0)
    seconds = chars / (CHARS_PER_SECOND * speed_factor) if chars else 0

    return f"""
<div class="ks-stats">
  <div class="ks-stat"><span class="ks-stat-value">{_tr_num(chars)}</span><span class="ks-stat-label">karakter</span></div>
  <div class="ks-stat"><span class="ks-stat-value">{_tr_num(words)}</span><span class="ks-stat-label">kelime</span></div>
  <div class="ks-stat"><span class="ks-stat-value">{_fmt_duration(seconds)}</span><span class="ks-stat-label">tahmini süre</span></div>
  <div class="ks-stat"><span class="ks-stat-value">{_tr_num(chunks)}</span><span class="ks-stat-label">ses parçası</span></div>
</div>
"""


def _bar_html(pct: float, label: str, detail: str = "", state: str = "running") -> str:
    pct = max(0.0, min(100.0, float(pct)))
    detail_html = f'<div class="ks-detail">{html.escape(detail)}</div>' if detail else ""
    return f"""
<div class="ks-progress {state}">
  <div class="ks-progress-head"><span>{html.escape(label)}</span><span>{pct:.0f}%</span></div>
  <div class="ks-bar"><div class="ks-bar-fill" style="width:{pct:.1f}%"></div></div>
  {detail_html}
</div>
"""


def _success_html(
    mp3_path: Path | None,
    m4b_path: Path | None,
    title: str,
    duration: float | None,
    elapsed: float,
    warning: str = "",
) -> str:
    files = []
    if mp3_path:
        files.append(f"MP3 · {_fmt_size(mp3_path)}")
    if m4b_path:
        files.append(f"M4B · {_fmt_size(m4b_path)}")
    duration_text = _fmt_duration(duration) if duration else "—"
    warning_html = _alert(warning, "warning") if warning else ""
    return f"""
<div class="ks-progress done">
  <div class="ks-progress-head"><span>✅ Dönüşüm tamamlandı</span><span>100%</span></div>
  <div class="ks-bar"><div class="ks-bar-fill" style="width:100%"></div></div>
  <div class="ks-detail">
    <strong>{html.escape(title)}</strong> · {duration_text} · {' + '.join(files)} ·
    işlem süresi {_fmt_clock(elapsed)}
  </div>
  <div class="ks-detail">Dosyalar aşağıdaki indirme düğmelerinden alınabilir.</div>
</div>
{warning_html}
"""


HINT_HTML = '<div class="ks-hint">Dönüşüm başladığında ilerleme burada görünecek.</div>'


# ---------------------------------------------------------------------------
# Metin çıkarma işleyicileri
# ---------------------------------------------------------------------------

def load_book(
    file_path: str | None,
    top_margin: float,
    bottom_margin: float,
    join_hyphens: bool,
    drop_page_numbers: bool,
    rate: float,
) -> tuple[str, str, str, str | None]:
    if not file_path:
        return "", _stats_html("", rate), "", None

    path = Path(file_path)
    try:
        text, warnings = extract_book(
            path,
            top_margin=top_margin,
            bottom_margin=bottom_margin,
            join_hyphens=bool(join_hyphens),
            drop_page_numbers=bool(drop_page_numbers),
        )
    except ExtractionError as exc:
        return "", _stats_html("", rate), _alert(str(exc), "error"), str(path)
    except Exception as exc:  # noqa: BLE001
        return (
            "",
            _stats_html("", rate),
            _alert(f"Metin çıkarılırken beklenmeyen bir hata oluştu: {exc}", "error"),
            str(path),
        )

    warnings_html = "".join(_alert(warning, "warning") for warning in warnings)
    return text, _stats_html(text, rate), warnings_html, str(path)


def handle_upload(file_path, top_margin, bottom_margin, join_hyphens, drop_page_numbers, rate):
    text, stats, warnings, state = load_book(
        file_path, top_margin, bottom_margin, join_hyphens, drop_page_numbers, rate
    )
    return (
        text,
        stats,
        warnings,
        state,
        HINT_HTML,
        gr.update(visible=False),
        gr.update(visible=False),
    )


def handle_reextract(file_path, top_margin, bottom_margin, join_hyphens, drop_page_numbers, rate):
    return load_book(
        file_path, top_margin, bottom_margin, join_hyphens, drop_page_numbers, rate
    )


def refresh_stats(text: str, rate: float) -> str:
    return _stats_html(text, rate)


# ---------------------------------------------------------------------------
# Ses işleyicileri
# ---------------------------------------------------------------------------

def refresh_voices(current: str | None):
    choices, ok, error = get_voice_choices(force=True)
    values = [value for _, value in choices]
    selected = current if current in values else (DEFAULT_VOICE if DEFAULT_VOICE in values else values[0])
    if ok:
        status = f"✅ {len(choices)} ses hazır. Türkçe ve çok dilli sesler listelendi."
    else:
        status = f"⚠️ {error}"
    return gr.update(choices=choices, value=selected), status


def make_preview(voice: str | None, rate: float, pitch: float) -> str:
    if not voice:
        raise gr.Error("Lütfen önce bir ses seçin.")
    cleanup_previews(PREVIEW_DIR)
    out_path = PREVIEW_DIR / f"onizleme_{int(time.time() * 1000)}.mp3"
    try:
        synthesize(PREVIEW_TEXT, voice, rate, pitch, out_path, timeout=90)
    except Exception as exc:  # noqa: BLE001
        raise gr.Error(f"Önizleme oluşturulamadı: {exc}") from exc
    return str(out_path)


# ---------------------------------------------------------------------------
# Dönüşüm
# ---------------------------------------------------------------------------

def convert_book(
    text: str,
    voice: str,
    rate: float,
    pitch: float,
    formats: list[str] | None,
    source_path: str | None,
):
    formats = formats or []
    hide_buttons = (gr.update(visible=False), gr.update(visible=False))

    if not (text or "").strip():
        yield _alert("Dönüştürmek için önce bir dosya yükleyip metni kontrol edin.", "error"), *hide_buttons
        return

    if not voice:
        yield _alert("Lütfen bir ses seçin.", "error"), *hide_buttons
        return

    if not formats:
        yield _alert("En az bir çıktı formatı seçin (MP3 veya M4B).", "error"), *hide_buttons
        return

    if not FFMPEG_READY:
        yield (
            _alert(
                "FFmpeg bulunamadı. Ses birleştirme ve M4B dönüşümü için FFmpeg "
                "kurup PATH'e eklemelisiniz. Kurulumdan sonra uygulamayı yeniden başlatın.",
                "error",
            ),
            *hide_buttons,
        )
        return

    chunks = split_into_chunks(text)
    if not chunks:
        yield _alert("Metin seslendirme için uygun parçalara ayrılamadı.", "error"), *hide_buttons
        return

    title = Path(source_path).stem if source_path else "kitap"
    title = title.strip() or "kitap"
    slug = _slugify(title)
    total_chars = sum(len(chunk) for chunk in chunks)
    total = len(chunks)

    out_dir = OUTPUT_DIR / f"{slug}_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    mp3_path = out_dir / f"{slug}.mp3"
    m4b_path = out_dir / f"{slug}.m4b"
    temp_dir = Path(tempfile.mkdtemp(prefix="kitap_seslendir_"))
    parts: list[Path] = []
    started_at = time.time()
    produced: list[Path] = []

    try:
        yield (
            _bar_html(
                0,
                "Hazırlanıyor",
                f"{total} parça · {_tr_num(total_chars)} karakter · ses: {voice}",
            ),
            *hide_buttons,
        )

        processed_chars = 0
        for index, chunk in enumerate(chunks, start=1):
            part_path = temp_dir / f"parca_{index:04d}.mp3"
            pct = processed_chars / total_chars * 100

            yield (
                _bar_html(
                    pct,
                    f"Parça {index}/{total} seslendiriliyor",
                    f"{_tr_num(processed_chars)} / {_tr_num(total_chars)} karakter işlendi",
                ),
                *hide_buttons,
            )

            last_error: Exception | None = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    synthesize(chunk, voice, rate, pitch, part_path)
                    last_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < MAX_RETRIES:
                        yield (
                            _bar_html(
                                pct,
                                f"Parça {index}/{total} · yeniden deneniyor ({attempt}/{MAX_RETRIES})",
                                str(exc)[:220],
                                state="retry",
                            ),
                            *hide_buttons,
                        )
                        time.sleep(1.5 * attempt)

            if last_error is not None:
                raise RuntimeError(
                    f"{index}/{total}. parça {MAX_RETRIES} denemede de oluşturulamadı: {last_error}"
                )

            parts.append(part_path)
            processed_chars += len(chunk)

            elapsed = time.time() - started_at
            pct = processed_chars / total_chars * 100
            eta = (elapsed / processed_chars) * (total_chars - processed_chars) if processed_chars else 0
            yield (
                _bar_html(
                    pct,
                    f"Parça {index}/{total} tamamlandı",
                    f"Geçen süre {_fmt_clock(elapsed)} · tahmini kalan {_fmt_clock(eta)}",
                ),
                *hide_buttons,
            )

        # --- Birleştirme ---
        yield (
            _bar_html(98, "Ses parçaları birleştiriliyor", f"{total} parça FFmpeg ile birleştiriliyor"),
            *hide_buttons,
        )

        if "MP3" in formats:
            concat_mp3(parts, mp3_path)
            produced.append(mp3_path)
            source_mp3 = mp3_path
        else:
            source_mp3 = temp_dir / "birlestirilmis.mp3"
            concat_mp3(parts, source_mp3)

        # --- M4B ---
        m4b_error = ""
        if "M4B" in formats:
            yield (
                _bar_html(99, "M4B dosyası oluşturuluyor", "AAC kodlaması ve sesli kitap kabı hazırlanıyor"),
                *hide_buttons,
            )
            try:
                to_m4b(source_mp3, m4b_path, title=title)
                produced.append(m4b_path)
            except Exception as exc:  # noqa: BLE001
                m4b_error = f"M4B oluşturulamadı: {exc}"
                if "MP3" not in formats:
                    raise

        elapsed = time.time() - started_at
        duration = media_duration(produced[0]) if produced else None

        mp3_update = (
            gr.update(
                value=str(mp3_path),
                label=f"MP3 indir · {_fmt_size(mp3_path)}",
                visible=True,
            )
            if mp3_path in produced
            else gr.update(visible=False)
        )
        m4b_update = (
            gr.update(
                value=str(m4b_path),
                label=f"M4B indir · {_fmt_size(m4b_path)}",
                visible=True,
            )
            if m4b_path in produced
            else gr.update(visible=False)
        )

        yield (
            _success_html(
                mp3_path if mp3_path in produced else None,
                m4b_path if m4b_path in produced else None,
                title,
                duration,
                elapsed,
                warning=m4b_error,
            ),
            mp3_update,
            m4b_update,
        )

    except Exception as exc:  # noqa: BLE001
        yield _alert(f"Dönüşüm başarısız oldu: {exc}", "error"), *hide_buttons
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if not produced:
            shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Arayüz
# ---------------------------------------------------------------------------

CSS = """
.gradio-container {
    max-width: 1280px !important; margin: 0 auto !important;
    background: #fffdf8 !important;
    --block-background-fill: #fffaf0 !important;
    --body-text-color: #4a3222 !important;
}

.ks-hero {
    display: flex; align-items: center; gap: 18px;
    padding: 24px 28px; border-radius: 20px; margin-bottom: 14px;
    background: linear-gradient(135deg, #3e2723 0%, #795548 55%, #a1887f 100%);
    color: #fff; box-shadow: 0 12px 30px rgba(121, 85, 72, .25);
}
.ks-hero-icon { font-size: 2.6rem; line-height: 1; }
.ks-hero h1 { margin: 0; font-size: 1.85rem; font-weight: 800; letter-spacing: -.02em; color: #fffaf0 !important; }
.ks-hero p { margin: 5px 0 0; opacity: .92; font-size: .95rem; color: #f5e6d3 !important; }

.ks-alert {
    display: flex; gap: 10px; align-items: flex-start;
    padding: 12px 16px; border-radius: 12px; margin: 8px 0;
    font-size: .93rem; line-height: 1.45;
}
.ks-alert.ok { background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; }
.ks-alert.warning { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }
.ks-alert.error { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }
.ks-alert-icon { flex: 0 0 auto; }

.ks-stats { display: flex; gap: 10px; flex-wrap: wrap; margin: 6px 0 2px; }
.ks-stat {
    flex: 1 1 130px; display: flex; flex-direction: column; gap: 2px;
    padding: 10px 14px; border-radius: 12px;
    background: var(--background-fill-secondary);
    border: 1px solid var(--border-color-primary);
}
.ks-stat-value { font-size: 1.15rem; font-weight: 700; }
.ks-stat-label {
    font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
    color: var(--body-text-color-subdued);
}

.ks-progress {
    padding: 14px 16px; border-radius: 14px; margin-top: 6px;
    background: var(--background-fill-secondary);
    border: 1px solid var(--border-color-primary);
}
.ks-progress.retry { border-color: #fbbf24; }
.ks-progress.done { border-color: #34d399; }
.ks-progress-head {
    display: flex; justify-content: space-between; gap: 12px;
    font-weight: 650; font-size: .95rem; margin-bottom: 8px;
}
.ks-bar { height: 10px; border-radius: 999px; overflow: hidden; background: var(--border-color-primary); }
.ks-bar-fill {
    height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, #8b5e3c, #c19a6b);
    transition: width .35s ease;
}
.ks-progress.retry .ks-bar-fill { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
.ks-progress.done .ks-bar-fill { background: linear-gradient(90deg, #059669, #34d399); }
.ks-detail { margin-top: 7px; font-size: .82rem; color: var(--body-text-color-subdued); line-height: 1.45; }
.ks-hint { font-size: .85rem; color: var(--body-text-color-subdued); padding: 6px 2px; }

#ks-file { min-height: 150px; }
#ks-convert { min-height: 48px; font-size: 1.02rem; }
"""

# Kitap dünyasına uygun sıcak palet: krem zemin, kahverengi vurgular.
KAHVERENGI = gr.themes.Color(
    c50="#fdf8f3", c100="#f6ece1", c200="#ead8c4", c300="#d7b99a",
    c400="#bd956f", c500="#a5774f", c600="#8b5e3c", c700="#70492f",
    c800="#593b27", c900="#4a3222", c950="#2a1a11", name="kahverengi",
)
KREM = gr.themes.Color(
    c50="#fffdf8", c100="#faf4e8", c200="#efe2cc", c300="#ddc9a9",
    c400="#c4a882", c500="#a98a66", c600="#8d7155", c700="#715a45",
    c800="#5d4a3a", c900="#4e3f32", c950="#2a211b", name="krem",
)
TERRAKOTA = gr.themes.Color(
    c50="#fdf6f0", c100="#f9e8dd", c200="#f0cdb9", c300="#e2a98d",
    c400="#cd8363", c500="#b96b4b", c600="#9d5438", c700="#7e412d",
    c800="#673828", c900="#563124", c950="#2f1812", name="terrakota",
)

with gr.Blocks(title="Kitap Seslendir") as demo:
    gr.HTML(
        """
        <div class="ks-hero">
          <div class="ks-hero-icon">🎧</div>
          <div>
            <h1>Kitap Seslendir</h1>
            <p>EPUB, PDF veya TXT dosyanızı saniyeler içinde sesli kitaba dönüştürün.
            Metni düzenleyin, sesi seçin, MP3 veya M4B olarak indirin.</p>
          </div>
        </div>
        """
    )

    if FFMPEG_READY:
        gr.HTML(_alert(f"FFmpeg hazır (sürüm {FFMPEG_VERSION or 'bilinmiyor'}).", "ok"))
    else:
        gr.HTML(
            _alert(
                "FFmpeg bulunamadı! Ses birleştirme ve M4B dönüşümü çalışmaz. "
                "Windows'ta 'winget install Gyan.FFmpeg' veya 'choco install ffmpeg' "
                "komutuyla kurup uygulamayı yeniden başlatın.",
                "error",
            )
        )

    with gr.Row(equal_height=False):
        # ---------------- Sol sütun: dosya + metin ----------------
        with gr.Column(scale=7, min_width=420):
            file_input = gr.File(
                label="Kitap dosyası (EPUB · PDF · TXT)",
                file_types=[".epub", ".pdf", ".txt"],
                type="filepath",
                elem_id="ks-file",
            )

            with gr.Row():
                drop_page_numbers = gr.Checkbox(
                    value=True,
                    label="Sayfa numaralarını otomatik temizle",
                    info="EPUB, PDF ve TXT'te sayfa numaralarını metinden ayıklar",
                )

            with gr.Accordion("PDF ayarları (yalnızca PDF için)", open=False):
                with gr.Row():
                    top_margin = gr.Slider(
                        0, 30, value=8, step=0.5,
                        label="Üst marj kırpma (%)",
                        info="Üst başlıkları atlamak için",
                    )
                    bottom_margin = gr.Slider(
                        0, 30, value=8, step=0.5,
                        label="Alt marj kırpma (%)",
                        info="Alt bilgi ve sayfa numaralarını atlamak için",
                    )
                join_hyphens = gr.Checkbox(
                    value=True,
                    label="Satır sonu tirelerini birleştir",
                    info="Bölünen kelimeleri onarır",
                )
                reextract_btn = gr.Button("PDF ayarlarıyla yeniden çıkar", size="sm")

            text_box = gr.Textbox(
                label="Çıkarılan metin (düzenleyebilirsiniz)",
                placeholder="Bir EPUB, PDF veya TXT dosyası yükleyin; metin burada görünecek…",
                lines=16,
                max_lines=30,
                buttons=["copy"],
                elem_id="ks-text",
            )
            stats_html = gr.HTML(_stats_html("", 0))
            warning_html = gr.HTML("")

        # ---------------- Sağ sütun: ses ayarları ----------------
        with gr.Column(scale=5, min_width=340):
            with gr.Row():
                voice_dropdown = gr.Dropdown(
                    choices=VOICE_CHOICES,
                    value=DEFAULT_SELECTED_VOICE,
                    label="Ses",
                    filterable=True,
                    scale=5,
                )
                refresh_voices_btn = gr.Button("⟳", size="sm", scale=1, min_width=48)

            voice_status = gr.Markdown(
                f"✅ {len(VOICE_CHOICES)} ses hazır."
                if VOICES_OK
                else f"⚠️ {VOICES_ERROR}"
            )

            rate_slider = gr.Slider(
                -50, 50, value=0, step=1,
                label="Okuma hızı (%)",
                info="-50 yavaş · 0 normal · +50 hızlı",
            )
            pitch_slider = gr.Slider(
                -50, 50, value=0, step=1,
                label="Ses tonu (Hz)",
                info="Negatif daha kalın, pozitif daha ince",
            )

            with gr.Row():
                preview_btn = gr.Button("🔊 Sesi Önizle", variant="secondary")
            preview_audio = gr.Audio(
                label="Önizleme",
                type="filepath",
                format="mp3",
                autoplay=True,
                interactive=False,
            )

            formats = gr.CheckboxGroup(
                choices=["MP3", "M4B"],
                value=["MP3"],
                label="Çıktı formatı",
                info="M4B, sesli kitap oynatıcıları için uygun kaptır.",
            )

            convert_btn = gr.Button(
                "🎙️ Seslendir",
                variant="primary",
                elem_id="ks-convert",
                interactive=FFMPEG_READY,
            )

            progress_html = gr.HTML(HINT_HTML)

            with gr.Row():
                download_mp3 = gr.DownloadButton(
                    "MP3 indir", variant="primary", visible=False
                )
                download_m4b = gr.DownloadButton(
                    "M4B indir", variant="secondary", visible=False
                )

    last_file = gr.State(None)

    # --- Olaylar ---
    file_input.change(
        handle_upload,
        inputs=[file_input, top_margin, bottom_margin, join_hyphens, drop_page_numbers, rate_slider],
        outputs=[text_box, stats_html, warning_html, last_file, progress_html, download_mp3, download_m4b],
        show_progress="minimal",
    )

    reextract_btn.click(
        handle_reextract,
        inputs=[last_file, top_margin, bottom_margin, join_hyphens, drop_page_numbers, rate_slider],
        outputs=[text_box, stats_html, warning_html, last_file],
        show_progress="minimal",
    )

    # Sayfa numarası temizliği açılıp kapatıldığında metni yeniden çıkar.
    drop_page_numbers.change(
        handle_reextract,
        inputs=[last_file, top_margin, bottom_margin, join_hyphens, drop_page_numbers, rate_slider],
        outputs=[text_box, stats_html, warning_html, last_file],
        show_progress="minimal",
    )

    text_box.blur(refresh_stats, inputs=[text_box, rate_slider], outputs=stats_html)
    text_box.submit(refresh_stats, inputs=[text_box, rate_slider], outputs=stats_html)
    rate_slider.release(refresh_stats, inputs=[text_box, rate_slider], outputs=stats_html)

    refresh_voices_btn.click(
        refresh_voices,
        inputs=[voice_dropdown],
        outputs=[voice_dropdown, voice_status],
        show_progress="minimal",
    )

    preview_btn.click(
        make_preview,
        inputs=[voice_dropdown, rate_slider, pitch_slider],
        outputs=preview_audio,
        show_progress="minimal",
    )

    convert_btn.click(
        convert_book,
        inputs=[text_box, voice_dropdown, rate_slider, pitch_slider, formats, last_file],
        outputs=[progress_html, download_mp3, download_m4b],
        concurrency_limit=1,
        show_progress="hidden",
    )

    # Sekme açılınca/yenilenince kapanmayı iptal et; sekme kapanınca uygulamayı kapat.
    demo.load(on_page_load)
    demo.unload(on_page_unload)


if __name__ == "__main__":
    import os

    is_web = os.environ.get("RENDER") == "true" or os.environ.get("PORT")

    if is_web:
        _start_keep_awake()

    if not is_web and _app_already_running():
        webbrowser.open(APP_URL)
        raise SystemExit(0)

    demo.queue(default_concurrency_limit=8).launch(
        server_name="0.0.0.0" if is_web else "127.0.0.1",
        server_port=int(os.environ.get("PORT", "7860")),
        inbrowser=not is_web,
        theme=gr.themes.Soft(
            primary_hue=KAHVERENGI,
            secondary_hue=TERRAKOTA,
            neutral_hue=KREM,
        ),
        css=CSS,
        allowed_paths=[str(OUTPUT_DIR)],
        footer_links=[],
    )
