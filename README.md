# REEL/LAB

Pipeline personal de generacion de **reels verticales 1080x1920** (TikTok / Reels /
YouTube Shorts) desde videos largos o URLs (YouTube, TikTok, Instagram, Twitter,
Vimeo, etc.). Stack: Python + FFmpeg + Whisper + Claude API + FastAPI.

Disenado para uso interno. Un solo admin (`info@ilernetworklleida.com` por defecto).

## Pipeline en una linea

```
URL/video -> Whisper transcribe -> Claude elige momentos virales (con tus
instrucciones libres) -> recorte 9:16 -> Ken Burns -> color grading -> subs
animados estilo viral -> hook arriba -> musica con ducking -> watermark ->
outro brandeado -> .mp4 + .jpg + .txt + .srt + .segs.json
```

Despues puedes generar un teaser-montage con crossfades, variantes A/B con otro
estilo, editar las transcripciones y descargar .srt corregidos.

---

## Inicio rapido

```bash
# 1. Pre-requisitos: FFmpeg + yt-dlp (Windows)
winget install Gyan.FFmpeg
winget install yt-dlp.yt-dlp

# 2. Entorno Python
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 3. Wizard interactivo (crea .env, verifica deps, valida API key)
python scripts/setup.py

# 4. Lanza la web
python scripts/run_web.py
```

Abre <http://localhost:8000>. Login con `info@ilernetworklleida.com` + tu password.

### Atajos de teclado

| Tecla | Accion |
| --- | --- |
| `/` | Focus en el buscador (en /jobs) |
| `g` luego `h` | Ir a Home |
| `g` luego `j` | Ir a Jobs |
| `g` luego `s` | Ir a Schedules |
| `g` luego `p` | Ir a Perfiles |
| `g` luego `t` | Ir a API tokens |
| `g` luego `a` | Ir a Stats |

---

## Que hace el pipeline

Cada reel generado incluye:

- **Vertical 9:16** 1080x1920 H.264 con escalado lanczos
- **Smart highlights**: heuristica de densidad de palabras (gratis) o Claude API
  (~$0.05/video, 5x mejor seleccion)
- **Subtitulos en chunks animados** estilo viral (3 estilos: clean / hype / money)
- **Hook** del clip arriba en grande durante 1.2s al inicio
- **Ken Burns**: zoom progresivo 6% durante el clip
- **Color grading** (5 presets: none / warm / cold / cinematic / vivid)
- **Fades** de entrada/salida video y audio (0.4s)
- **Musica de fondo** opcional con ducking sidechain (la musica baja con la voz)
- **Watermark** opcional (PNG) en cualquier esquina
- **Outro brandeado** opcional (texto custom durante los ultimos N segundos)
- **Subtitulos EN traducidos** opcional (.srt extra para audiencia internacional)
- **Por reel**: .mp4 + .jpg miniatura + .txt con transcripcion + 8 hashtags

Tras los reels puedes generar un **teaser/montage** que junta los N reels en un
solo video corto con crossfades, ideal como aviso en el feed.

---

## Estructura del proyecto

```
Editor/
├── app/                       FastAPI + HTML/CSS/JS plano (interfaz web)
│   ├── main.py
│   ├── templates/             home, job, jobs, stats, profiles, login
│   └── static/                styles.css + app.js + job.js
├── scripts/
│   ├── auto_reels_pro.py      Pipeline principal (CLI)
│   ├── auto_reels.py          Version simple (line-based subs)
│   ├── auto_yt.py             URL YouTube -> pipeline
│   ├── auto_montage.py        Junta reels en teaser con crossfades
│   ├── auto_batch.py          Procesa varios videos en serie
│   ├── run_web.py             Lanza el servidor web
│   ├── 01_subtitular.py       Solo .srt desde un video
│   ├── 02_clips_de_largo.py   Solo trocear sin subs
│   ├── 03_cortar_silencios.py Solo limpiar pausas
│   └── 04_comprimir_web.py    Solo batch H.264 + faststart
├── input/                     Videos fuente (gitignored)
├── output/                    Reels generados (gitignored)
├── music/                     .mp3 para musica de fondo (gitignored)
├── branding/                  Logos PNG para watermark (gitignored)
├── profiles/                  Combos de flags reutilizables (.json)
├── jobs/                      Metadata + logs de jobs (gitignored)
├── requirements.txt
└── .env.example               Template de variables de entorno
```

---

## Interfaz web — paginas

| Path | Que es |
| --- | --- |
| `/` | Form principal: subir video, pegar URL(s), elegir perfil/musica/watermark/outro, instrucciones libres para la IA, lanzar job |
| `/jobs` | Listado paginado de jobs con busqueda y filtro de estado |
| `/job/{id}` | Detalle: progreso en vivo, log streaming, galeria de reels con AI reason, descarga ZIP, variante A/B, teaser, re-ejecutar, cancelar |
| `/job/{id}/edit/{N}` | Editor de transcripcion del reel N (descarga .srt corregido) |
| `/schedules` | CRUD de schedules cron-style. Procesa URLs en automatico (ej: cada lunes 9am) |
| `/profiles` | CRUD de perfiles (combos de flags) desde navegador |
| `/stats` | Uso de disco + jobs por estado + cost tracking Claude API + estado de servicios + cleanup |
| `/login` | Form de acceso (email + password) |

### Header
- "Editor" logo en naranja
- Nav: Home, Jobs, Perfiles, Stats, GitHub, email del usuario, boton logout
- Sticky con backdrop-blur

### Home: 3 fuentes de video
1. **Subir archivo**: drag&drop o click (sube async, recarga lista)
2. **Pegar URL YouTube**: una URL = 1 job. Multiples URLs (una por linea) = N jobs en bulk
3. **Usar existente**: dropdown con videos en `input/`

### Job page
- Badge de estado (queued/running/done/error/cancelled/interrupted) animada
- Barra de progreso parseada del log: starting -> downloading -> loading whisper
  -> transcribing -> selecting -> rendering reel X/N -> done (con %)
- Log live (poll cada 1.5s)
- Boton **Cancelar** mientras esta running (taskkill /T en Win, killpg en Unix)
- Galeria de reels con video player, descarga mp4, copiar transcripcion+hashtags
- AI reason visible en cada reel card (si se uso Claude API)
- Boton **Descargar todo (ZIP)** con todo el output
- Seccion **Generar variante**: nuevo job con mismo input pero distinto estilo+grade
- Seccion **Teaser/montage**: genera un montage de los N reels con crossfades
- Notificacion del navegador cuando termina

---

## Configuracion via .env

Crea `.env` desde `.env.example`:

```bash
# Auth
ADMIN_EMAIL=info@ilernetworklleida.com   # default ya configurado
ADMIN_PASS=tu-password-largo-y-seguro
EDITOR_SECRET=$(python -c "import secrets;print(secrets.token_hex(32))")

# Claude API (opcional, para smart highlights)
ANTHROPIC_API_KEY=sk-ant-...
HIGHLIGHTS_MODEL=claude-opus-4-7   # default; alternativas: claude-sonnet-4-6, claude-haiku-4-5

# Webhook al terminar job (Slack/Discord/IFTTT/generico)
WEBHOOK_URL=https://hooks.slack.com/...
WEBHOOK_PUBLIC_URL=https://editor.tu-dominio.com   # para link clickable en notif
```

Sin `ADMIN_PASS` definido: modo local sin auth. Sin `ANTHROPIC_API_KEY`: la IA
cae a heuristica de densidad. Sin `WEBHOOK_URL`: no se envian notificaciones.

---

## Perfiles incluidos

3 perfiles JSON listos en `profiles/`:

| Perfil | Estilo | Grade | Chunk | Duracion | Para |
| --- | --- | --- | --- | --- | --- |
| `viral` | hype | vivid | 2 | 25s | TikTok energico |
| `educativo` | clean | warm | 3 | 40s | Tutoriales |
| `finanzas` | money | cold | 3 | 30s | Trading / business |

Crea el tuyo desde la UI en `/profiles` (sidebar -> Nuevo perfil) o copiando un
`.json`.

---

## CLI (avanzado)

Si prefieres saltarte la UI, todo funciona como CLI:

```bash
# Pipeline completo
python scripts/auto_reels_pro.py input/v.mp4 6

# Con todos los superpoderes
python scripts/auto_reels_pro.py input/v.mp4 6 \
    --profile viral \
    --music music/cancion.mp3 --music-vol 0.25 --duck \
    --watermark branding/logo.png --watermark-pos br \
    --outro "@ilernetworklleida\nSigueme para mas" \
    --skip-start 30 --skip-end 30 \
    --ai-highlights \
    --translate-en

# Desde URL YouTube
python scripts/auto_yt.py "https://youtu.be/ID" 6 --profile viral

# Procesar carpeta entera
python scripts/auto_batch.py input/ 4 --profile educativo

# Teaser despues
python scripts/auto_montage.py output/v_pro --per-clip 5
```

### Flags disponibles

```
--style {clean|hype|money}         Estilo de subs
--grade {none|warm|cold|cinematic|vivid}   Color grading
--duration N                       Segundos por reel (default 35)
--chunk {N|auto}                   Palabras por bocadillo (auto = segun WPS)
--skip-start N                     Ignora primeros N segundos
--skip-end N                       Ignora ultimos N segundos
--music PATH                       Musica de fondo (.mp3)
--music-vol 0.18                   Volumen musica (0-1)
--duck                             Ducking sidechain (musica baja con voz)
--watermark PATH                   Logo PNG (idealmente con transparencia)
--watermark-pos {br|bl|tr|tl}     Esquina (default br)
--watermark-scale 12               Tamano % del ancho
--outro "TEXT\nCTA"               Outro brandeado (\n = salto de linea)
--outro-duration 1.5               Segundos del outro
--no-hook                          Quita el gancho de 3 palabras al inicio
--translate-en                     Genera .srt EN extra
--ai-highlights                    Claude API selecciona los mejores momentos
--instructions "..."               Texto libre que Claude usa como criterio prioritario
--no-normalize                     Desactiva loudnorm (audio normalizado por defecto)
--equal                            Corte en N iguales (con smart cuts)
--profile NAME                     Carga combo desde profiles/NAME.json
--out-suffix _x                    Sufijo del directorio output (para variantes)
```

---

## Despliegue en un dominio

La pipeline (FFmpeg + Whisper + Python) es **demasiado pesada** para Hostinger
SHARED. Necesitas que el servidor este en una maquina con CPU suficiente.

### Opcion A — Cloudflare Tunnel desde tu PC (gratis)

Tu PC procesa, el tunel publica `editor.tu-dominio.com`. 5 min de setup.

```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create editor
# En el panel de Cloudflare: CNAME editor.tu-dominio.com -> ID del tunel
cloudflared tunnel --url http://localhost:8000 run editor
```

En otra terminal:
```bash
python scripts/run_web.py
```

Accede en `https://editor.tu-dominio.com` con login.

### Opcion B — VPS dedicado (~6 EUR/mes)

VPS con >= 2 CPU + 4GB RAM. Hostinger VPS KVM 2 / Hetzner / DigitalOcean.

```bash
# En el VPS (Ubuntu/Debian)
apt update && apt install -y python3.11-venv ffmpeg git
git clone https://github.com/ilernetworklleida/editor.git
cd editor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edita con tus credenciales

# Servicio systemd (/etc/systemd/system/editor.service):
# [Service]
# ExecStart=/root/editor/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# Restart=always

systemctl enable --now editor

# Caddy (mas facil que nginx):
# editor.tu-dominio.com {
#     reverse_proxy localhost:8000
# }
```

### Opcion C — Tailscale (privado)

Instala Tailscale en tu PC y dispositivos. Lanza con `--host 0.0.0.0`. Accede en
`http://<nombre-pc>.tail<XYZ>.ts.net:8000`. No expuesto a internet, solo tu red.

---

## Notas operativas

- **Whisper** descarga ~470MB del modelo `small` la primera vez. Despues queda
  en cache (`~/.cache/huggingface/`).
- **Tiempo de proceso**: Whisper en CPU = ~1x duracion del audio. Render por reel
  con preset `slow` + CRF 18 = ~1-2 min/reel. Total: ~1.5-3 min por reel.
- **Disco**: input/ y output/ pueden llenarse rapido (videos pesados). Usa
  `/stats` para ver uso y la **Limpieza** para borrar jobs viejos.
- **Coste API**: con Opus 4.7 son ~$0.05/video. Con prompt caching, los re-runs
  del mismo video bajan a ~$0.01.
- **Para edicion creativa fina** (transiciones complejas, color grading manual,
  motion graphics) sigues necesitando DaVinci Resolve / Premiere / CapCut. Esto
  es para los reels masivos automaticos.

---

## Repositorio

<https://github.com/ilernetworklleida/editor>
