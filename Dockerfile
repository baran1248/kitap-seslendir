# Kitap Seslendir - Render.com için web sürümü
# kaynak: https://github.com/baran1248/kitap-seslendir

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GRADIO_SERVER_NAME=0.0.0.0 \
    PORT=10000

# FFmpeg: ses parçalarini birlestirmek ve M4B uretmek icin gerekli
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app.py extractors.py tts_engine.py audio_tools.py ./

# Render PORT ortam degiskenini kullanir; app.py bunu okur.
CMD ["python", "app.py"]
