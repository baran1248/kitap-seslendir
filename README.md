# 🎧 Kitap Seslendir

EPUB, PDF ve TXT dosyalarını **MP3** veya **M4B** sesli kitaba dönüştüren,
tamamen kendi bilgisayarınızda çalışan bir Gradio uygulaması.

Metin önce düzenlenebilir bir kutuda gösterilir; sesi, okuma hızını ve tonu
seçtikten sonra dönüştürme tek tuşla başlar. Seslendirme için
[Microsoft edge-tts](https://github.com/rany2/edge-tts) kullanıldığından
**internet bağlantısı gerekir**; dosyalarınız üçüncü taraf bir sunucuya
yüklenmez, yalnızca metin seslendirme servisine gönderilir.

---

## Özellikler

- **Kitap Seslendir** başlıklı, sürükle-bırak destekli arayüz
- **EPUB**: `ebooklib` + `BeautifulSoup` ile temiz metin çıkarma
  - `application/xhtml+xml` ve `text/html` media-type'lı bölümler
  - Internet Archive OCR kitaplarında uyarı ve boilerplate temizliği
- **PDF**: PyMuPDF (`fitz`) ile metin çıkarma
  - Üst/alt marj kırpma yüzdesi (üst/alt başlıkları atlar)
  - Sayfa numaralarını atlama
  - Satır sonu tirelerini birleştirme (`kelime-` + `sonu` → `kelimesonu`)
  - Taranmış (metin katmanı olmayan) PDF'lerde uyarı
- **TXT**: UTF-8, Windows-1254 ve ISO-8859-9 kodlamalarını otomatik dener
- Metin düzenleme, karakter/kelime sayısı ve **tahmini dinleme süresi**
- `tr-TR` ve `Multilingual` seslerin otomatik listelenmesi
  (varsayılan: `tr-TR-AhmetNeural`)
- Okuma hızı (**-50% … +50%**) ve ses tonu (**-50 … +50 Hz**) kaydırıcıları
- **Sesi Önizle** düğmesiyle anlık Türkçe örnek
- **MP3**, **M4B** veya ikisi birden
- Metni cümle sonlarından ~3000 karakterlik parçalara bölme
- Her parça için **3 deneme** ve otomatik bekleme
- Canlı ilerleme çubuğu, geçen süre ve tahmini kalan süre
- FFmpeg ile birleştirme, geçici dosya temizliği ve indirme düğmeleri
- FFmpeg kurulu değilse başlangıçta net uyarı

---

## Gereksinimler

| Bileşen | Sürüm / not |
|---|---|
| Python | 3.10 veya üzeri (3.13 ile test edildi) |
| FFmpeg | Ses birleştirme ve M4B için zorunlu |
| İnternet | edge-tts seslendirmesi için gerekli |

### FFmpeg kurulumu

**Windows (önerilen):**

```powershell
winget install Gyan.FFmpeg
```

veya Chocolatey ile:

```powershell
choco install ffmpeg
```

Kurulumdan sonra terminali kapatıp yeniden açın ve doğrulayın:

```powershell
ffmpeg -version
```

**macOS:** `brew install ffmpeg`
**Linux (Debian/Ubuntu):** `sudo apt install ffmpeg`

> FFmpeg PATH'te değilse uygulama başlangıçta kırmızı bir uyarı gösterir ve
> dönüştürme düğmesini devre dışı bırakır.

---

## Kurulum

```powershell
cd KitapSeslendir

# Sanal ortam (önerilir)
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # PowerShell
# .\.venv\Scripts\activate.bat      # CMD

python -m pip install --upgrade pip
pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Çalıştırma

```powershell
python app.py
```

Tarayıcı otomatik açılır. Açılmazsa <http://127.0.0.1:7860> adresine gidin.

Windows'ta hazır kurulum için `start.bat` dosyasına çift tıklayabilirsiniz;
ilk çalıştırmada `.venv` oluşturup bağımlılıkları kurar.

---

## Kullanım

1. EPUB, PDF veya TXT dosyanızı sürükleyip bırakın.
2. PDF için gerekirse **PDF ayarları** bölümünden marj kırpmayı değiştirip
   **PDF ayarlarıyla yeniden çıkar** düğmesine basın.
3. Çıkarılan metni kontrol edin / düzenleyin. Alanın dışına tıkladığınızda
   karakter sayısı ve tahmini süre güncellenir.
4. Ses listesinden bir ses seçin. **Sesi Önizle** ile kısa bir örnek dinleyin.
5. Hız ve ton kaydırıcılarını ayarlayın.
6. Çıktı formatını seçin (**MP3**, **M4B** veya ikisi).
7. **Seslendir** düğmesine basın; ilerleme çubuğunu izleyin.
8. İşlem bitince **MP3 indir** / **M4B indir** düğmeleri görünür.

Çıktılar proje klasöründeki `outputs/<kitap-adı>_<tarih>/` altına da kaydedilir.
Önizleme dosyaları `outputs/_onizlemeler/` içinde tutulur ve bir günden eskiler
otomatik silinir.

---

## Proje yapısı

```
KitapSeslendir/
├── app.py             # Gradio arayüzü ve dönüşüm akışı
├── extractors.py      # EPUB / PDF / TXT metin çıkarma
├── tts_engine.py      # edge-tts, ses listesi, parçalama, sentez
├── audio_tools.py     # FFmpeg kontrolü, MP3 birleştirme, M4B
├── requirements.txt
├── start.bat          # Windows için hızlı başlatıcı
├── README.md
└── outputs/           # Üretilen ses dosyaları (otomatik oluşur)
```

## Nasıl çalışır?

- Metin, cümle sonlarından (`.!?…`) yaklaşık **3000 karakterlik** parçalara
  bölünür. 3000 karakteri aşan tek bir cümle kelime sınırından ayrılır.
- Parçalar sırayla edge-tts'e gönderilir. Bir parça hata verirse **3 kez**
  denenir; denemeler arasında artan süreyle beklenir.
- Parçalar FFmpeg `concat` demuxer'ı ile kayıpsız birleştirilir; gerekirse
  yeniden kodlama yedeği devreye girer.
- M4B, AAC (64 kbps, mono, 24 kHz) olarak `ipod` muxer'ı ile üretilir ve kitap
  adı metadata olarak yazılır. Bölüm işaretleri otomatik eklenmez.
- Tahmini süre, normal hızda saniyede ~15 karakter varsayımıyla hesaplanır ve
  hız kaydırıcısına göre ölçeklenir.

## Sorun giderme

| Sorun | Çözüm |
|---|---|
| “FFmpeg bulunamadı” | FFmpeg'i kurun, PATH'e ekleyin, uygulamayı yeniden başlatın. |
| “Ses listesi alınamadı” | İnternet bağlantısını kontrol edip **⟳** düğmesiyle yenileyin; yerleşik liste kullanılabilir. |
| “PDF'te seçilebilir metin bulunamadı” | PDF taranmıştır; OCR (ör. `ocrmypdf`) uygulanmış sürümü kullanın. |
| Seslendirme yarıda kaldı | Her parça 3 kez denenir; ağ sürekli kopuyorsa metni daha küçük dosyalara bölün. |
| M4B oynatılmıyor | M4B/AAC desteği olan bir oynatıcı kullanın; gerekirse MP3 çıktısını tercih edin. |
| Çok uzun kitaplarda tarayıcı yavaşlıyor | Metin kutusu büyük metinlerde ağırdır; kitabı bölümlere ayırarak dönüştürebilirsiniz. |

## Notlar

- edge-tts, Microsoft'un çevrimiçi konuşma servisini kullanır; seslendirme
  sırasında internet gerekir ve servis kullanım koşulları geçerlidir.
- Seslendirdiğiniz kitapların telif haklarına uymak sizin sorumluluğunuzdadır.
- Uygulama yalnızca `127.0.0.1` üzerinde dinler; ağdaki diğer cihazlara açık
  değildir.

## Masaüstü kısayolu ve otomatik kapanma

- Masaüstündeki **Kitap Seslendir** kısayolu uygulamayı doğrudan başlatır.
- Tarayıcı sekmesini kapattığınızda uygulama arka planda kendiliğinden kapanır
  (yaklaşık 2 saniye). Sayfayı yenilerseniz kapanma iptal edilir.
- Zaten açıkken kısayola tekrar basarsanız yeni bir sunucu başlatılmaz,
  mevcut sayfa tarayıcıda açılır.
- Metindeki sayfa numaraları (ör. 12, Sayfa 12, - 12 -, 12 / 340)
  tüm dosya türlerinde otomatik olarak temizlenir. **PDF ayarları** bölümündeki
  *Sayfa numaralarını otomatik temizle* kutusuyla bu davranış kapatılabilir.
