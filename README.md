# Editor — Toolkit de edicion de video con Claude

Scripts en Python + FFmpeg para automatizar tareas de edicion de video:
subtitulado, troceado en clips, corte de silencios y compresion para web.

Pensado para Reels/Shorts/TikTok (9:16), YouTube largo (16:9) y videos
corporativos para webs/clientes.

---

## Requisitos previos (UNA vez)

### 1. FFmpeg y yt-dlp (Windows con winget)
```powershell
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp
```
Cierra y reabre la terminal. Comprueba:
```bash
ffmpeg -version
yt-dlp --version
```

### 2. Entorno Python aislado
Desde la raiz del proyecto:
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

A partir de aqui, antes de usar cualquier script:
```bash
.venv\Scripts\activate
```

---

## Estructura

```
Editor/
├── input/                 <- mete aqui tus videos originales
├── output/                <- aqui salen los resultados
├── scripts/
│   ├── 01_subtitular.py       audio -> .srt automatico (Whisper)
│   ├── 02_clips_de_largo.py   1 video largo -> N clips (vertical opcional)
│   ├── 03_cortar_silencios.py limpia pausas largas
│   ├── 04_comprimir_web.py    batch optimizar para web (H.264)
│   ├── auto_reels.py          [BOTON SIMPLE] N reels verticales subtitulados
│   ├── auto_reels_pro.py      [BOTON PRO] highlights + chunks + KenBurns + fades
│   └── auto_yt.py             [URL -> REELS] baja de YouTube + procesa pro en 1 comando
├── requirements.txt
└── README.md
```

---

## Uso de cada script

### 01 — Subtitular automaticamente
Genera un archivo `.srt` desde el audio del video usando Whisper.
Autodetecta el idioma por defecto.
```bash
python scripts/01_subtitular.py input/mi_video.mp4
python scripts/01_subtitular.py input/mi_video.mp4 medium       # mas preciso, mas lento
python scripts/01_subtitular.py input/mi_video.mp4 small es     # forzar idioma (es, en, ca, fr...)
```
Modelos: `tiny` < `base` < `small` (default) < `medium` < `large`.

Salida: `output/mi_video.srt`

### 02 — Trocear video largo en N clips
Divide en partes iguales. Con `--vertical` recorta el centro a 9:16
(ideal para sacar 5-10 Reels de un podcast de 1h).
```bash
python scripts/02_clips_de_largo.py input/podcast.mp4 5
python scripts/02_clips_de_largo.py input/podcast.mp4 8 --vertical
```

Salida: `output/podcast/podcast_clip01.mp4 ...`

### 03 — Cortar silencios
Detecta y elimina pausas largas. Util para podcast/tutorial sin re-grabar.
```bash
python scripts/03_cortar_silencios.py input/video.mp4
python scripts/03_cortar_silencios.py input/video.mp4 --umbral -30 --min 0.7
```
- `--umbral`: dB por debajo de los cuales se considera silencio (default -30)
- `--min`: duracion minima del silencio en segundos (default 0.7)

Salida: `output/video_sin_silencios.mp4`

### auto_reels.py — Boton simple: 1 video -> N reels subtitulados
Pipeline en 1 comando: trocea en N partes iguales + 9:16 + subtitula
+ quema subs (linea por linea) + fades + color pop + lanczos + CRF 18.
```bash
python scripts/auto_reels.py input/video.mp4 6
```
Salida: `output/<video>_reels/reel_01.mp4 ... reel_06.mp4`

### auto_yt.py — Boton magico desde URL
Un solo comando: pega URL de YouTube, indica cuantos reels quieres, listo.
Cachea el video en `input/yt_<ID>.mp4` para no re-bajar si re-procesas.
```bash
python scripts/auto_yt.py "https://youtube.com/watch?v=ID" 6
python scripts/auto_yt.py "https://youtube.com/watch?v=ID" 6 --range 60 600   # solo de 60s a 10min
python scripts/auto_yt.py "https://youtube.com/watch?v=ID" 6 --duration 30 --chunk 4
```
Internamente llama a auto_reels_pro.py con todos sus features.

### auto_reels_pro.py — Boton PRO: smart highlights + estilo viral
Como el simple pero MUCHO mejor:
- **Smart highlights**: detecta los N momentos mas interesantes (densidad de
  palabras), no trocea en partes iguales. Si no encuentra suficientes,
  rellena los huecos con clips alineados a frase.
- **Smart cuts**: cortes alineados a final de frase (no parte palabras).
- **Word chunks**: subtitulos en grupos de 3 palabras animados con pop-in
  (estilo viral Reels), no linea entera.
- **Ken Burns**: zoom progresivo 6% durante cada clip -> sensacion de
  movimiento sin tu hacer nada.
- **Estilo**: Arial Black 88px, blanco con outline negro grueso.
```bash
python scripts/auto_reels_pro.py input/video.mp4 6
python scripts/auto_reels_pro.py input/video.mp4 6 --equal       # corte en N partes iguales (con smart-cuts)
python scripts/auto_reels_pro.py input/video.mp4 6 --duration 30 # clips de 30s en vez de 35s
python scripts/auto_reels_pro.py input/video.mp4 6 --chunk 4     # 4 palabras por bocadillo
```
Salida por cada reel:
- `reel_NN.mp4` — el video listo para subir
- `reel_NN.jpg` — miniatura (frame del medio) por si quieres portada custom
- `reel_NN.txt` — transcripcion del clip + metadata, copia-pega para descripcion

### 04 — Comprimir para web (batch)
Procesa TODA la carpeta `input/` con preset web (H.264 + faststart).
```bash
python scripts/04_comprimir_web.py
python scripts/04_comprimir_web.py --crf 26   # mas comprimido (peor calidad)
```
- `--crf`: 18 (calidad alta) - 23 (default) - 28 (calidad baja). Mas alto = menor peso.

Salida: `output/<nombre>_web.mp4`

---

## Flujo tipico (caso real)

**Sacar 6 Reels de un podcast de 1 hora:**
```bash
.venv\Scripts\activate

# 1. Trocea y recorta a vertical
python scripts/02_clips_de_largo.py input/podcast.mp4 6 --vertical

# 2. Subtitula cada clip resultante (manualmente uno a uno o bucle)
python scripts/01_subtitular.py output/podcast/podcast_clip01.mp4
# ... etc

# 3. (Opcional) Quemar los .srt sobre el video con ffmpeg manualmente
```

---

## Notas

- Los videos de `input/` y `output/` NO se commitean (son pesados). Solo el codigo.
- Whisper la primera vez descarga el modelo (~500MB para `small`). Tarda. Despues queda en cache.
- Para edicion creativa fina (transiciones, color) sigues necesitando DaVinci/Premiere/CapCut.
- Repo: https://github.com/ilernetworklleida/editor
