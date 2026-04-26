"""
auto_reels_pro.py — Pipeline PRO: smart highlights + chunks + Ken Burns + fades.

Convierte 1 video largo en N reels verticales 1080x1920 listos para subir.

Mejoras vs auto_reels.py basico:
  - Smart highlights: elige los N momentos MAS interesantes del video
    (ventanas con mas densidad de palabras) en lugar de N partes iguales.
  - Smart cuts: alinea los cortes a final de frase (no a media palabra).
  - Subtitulos en chunks de N palabras animados (estilo viral).
  - Ken Burns: zoom progresivo 6% durante cada clip -> sensacion de movimiento.
  - Fades de entrada/salida video y audio.
  - Color pop, lanczos scale, encoding pulido (CRF 18, preset slow).

Uso:
    python scripts/auto_reels_pro.py input/video.mp4 6
    python scripts/auto_reels_pro.py input/video.mp4 6 --equal
    python scripts/auto_reels_pro.py input/video.mp4 6 --duration 30
    python scripts/auto_reels_pro.py input/video.mp4 6 --chunk 4
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("[ERROR] Falta faster-whisper. Instala: pip install -r requirements.txt")
    sys.exit(1)


# ===== CONFIGURACION DE ESTILO =====
BURNS_ZOOM = 0.06         # 6% zoom durante el clip (Ken Burns)
FADE = 0.4                # segundos de fade in/out
TARGET_CLIP_LEN = 35.0    # duracion ideal por clip
WORDS_PER_CHUNK = 3       # palabras por bocadillo de subtitulo
FPS = 30                  # fps de salida

# Cabecera ASS para subtitulos viral. Estilo:
#  - Arial Black 88px (grande para movil)
#  - Color principal blanco, secundario amarillo
#  - Outline negro grueso, sombra
#  - Posicion central-baja (MarginV=560 en canvas 1920)
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Reels,Arial Black,88,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,1,0,1,7,3,2,80,80,560,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# ===== UTILIDADES =====

def get_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def fmt_ass_time(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def ffmpeg_path_for_filter(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", r"\:")


# ===== SMART HIGHLIGHTS (ITER D) =====

def _build_candidates(all_segs: list, target_dur: float, min_ratio: float) -> list[dict]:
    candidates = []
    for i, (s_start, _, _) in enumerate(all_segs):
        end = s_start
        words = 0
        for j in range(i, len(all_segs)):
            seg_s, seg_e, seg_t = all_segs[j]
            if seg_e - s_start > target_dur * 1.15:
                break
            words += len(seg_t.split())
            end = seg_e
        actual_dur = end - s_start
        if actual_dur < target_dur * min_ratio:
            continue
        wps = words / max(actual_dur, 1.0)
        score = wps * 1.0 + (actual_dur / target_dur) * 0.5
        candidates.append({
            "start": s_start, "end": end, "score": score, "words": words
        })
    return candidates


def _greedy_select(candidates: list[dict], n: int) -> list[dict]:
    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
    selected: list[dict] = []
    for c in candidates:
        if any(not (c["end"] <= s["start"] or c["start"] >= s["end"]) for s in selected):
            continue
        selected.append(c)
        if len(selected) >= n:
            break
    return selected


def find_highlights(all_segs: list, target_dur: float, n: int) -> list[dict]:
    """
    Busca los N tramos de target_dur con mayor densidad de palabras.
    Estrategia adaptativa: si no encuentra N suficientes con criterio
    estricto, relaja el minimo de duracion progresivamente.
    """
    if not all_segs:
        return []
    selected: list[dict] = []
    for min_ratio in (0.65, 0.50, 0.35, 0.20):
        cands = _build_candidates(all_segs, target_dur, min_ratio)
        sel = _greedy_select(cands, n)
        if len(sel) > len(selected):
            selected = sel
        if len(selected) >= n:
            break
    selected.sort(key=lambda x: x["start"])
    return selected


def fill_gaps_with_segments(existing: list[dict], all_segs: list,
                            duration: float, n_extra: int,
                            min_clip_len: float = 12.0) -> list[dict]:
    """
    Rellena con clips ubicados en los HUECOS mas grandes entre clips existentes.
    Cada nuevo clip se alinea al primer y ultimo segmento que cabe en su hueco.
    """
    if n_extra <= 0:
        return []
    sorted_existing = sorted(existing, key=lambda c: c["start"])
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for c in sorted_existing:
        if c["start"] > cursor:
            gaps.append((cursor, c["start"]))
        cursor = c["end"]
    if cursor < duration:
        gaps.append((cursor, duration))

    gaps.sort(key=lambda g: g[1] - g[0], reverse=True)
    new_clips: list[dict] = []
    for g_start, g_end in gaps:
        if g_end - g_start < min_clip_len:
            continue
        seg_in_gap = [(s, e) for s, e, _ in all_segs
                      if s >= g_start - 0.5 and e <= g_end + 0.5]
        if seg_in_gap:
            s = seg_in_gap[0][0]
            e = seg_in_gap[-1][1]
        else:
            s = g_start
            e = g_end
        if e - s < min_clip_len:
            continue
        new_clips.append({"start": s, "end": e, "score": 0, "words": 0})
        if len(new_clips) >= n_extra:
            break
    return new_clips


def equal_cuts(all_segs: list, duration: float, n: int) -> list[dict]:
    """
    Trocea en N partes iguales pero ALINEA cada corte al final de segmento
    mas cercano (smart cuts - ITER B).
    """
    if not all_segs:
        return [{"start": i * (duration / n), "end": (i + 1) * (duration / n),
                 "score": 0, "words": 0} for i in range(n)]

    target_len = duration / n
    seg_ends = [s[1] for s in all_segs]
    seg_starts = [s[0] for s in all_segs]

    cuts = [0.0]
    for i in range(1, n):
        target = i * target_len
        # Busca el final de segmento mas cercano a target
        best = min(seg_ends, key=lambda e: abs(e - target))
        cuts.append(best)
    cuts.append(duration)

    out = []
    for i in range(n):
        # Para el inicio: usa el cut anterior, pero alinea al inicio del
        # siguiente segmento si es posible (limpio en arranque).
        s = cuts[i]
        if i > 0:
            # Encuentra el seg start mas cercano DESPUES de cuts[i]
            after = [x for x in seg_starts if x >= s - 0.5]
            if after:
                s = min(after, key=lambda x: abs(x - cuts[i]))
        e = cuts[i + 1]
        out.append({"start": s, "end": e, "score": 0, "words": 0})
    return out


# ===== SUBTITULOS POR CHUNKS (ITER C) =====

def get_words_for_clip(all_words: list, t0: float, t1: float) -> list[dict]:
    """Palabras dentro del rango [t0,t1] con tiempos relativos al inicio del clip."""
    out = []
    for w in all_words:
        if w["end"] <= t0 or w["start"] >= t1:
            continue
        out.append({
            "start": max(0.0, w["start"] - t0),
            "end": min(t1 - t0, w["end"] - t0),
            "word": w["word"].strip(),
        })
    return out


def write_chunk_ass(words: list, ass_path: Path, words_per_chunk: int = 3) -> None:
    """Escribe ASS con chunks de N palabras animados (pop-in scale)."""
    if not words:
        ass_path.write_text(ASS_HEADER, encoding="utf-8")
        return

    lines = []
    i = 0
    while i < len(words):
        chunk = words[i:i + words_per_chunk]
        if not chunk:
            break
        text = " ".join(w["word"] for w in chunk).upper()
        # Limpieza: quita signos que rompen ASS o no aportan
        for ch in [",", ".", "?", "!", ":", ";", "\"", "(", ")"]:
            text = text.replace(ch, "")
        text = text.strip()
        if not text:
            i += words_per_chunk
            continue

        start = chunk[0]["start"]
        end = chunk[-1]["end"]
        # Animacion pop-in: empieza al 115% y baja a 100% en 150ms
        text_fx = (
            r"{\fad(60,0)\fscx115\fscy115"
            r"\t(0,150,\fscx100\fscy100)}"
        ) + text
        lines.append(
            f"Dialogue: 0,{fmt_ass_time(start)},{fmt_ass_time(end)},"
            f"Reels,,0,0,0,,{text_fx}"
        )
        i += words_per_chunk

    ass_path.write_text(ASS_HEADER + "\n".join(lines), encoding="utf-8")


# ===== KEN BURNS + FADES + ENCODING (ITER A) =====

def build_video_filter(clip_len: float, ass_arg: str) -> str:
    fade_out_st = max(0.0, clip_len - FADE)
    total_frames = max(1, int(clip_len * FPS))
    zoom_max = 1 + BURNS_ZOOM
    # zoom progresivo 1.0 -> 1+BURNS_ZOOM repartido en total_frames
    zoom_expr = f"min(1+{BURNS_ZOOM}*on/{total_frames}\\,{zoom_max})"
    return (
        f"fps={FPS},"
        f"crop=ih*9/16:ih,"
        f"scale=1080:1920:flags=lanczos,"
        f"zoompan=z='{zoom_expr}':d=1:s=1080x1920:fps={FPS}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
        f"eq=saturation=1.08:contrast=1.04,"
        f"ass='{ass_arg}',"
        f"fade=t=in:st=0:d={FADE},"
        f"fade=t=out:st={fade_out_st}:d={FADE}"
    )


def build_audio_filter(clip_len: float) -> str:
    fade_out_st = max(0.0, clip_len - FADE)
    return (
        f"afade=t=in:st=0:d={FADE},"
        f"afade=t=out:st={fade_out_st}:d={FADE}"
    )


# ===== MAIN =====

def main(video_path: str, n_clips: int, mode: str, target_dur: float,
         words_chunk: int) -> None:
    video = Path(video_path)
    if not video.exists():
        print(f"[ERROR] No existe: {video}")
        sys.exit(1)

    out_dir = Path("output") / f"{video.stem}_pro"
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = get_duration(video)
    print(f"[..] Video: {duration:.1f}s | modo: {mode} | "
          f"target: {target_dur:.0f}s/clip | chunk: {words_chunk} pal")

    print(f"[..] Cargando Whisper 'small' (segmentos + palabras)")
    model = WhisperModel("small", device="cpu", compute_type="int8")

    print(f"[..] Transcribiendo TODO el video con timestamps por palabra")
    segments, info = model.transcribe(
        str(video), beam_size=5, word_timestamps=True
    )
    all_segs: list = []
    all_words: list = []
    for s in segments:
        all_segs.append((s.start, s.end, s.text))
        if s.words:
            for w in s.words:
                all_words.append({"start": w.start, "end": w.end, "word": w.word})

    print(f"[..] {len(all_segs)} segmentos | {len(all_words)} palabras | "
          f"idioma: {info.language}")

    if not all_segs:
        print("[ERROR] No se detecto voz")
        sys.exit(1)

    # Selecciona los clips
    if mode == "equal":
        clips = equal_cuts(all_segs, duration, n_clips)
        print(f"[..] Modo equal: {n_clips} cortes alineados a fin de frase")
    else:
        clips = find_highlights(all_segs, target_dur, n_clips)
        print(f"[..] Modo smart: {len(clips)} highlights detectados")
        if len(clips) < n_clips:
            extra_n = n_clips - len(clips)
            print(f"[..] Faltan {extra_n}, los relleno con clips de los huecos")
            extra = fill_gaps_with_segments(clips, all_segs, duration, extra_n)
            clips = clips + extra
            clips.sort(key=lambda x: x["start"])
            if len(clips) < n_clips:
                print(f"[!!] Solo se pudieron generar {len(clips)} reels validos")

    work_dir = Path(tempfile.mkdtemp(prefix="reelspro_"))
    try:
        for i, clip in enumerate(clips):
            t0 = clip["start"]
            t1 = clip["end"]
            clip_len = t1 - t0
            clip_id = i + 1
            print(f"\n=== REEL {clip_id}/{len(clips)}  "
                  f"({t0:.0f}s -> {t1:.0f}s, {clip_len:.0f}s) ===")
            if clip.get("words"):
                print(f"    score {clip.get('score', 0):.2f} | "
                      f"{clip['words']} palabras")

            clip_words = get_words_for_clip(all_words, t0, t1)
            if not clip_words:
                print(f"[!!] Sin palabras detectadas, salto")
                continue

            ass_path = work_dir / f"reel_{clip_id}.ass"
            write_chunk_ass(clip_words, ass_path, words_chunk)

            out_path = out_dir / f"reel_{clip_id:02d}.mp4"
            ass_arg = ffmpeg_path_for_filter(ass_path)

            vf = build_video_filter(clip_len, ass_arg)
            af = build_audio_filter(clip_len)

            cmd = [
                "ffmpeg", "-y",
                "-ss", str(t0), "-i", str(video),
                "-t", str(clip_len),
                "-vf", vf,
                "-af", af,
                "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(out_path),
            ]
            print(f"[..] Render: KenBurns + chunks + lanczos + color + fades")
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"[ERROR] ffmpeg fallo en reel {clip_id}:")
                print(res.stderr[-1500:])
                continue
            print(f"[OK] {out_path.name}")

            # Miniatura JPG desde el medio del reel (utiL para portada)
            thumb_path = out_dir / f"reel_{clip_id:02d}.jpg"
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(clip_len / 2), "-i", str(out_path),
                "-frames:v", "1", "-q:v", "2", str(thumb_path),
            ], capture_output=True)

            # Info + transcripcion (texto natural de Whisper, util para descripciones)
            info_path = out_dir / f"reel_{clip_id:02d}.txt"
            clip_text = " ".join(
                t.strip() for s, e, t in all_segs
                if s >= t0 - 0.5 and e <= t1 + 0.5
            )
            info_path.write_text(
                f"REEL {clip_id} -- {clip_len:.0f}s\n"
                f"Origen: {video.name}\n"
                f"Tiempo en original: {t0:.1f}s -> {t1:.1f}s\n"
                f"Score smart: {clip.get('score', 0):.2f} | "
                f"palabras: {clip.get('words', 0)}\n"
                f"\n"
                f"TRANSCRIPCION (copia-pega para descripcion):\n"
                f"{clip_text}\n",
                encoding="utf-8",
            )
            print(f"     + thumbnail .jpg + transcripcion .txt")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n[OK] {len(clips)} reels listos en {out_dir}")


def parse_args(argv: list) -> tuple:
    args = list(argv)
    mode = "smart"
    if "--equal" in args:
        mode = "equal"
        args.remove("--equal")
    target = TARGET_CLIP_LEN
    if "--duration" in args:
        idx = args.index("--duration")
        target = float(args[idx + 1])
        del args[idx:idx + 2]
    chunk = WORDS_PER_CHUNK
    if "--chunk" in args:
        idx = args.index("--chunk")
        chunk = int(args[idx + 1])
        del args[idx:idx + 2]
    if len(args) < 2:
        return None
    return args[0], int(args[1]), mode, target, chunk


if __name__ == "__main__":
    parsed = parse_args(sys.argv[1:])
    if not parsed:
        print(__doc__)
        sys.exit(1)
    main(*parsed)
