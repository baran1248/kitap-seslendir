"""Kitap Seslendir - EPUB, PDF ve TXT dosyalarından metin çıkarma.

Bu modül yalnızca metin çıkarır; seslendirme tarafı ``tts_engine`` içindedir.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import epub

try:  # PyMuPDF 1.24+ önerilen içe aktarma yolu
    import pymupdf as fitz
except ImportError:  # eski sürümler
    import fitz  # type: ignore[no-redef]

SUPPORTED_EXTENSIONS = (".epub", ".pdf", ".txt")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExtractionError(Exception):
    """Kullanıcıya doğrudan gösterilebilecek metin çıkarma hatası."""


# ---------------------------------------------------------------------------
# Genel metin temizliği
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Satır sonlarını, boşlukları ve görünmez karakterleri düzenler."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)  # ﬁ -> fi, ligatürler vb.
    text = text.replace("\u00ad", "")           # yumuşak tire
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")          # bölünmez boşluk
    text = _CONTROL_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Sayfa numarası temizliği
# ---------------------------------------------------------------------------

_LINE_END_NUMBER_RE = re.compile(r"(?<=\s)(\d{1,4})\s*$")
_PAGE_MARKER_RE = re.compile(
    r"^(?:sayfa|page|s\.|p\.)\s*(\d{1,4})$",
    re.IGNORECASE,
)
_PAGE_RANGE_RE = re.compile(r"^(\d{1,4})\s*/\s*\d{1,4}$")


def _classify_page_line(line: str) -> tuple[str, int] | None:
    """Tek başına duran sayfa numarası satırlarını sınıflandırır.

    Dönen değer ``("strong", n)`` ise satır kesin bir sayfa işaretidir
    (``Sayfa 12``, ``12 / 340``, ``- 12 -`` gibi). ``("plain", n)`` ise
    yalnızca sayıdan oluşan bir satırdır; bunlar artan dizi analizine girer.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 40:
        return None

    core = stripped.strip("-–—•·|").strip()
    if not core:
        return None

    marker = _PAGE_MARKER_RE.fullmatch(core)
    if marker:
        return "strong", int(marker.group(1))

    page_range = _PAGE_RANGE_RE.fullmatch(core)
    if page_range:
        return "strong", int(page_range.group(1))

    # "- 12 -", "— 12 —" gibi çevrelenmiş sayılar
    if re.fullmatch(r"[-–—]\s*\d{1,4}\s*[-–—]", stripped):
        return "strong", int(re.search(r"\d{1,4}", stripped).group(0))

    if re.fullmatch(r"\d{1,4}", core):
        return "plain", int(core)

    return None


def _select_page_sequence(candidates: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Satır sonlarındaki adaylardan en tutarlı artan sayfa dizisini seçer.

    Adaylar ``(satır_no, sayı, başlangıç, bitiş)`` biçimindedir. Sayfa
    numaraları kitap boyunca artar ve birbirine yakındır; yıl, miktar gibi
    tek tük sayılar bu dizinin dışında kalır.
    """
    if len(candidates) < 4:
        return []

    candidates = sorted(candidates, key=lambda item: (item[0], item[2]))
    count = len(candidates)
    lengths = [1] * count
    previous = [-1] * count

    for i in range(count):
        current = candidates[i][1]
        for j in range(i):
            step = current - candidates[j][1]
            if 0 < step <= 30 and lengths[j] + 1 > lengths[i]:
                lengths[i] = lengths[j] + 1
                previous[i] = j

    best = max(range(count), key=lambda index: lengths[index])
    chain: list[tuple[int, int, int, int]] = []
    while best != -1:
        chain.append(candidates[best])
        best = previous[best]
    chain.reverse()

    if len(chain) < 4:
        return []

    numbers = [item[1] for item in chain]
    span = max(numbers) - min(numbers) + 1
    coverage = len(chain) / span if span else 0.0
    if len(chain) < 6 or coverage < 0.35:
        # Sayfa numaraları kitap boyunca yoğun ve düzenli bir dizi oluşturur;
        # tek tük artan sayılar (yıl, miktar, dipnot) bu eşiği geçemez.
        return []

    return chain


def remove_page_numbers(text: str) -> tuple[str, int]:
    """Metindeki sayfa numaralarını otomatik olarak ayıklar.

    İki katmanlı çalışır:
    - ``Sayfa 12``, ``12 / 340``, ``- 12 -`` gibi kesin işaretleri siler.
    - Tek başına duran sayıları ve satır sonlarına eklenmiş sayıları,
      kitap boyunca artan sayfa dizisiyle eşleştirerek siler. Böylece
      cümle içindeki yıl/miktar gibi sayılar korunur.

    Dönen değer: ``(temizlenmiş_metin, silinen_sayfa_numarası_sayısı)``.
    """
    if not text:
        return text, 0

    lines = text.split("\n")
    candidates: list[tuple[int, int, int, int]] = []
    strong_lines: list[tuple[int, int]] = []

    for index, line in enumerate(lines):
        classification = _classify_page_line(line)
        if classification is not None:
            kind, number = classification
            if kind == "strong":
                strong_lines.append((index, number))
            elif 1 <= number <= 1200:
                match = re.search(r"\d{1,4}", line)
                if match:
                    candidates.append((index, number, match.start(), match.end()))
            continue

        match = _LINE_END_NUMBER_RE.search(line)
        if match and len(line.strip()) >= 30:
            number = int(match.group(1))
            if 1 <= number <= 1200:
                candidates.append((index, number, match.start(1), match.end(1)))

    chain = _select_page_sequence(candidates)

    removals: dict[int, list[tuple[int, int]]] = {}
    for index, _number in strong_lines:
        removals.setdefault(index, []).append((0, len(lines[index])))
    for index, _number, start, end in chain:
        removals.setdefault(index, []).append((start, end))

    removed = 0
    for index, spans in removals.items():
        line = lines[index]
        for start, end in sorted(spans, reverse=True):
            line = line[:start] + line[end:]
            removed += 1
        lines[index] = line.rstrip()

    cleaned = normalize_text("\n".join(lines))
    return cleaned, removed


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

_ROMAN_PAGES = set()


def _build_roman_pages() -> None:
    values = (
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    )
    for number in range(1, 51):
        remaining, result = number, ""
        for value, symbol in values:
            while remaining >= value:
                result += symbol
                remaining -= value
        _ROMAN_PAGES.add(result)


_build_roman_pages()


def _looks_like_page_number(text: str) -> bool:
    """Yalnızca sayfa numarasından oluşan blokları yakalar."""
    cleaned = " ".join(text.split()).strip(" -–—•·|")
    if not cleaned or len(cleaned) > 24:
        return False

    lowered = cleaned.lower()
    if lowered.startswith("sayfa"):
        rest = lowered[5:].strip(" :.-–—")
        if not rest:
            return True
        return bool(re.fullmatch(r"\d{1,4}(?:\s*/\s*\d{1,4})?", rest))

    if re.fullmatch(r"\d{1,4}", cleaned):
        return True
    if re.fullmatch(r"\d{1,4}\s*/\s*\d{1,4}", cleaned):
        return True
    if cleaned.upper() in _ROMAN_PAGES:
        return True
    return False


def _join_hyphenated_lines(lines: list[str], join_hyphens: bool) -> str:
    """Bir blok içindeki satırları birleştirir; satır sonu tirelerini onarır."""
    buffer = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not buffer:
            buffer = line
            continue
        if (
            join_hyphens
            and re.search(r"[^\W\d_]-$", buffer)
            and line[:1].islower()
        ):
            buffer = buffer[:-1] + line
        else:
            buffer += " " + line
    return buffer.strip()


def extract_pdf(
    path: str | Path,
    top_margin: float = 8.0,
    bottom_margin: float = 8.0,
    join_hyphens: bool = True,
    drop_page_numbers: bool = True,
) -> tuple[str, list[str]]:
    """PDF'ten metin çıkarır.

    ``top_margin`` / ``bottom_margin`` sayfa yüksekliğinin yüzdesi olarak
    kırpılır; böylece üst/alt başlıklar (running header/footer) atlanır.
    """
    pdf_path = Path(path)
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - kullanıcıya anlamlı mesaj
        raise ExtractionError(f"PDF dosyası açılamadı: {exc}") from exc

    if doc.needs_pass:
        doc.close()
        raise ExtractionError(
            "Bu PDF parola korumalı. Lütfen parolayı kaldırıp yeniden deneyin."
        )

    page_count = doc.page_count
    if page_count == 0:
        doc.close()
        raise ExtractionError("PDF boş görünüyor (0 sayfa).")

    top_pct = max(0.0, min(float(top_margin), 40.0))
    bottom_pct = max(0.0, min(float(bottom_margin), 40.0))

    pages: list[str] = []
    empty_pages = 0

    try:
        for page in doc:
            rect = page.rect
            clip = fitz.Rect(
                rect.x0,
                rect.y0 + rect.height * top_pct / 100.0,
                rect.x1,
                rect.y1 - rect.height * bottom_pct / 100.0,
            )
            if clip.height < 10 or clip.width < 10:
                empty_pages += 1
                continue

            blocks = page.get_text("blocks", clip=clip, sort=True)
            page_blocks: list[str] = []

            for block in blocks:
                # (x0, y0, x1, y1, text, block_no, block_type)
                if len(block) < 7 or block[6] != 0:
                    continue
                raw = (block[4] or "").strip()
                if not raw:
                    continue

                center_y = (block[1] + block[3]) / 2.0
                near_edge = (
                    center_y < rect.y0 + rect.height * 0.10
                    or center_y > rect.y1 - rect.height * 0.10
                )
                if drop_page_numbers and near_edge and _looks_like_page_number(raw):
                    continue

                lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
                lines = [line for line in lines if line]
                if not lines:
                    continue

                joined = _join_hyphenated_lines(lines, join_hyphens)
                if joined:
                    page_blocks.append(joined)

            page_text = "\n\n".join(page_blocks).strip()
            if page_text:
                pages.append(page_text)
            else:
                empty_pages += 1
    finally:
        doc.close()

    text = normalize_text("\n\n".join(pages))
    warnings: list[str] = []

    if len(text) < 20:
        raise ExtractionError(
            "Bu PDF'te seçilebilir metin bulunamadı. Dosya taranmış (görüntü) "
            "olabilir; seslendirmek için OCR uygulanmış bir sürüm gerekir."
        )

    if page_count >= 2 and empty_pages / page_count >= 0.5:
        warnings.append(
            f"PDF'in {empty_pages}/{page_count} sayfasında metin katmanı yok. "
            "Dosya büyük ölçüde taranmış olabilir; eksik sayfalar seslendirilmez."
        )
    elif page_count >= 3 and len(text) / page_count < 80:
        warnings.append(
            "Sayfa başına çok az metin bulundu. PDF taranmış olabilir; "
            "metni yüklemeden sonra kontrol etmeniz önerilir."
        )

    return text, warnings


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------

_BLOCK_TAGS = [
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "ul", "ol", "blockquote", "pre", "section", "article",
    "table", "tr", "td", "th", "dd", "dt", "figcaption", "aside",
]


def _html_to_text(html: bytes | str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "head", "nav"]):
        tag.decompose()

    root = soup.body or soup
    for br in root.find_all("br"):
        br.replace_with("\n")

    # Blok etiketlerinin çevresine paragraf aralığı ekle; böylece iç içe
    # etiketler metni çoğaltmadan doğru sırada okunur.
    for tag in root.find_all(_BLOCK_TAGS):
        tag.insert_before("\n\n")
        tag.insert_after("\n\n")

    text = root.get_text(" ")
    # Internet Archive OCR çıktılarındaki güven oranı satırını kaldır.
    text = re.sub(
        r"The text on this page is estimated to be only [\d.]+% accurate\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return normalize_text(text)


def _is_html_document(item) -> bool:
    """EPUB içindeki HTML/XHTML belgelerini media-type veya uzantıdan tanır.

    Bazı EPUB'lar (özellikle Internet Archive tarafından üretilenler)
    ``application/xhtml+xml`` yerine ``text/html`` media-type'ı kullanır;
    ebooklib bu öğeleri ``ITEM_DOCUMENT`` olarak sınıflandırmaz.
    """
    media_type = (getattr(item, "media_type", "") or "").lower()
    name = (item.get_name() or "").lower()
    return (
        media_type in {"application/xhtml+xml", "text/html"}
        or name.endswith((".xhtml", ".html", ".htm", ".xht"))
    )


def extract_epub(
    path: str | Path,
    drop_page_numbers: bool = True,
) -> tuple[str, list[str]]:
    epub_path = Path(path)
    try:
        book = epub.read_epub(str(epub_path))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"EPUB dosyası okunamadı: {exc}") from exc

    items = {
        item.get_id(): item
        for item in book.get_items()
        if _is_html_document(item)
    }

    ordered_ids: list[str] = []
    for entry in book.spine:
        idref = entry[0] if isinstance(entry, (tuple, list)) else entry
        if idref in items and idref not in ordered_ids:
            ordered_ids.append(idref)
    ordered_ids.extend(item_id for item_id in items if item_id not in ordered_ids)

    parts: list[str] = []
    ocr_detected = False
    for item_id in ordered_ids:
        item = items[item_id]
        properties = getattr(item, "properties", None) or []
        base_name = (item.get_name() or "").lower().rsplit("/", 1)[-1]
        if "nav" in properties or base_name in {
            "nav.xhtml", "nav.html", "toc.xhtml", "toc.html", "contents.xhtml",
        }:
            continue
        try:
            content = item.get_content()
        except Exception:  # noqa: BLE001 - bozuk bir bölüm tüm kitabı durdurmasın
            continue
        chapter = _html_to_text(content)
        if not chapter:
            continue

        lowered = chapter.lower()
        if "internet archive" in lowered and (
            "optical character recognition" in lowered
            or "scanned and converted to epub" in lowered
        ):
            # Internet Archive'ın otomatik OCR uyarı sayfası kitabın parçası değil.
            ocr_detected = True
            continue

        parts.append(chapter)

    text = normalize_text("\n\n".join(parts))
    if len(text) < 20:
        raise ExtractionError(
            "EPUB içinde okunabilir metin bulunamadı. Dosya bozuk veya "
            "yalnızca görsellerden oluşuyor olabilir."
        )

    warnings: list[str] = []

    if drop_page_numbers:
        text, removed = remove_page_numbers(text)
        if removed:
            warnings.append(
                f"Metin içinde {removed} adet sayfa numarası otomatik olarak silindi."
            )

    if ocr_detected:
        warnings.append(
            "Bu EPUB, taranmış sayfaların otomatik OCR'ı ile üretilmiş. "
            "Metinde tanıma hataları olabilir; seslendirmeden önce gözden "
            "geçirmeniz önerilir."
        )
    if len(text) < 500:
        warnings.append(
            "EPUB'dan çok az metin çıkarıldı. İçerik görsel tabanlı olabilir."
        )
    return text, warnings


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------

def extract_txt(
    path: str | Path,
    drop_page_numbers: bool = True,
) -> tuple[str, list[str]]:
    data = Path(path).read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1254", "iso-8859-9", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if text is None:  # pratikte latin-1 her zaman çözer
        raise ExtractionError("TXT dosyasının kodlaması çözülemedi.")

    cleaned = normalize_text(text)
    if not cleaned:
        raise ExtractionError("TXT dosyası boş.")
    if drop_page_numbers:
        cleaned, removed = remove_page_numbers(cleaned)
        if removed:
            return cleaned, [
                f"Metin içinde {removed} adet sayfa numarası otomatik olarak silindi."
            ]
    return cleaned, []


# ---------------------------------------------------------------------------
# Ortak giriş noktası
# ---------------------------------------------------------------------------

def extract_book(
    path: str | Path,
    *,
    top_margin: float = 8.0,
    bottom_margin: float = 8.0,
    join_hyphens: bool = True,
    drop_page_numbers: bool = True,
) -> tuple[str, list[str]]:
    """Uzantıya göre doğru çıkarıcıyı seçer ve (metin, uyarılar) döndürür."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        return extract_pdf(
            file_path,
            top_margin=top_margin,
            bottom_margin=bottom_margin,
            join_hyphens=join_hyphens,
            drop_page_numbers=drop_page_numbers,
        )

    if suffix == ".epub":
        text, warnings = extract_epub(file_path, drop_page_numbers=drop_page_numbers)
    elif suffix == ".txt":
        text, warnings = extract_txt(file_path, drop_page_numbers=drop_page_numbers)
    else:
        raise ExtractionError(
            f"Desteklenmeyen dosya türü: {suffix or '(uzantısız)'}. "
            "Lütfen EPUB, PDF veya TXT yükleyin."
        )

    # EPUB ve TXT'de sayfa numaraları metnin içine karışmış olarak gelir;
    # PDF'te ise sayfa kenarlarındaki bloklar yukarıda ayıklanır.
    if drop_page_numbers:
        text, removed = remove_page_numbers(text)
        if removed:
            warnings.append(
                f"{removed} sayfa numarası metinden otomatik olarak temizlendi."
            )

    return text, warnings
