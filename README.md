# Editor — Toolkit de edicion de video con Claude

Scripts en Python + FFmpeg + **interfaz web** para automatizar la generacion
de Reels/Shorts/TikTok desde videos largos: subtitulado, troceado en clips,
hooks, color grading, watermarks, musica con ducking, outros brandeados.

Pensado para Reels/Shorts/TikTok (9:16), YouTube largo (16:9) y videos
corporativos para webs/clientes.

## Lanzar la interfaz web (modo recomendado)

```bash
# Una vez setup (ver "Requisitos previos" abajo):
python scripts/run_web.py
```

Abre <http://localhost:8000> en el navegador. Subes el video, configuras
opciones (perfil, estilo, musica, watermark, outro), pulsas un boton, ves
progreso en vivo y descargas los reels resultantes.

Para acceder desde otro dispositivo en la misma WiFi:
```bash
python scripts/run_web.py --host 0.0.0.0 --port 8000
```
Y desde el movil/portatil: `http://<IP_DE_TU_PC>:8000/`

Para acceder desde **cualquier sitio** (tu dominio publico): ver seccion
"Despliegue en un dominio" mas abajo.

### Autenticacion (recomendado si lo expones a internet)

Por defecto el web UI **no tiene auth** — modo local sin password. Si vas a
exponerlo via Cloudflare Tunnel/VPS/etc., **define credenciales** copiando
`.env.example` a `.env` y rellenando:

```bash
cp .env.example .env
# Edita .env y pon:
#   EDITOR_USER=alvaro
#   EDITOR_PASS=algo-largo-y-aleatorio
```

`run_web.py` carga el `.env` al arrancar. Con auth activa todas las rutas
piden HTTP Basic (excepto `/api/health` para health-checks). El navegador
muestra el dialogo de login estandar y recuerda las credenciales en sesion.

`.env` esta en .gitignore — nunca se commitea.

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
│   ├── auto_batch.py          [BATCH] procesa una carpeta entera de videos
│   ├── auto_montage.py        [TEASER] junta los reels en un montaje 30-60s
│   └── run_web.py             [WEB] lanza el servidor FastAPI
├── app/                   <- interfaz web (FastAPI + HTML/CSS/JS plano)
├── music/                 <- mete aqui .mp3 para musica de fondo opcional
├── branding/              <- mete aqui tu logo .png (transparente) para watermark
├── profiles/              <- combos de flags reutilizables (viral, educativo, ...)
├── jobs/                  <- metadata de jobs del web UI (gitignored)
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

### auto_montage.py — Teaser/montaje de los reels generados
Junta los reels en un solo video con crossfades entre ellos. Ideal como
"aviso" en feed o story que envia trafico a los reels individuales.
```bash
python scripts/auto_montage.py output/test_es_pro                  # 6s por reel + 0.4s xfade
python scripts/auto_montage.py output/test_es_pro --per-clip 5     # 5s por reel
python scripts/auto_montage.py output/test_es_pro --per-clip 8 --xfade 0.6
```
Salida: `output/<carpeta>_montage.mp4` con todos los reels concatenados con
transiciones suaves video + audio.

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

**Watermark / logo (`--watermark`):**
Superpone un PNG (idealmente con transparencia) en una esquina de cada reel.
```bash
python scripts/auto_reels_pro.py input/v.mp4 6 --watermark branding/logo.png
python scripts/auto_reels_pro.py input/v.mp4 6 --watermark branding/logo.png --watermark-pos br --watermark-scale 12
```
- `--watermark-pos`: `br` (default) `bl` `tr` `tl` (esquinas).
- `--watermark-scale`: tamano como % del ancho (12 = 12% de 1080px = 130px).
- Mete tu logo en `branding/` (gitignored para no commitear archivos privados).

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

## Despliegue en un dominio (acceso desde cualquier sitio)

La pipeline (FFmpeg + Whisper + Python) es **demasiado pesada** para
Hostinger SHARED. Necesitas que el servidor este en una maquina con CPU
suficiente. Tres caminos:

### Opcion A — Cloudflare Tunnel desde tu PC (gratis, mas rapido de montar)

Tu PC sigue procesando los videos, pero un tunel publica el puerto 8000
en internet con tu propio dominio. Sin abrir puertos en el router.

1. Instala cloudflared:
   ```powershell
   winget install Cloudflare.cloudflared
   ```
2. Login: `cloudflared tunnel login` (abre el navegador, autoriza tu cuenta CF).
3. Crea un tunel: `cloudflared tunnel create editor`
4. En el panel de Cloudflare, anade un DNS record CNAME `editor.tu-dominio.com`
   apuntando al ID del tunel.
5. Ejecuta el tunel:
   ```bash
   cloudflared tunnel --url http://localhost:8000 run editor
   ```
6. Lanza el servidor: `python scripts/run_web.py`
7. Accede desde donde sea: `https://editor.tu-dominio.com`

### Opcion B — VPS dedicado (ideal para uso intensivo)

VPS con >= 2 CPU + 4GB RAM. Hostinger tiene VPS plan KVM 2 (~6 EUR/mes),
o cualquier otro proveedor (Hetzner, DigitalOcean, etc).

1. Instalar en el VPS (Ubuntu/Debian):
   ```bash
   apt update && apt install -y python3.11-venv ffmpeg git
   git clone https://github.com/ilernetworklleida/editor.git
   cd editor
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Lanzar como servicio systemd (crear `/etc/systemd/system/editor.service`):
   ```ini
   [Unit]
   Description=Editor reels factory
   After=network.target

   [Service]
   Type=simple
   User=root
   WorkingDirectory=/root/editor
   ExecStart=/root/editor/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
3. `systemctl enable --now editor` para arrancarlo.
4. Apunta tu dominio al VPS via DNS A record.
5. Pon nginx delante con SSL (certbot) o usa Caddy (mas facil):
   ```
   editor.tu-dominio.com {
       reverse_proxy localhost:8000
   }
   ```

### Opcion C — Tailscale (acceso privado, no expuesto a internet)

Solo tu y tus dispositivos pueden ver el servidor. Mas seguro si es
herramienta personal:
1. Instala Tailscale en tu PC y en el dispositivo desde el que quieras acceder.
2. Lanza el servidor con `--host 0.0.0.0`.
3. Accede en `http://<nombre-de-tu-pc>.tail<XYZ>.ts.net:8000` desde cualquier
   dispositivo logueado en tu red Tailscale.

## Notas

- Los videos de `input/` y `output/` NO se commitean (son pesados). Solo el codigo.
- Whisper la primera vez descarga el modelo (~500MB para `small`). Tarda. Despues queda en cache.
- Para edicion creativa fina (transiciones, color) sigues necesitando DaVinci/Premiere/CapCut.
- Repo: https://github.com/ilernetworklleida/editor
