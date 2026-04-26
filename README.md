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
│   ├── auto_reels_pro.py      [BOTON PRO] highlights+chunks+KenBurns+hook+musica
│   ├── auto_yt.py             [URL -> REELS] baja de YouTube + procesa pro
│   └── auto_batch.py          [BATCH] procesa una carpeta entera de videos
├── music/                 <- mete aqui .mp3 para musica de fondo opcional
├── profiles/              <- combos de flags reutilizables (viral, educativo, ...)
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

### auto_batch.py — Procesa varios videos en serie
Mismo pipeline que `auto_reels_pro` pero aplicado a multiples videos.
Util para cuando tienes una carpeta llena de fuentes.
```bash
python scripts/auto_batch.py input/ 4                               # procesa todo input/ con 4 reels c/u
python scripts/auto_batch.py input/ 4 --style hype --skip-start 30  # mismo flag a todos
python scripts/auto_batch.py "input/v1.mp4,input/v2.mp4" 4          # solo esos dos
```

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
python scripts/auto_reels_pro.py input/video.mp4 6 --equal              # corte en N partes iguales
python scripts/auto_reels_pro.py input/video.mp4 6 --duration 30        # clips de 30s en vez de 35s
python scripts/auto_reels_pro.py input/video.mp4 6 --chunk 4            # 4 palabras por bocadillo
python scripts/auto_reels_pro.py input/video.mp4 6 --style hype         # subs amarillo viral (clean/hype/money)
python scripts/auto_reels_pro.py input/video.mp4 6 --skip-start 60 --skip-end 30  # ignora intro y outro
```

**Estilos de subtitulos** (`--style`):
- `clean` (default): Arial Black blanco sin caps, sobrio. Ideal tutoriales/educativo.
- `hype`: Impact amarillo en CAPS, mas grande. Estilo TikTok/viral.
- `money`: Arial Black verde con outline blanco en CAPS. Estilo finanzas/business.

**Hook + musica:**
```bash
python scripts/auto_reels_pro.py input/v.mp4 6 --no-hook                            # quita el gancho del inicio
python scripts/auto_reels_pro.py input/v.mp4 6 --music music/cancion.mp3            # musica de fondo (vol 0.18)
python scripts/auto_reels_pro.py input/v.mp4 6 --music music/cancion.mp3 --music-vol 0.10
```
Por defecto cada reel arranca con un overlay grande arriba (3 primeras palabras
durante 1.2s con animacion pop-in) que captura atencion en el feed.

**Color grading (`--grade`):**
- `none` (default): leve color pop (sat +8%, contraste +4%).
- `warm`: amarillento/calido. Vlogs, lifestyle.
- `cold`: azulado/frio. Tech, corporate.
- `cinematic`: desaturado + mas contraste. Look pelicula.
- `vivid`: saturacion alta. TikTok energico.

**Outro brandeado (`--outro`):**
```bash
python scripts/auto_reels_pro.py input/v.mp4 6 --outro "@tu_canal\nSigueme para mas"
python scripts/auto_reels_pro.py input/v.mp4 6 --outro "TU MARCA" --outro-duration 2.5
```
Anade un overlay centrado en pantalla durante los ultimos N segundos del reel.
`\n` se convierte en salto de linea. Estilo grande, blanco con outline negro,
fade in/out, ligero pop-in de escala.

**True ducking de musica (`--duck`):**
Sidechain compressor: la musica baja automaticamente cuando hay voz, sube
cuando hay silencio. Mucho mas pro que el mix simple.
```bash
python scripts/auto_reels_pro.py input/v.mp4 6 --music music/cancion.mp3 --duck
python scripts/auto_reels_pro.py input/v.mp4 6 --music music/cancion.mp3 --duck --music-vol 0.30
```
Con `--duck` puedes subir el `--music-vol` mas alto (0.25-0.35) porque el
compressor lo bajara cuando suene la voz.

**Perfiles (`--profile`):**
Combos de flags guardados como JSON en `profiles/`. Te ahorran teclear 10 flags
cada vez. Vienen 3 perfiles de ejemplo: `viral`, `educativo`, `finanzas`.
```bash
python scripts/auto_reels_pro.py input/v.mp4 6 --profile viral
python scripts/auto_reels_pro.py input/v.mp4 6 --profile educativo --music music/c.mp3
python scripts/auto_reels_pro.py input/v.mp4 6 --profile viral --grade warm   # override
```
Tus flags por linea de comandos **anulan** los del perfil. Para crear el tuyo:
copia uno y guardalo como `profiles/mi_canal.json`.
Salida por cada reel:
- `reel_NN.mp4` — el video listo para subir
- `reel_NN.jpg` — miniatura (frame del medio) por si quieres portada custom
- `reel_NN.txt` — transcripcion del clip + metadata + **hashtags sugeridos** (heuristica
  sobre las palabras mas frecuentes del clip, sin acentos, sin stopwords)

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
