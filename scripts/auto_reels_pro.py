"""
auto_reels_pro.py — Pipeline PRO: smart highlights + chunks + Ken Burns + fades.

Convierte 1 video largo en N reels verticales 1080x1920 listos para subir.

Features:
  - Smart highlights: elige los N momentos MAS interesantes del video.
  - Smart cuts: alinea los cortes a final de frase.
  - Subtitulos en chunks de N palabras animados (3 estilos: clean/hype/money).
  - Ken Burns: zoom progresivo 6% durante cada clip.
  - Fades de entrada/salida video y audio.
  - Color pop, lanczos scale, CRF 18 + preset slow.
  - Por reel: .mp4 + .jpg (miniatura) + .txt (transcripcion + hashtags).
  - Skip intro/outro: ignora primeros/ultimos N segundos del original.

Uso:
    python scripts/auto_reels_pro.py input/video.mp4 6
    python scripts/auto_reels_pro.py input/video.mp4 6 --equal
    python scripts/auto_reels_pro.py input/video.mp4 6 --duration 30
    python scripts/auto_reels_pro.py input/video.mp4 6 --chunk 4
    python scripts/auto_reels_pro.py input/video.mp4 6 --style hype
    python scripts/auto_reels_pro.py input/video.mp4 6 --skip-start 60 --skip-end 30
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
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

# Cabeceras ASS por preset de estilo. Cada preset es (header, force_caps).
#   clean: Arial Black blanco, sobrio, sin caps -- tutoriales/educativo.
#   hype:  Impact amarillo, caps, mas grande -- TikTok/viral.
#   money: Arial Black verde con outline blanco, caps -- finanzas/business.
_ASS_BASE = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

STYLES: dict[str, tuple[str, bool]] = {
    "clean": (
        _ASS_BASE.format(style_line=(
            "Style: Reels,Arial Black,82,&H00FFFFFF,&H0000FFFF,"
            "&H00000000,&H80000000,1,0,1,6,3,2,80,80,560,1"
        )),
        False,
    ),
    "hype": (
        _ASS_BASE.format(style_line=(
            "Style: Reels,Impact,100,&H0000FFFF,&H0000FFFF,"
            "&H00000000,&H80000000,1,0,1,8,4,2,80,80,540,1"
        )),
        True,
    ),
    "money": (
        _ASS_BASE.format(style_line=(
            "Style: Reels,Arial Black,92,&H0046C300,&H0000FFFF,"
            "&H00FFFFFF,&H80000000,1,0,1,8,3,2,80,80,560,1"
        )),
        True,
    ),
}
DEFAULT_STYLE = "clean"

# Presets de color grading. Cada uno es un nodo eq (+ opcional curves).
# Aplica DESPUES del zoompan, ANTES de quemar subs.
GRADE_PRESETS: dict[str, str] = {
    "none":      "eq=saturation=1.08:contrast=1.04",
    "warm":      "eq=gamma_r=1.07:gamma_b=0.95:saturation=1.10:contrast=1.05",
    "cold":      "eq=gamma_r=0.95:gamma_b=1.08:saturation=1.08:contrast=1.05",
    "cinematic": "eq=saturation=0.90:contrast=1.10:gamma=0.96,curves=preset=increase_contrast",
    "vivid":     "eq=saturation=1.30:contrast=1.10:gamma=0.97",
}
DEFAULT_GRADE = "none"

# Stopwords minimos en castellano (para hashtags)
SPANISH_STOPWORDS = set((
    "el la los las un una unos unas y o pero que de del en al a por para con sin "
    "sobre desde hacia hasta entre como mas menos donde cuando quien quienes cual "
    "cuales todo todos toda todas otro otros otra otras este esta estos estas ese "
    "esa esos esas aquel aquella aquellos aquellas mi tu su nuestro nuestra "
    "vuestro vuestra mis tus sus se me te lo le les nos os ya muy mucho mucha "
    "muchos muchas hay hace hacer hizo eso esto aqui ahi alli yo el ella nosotros "
    "vosotros ellos ellas si no algo nada bueno buena malo mala gran grande "
    "pequeno pequena tambien solo sola algun alguna algunos algunas tan tanto "
    "tanta cada asi pues entonces porque cuando bien sino aunque mientras siempre "
    "nunca casi quiza tal vez bastante demasiado hoy ayer manana tarde temprano "
    "antes despues luego ahora mismo seria sera fue fueron son eran estoy estas "
    "esta estamos estais estan era era eras eramos erais eran tiene tienes tengo "
    "tenemos tenia tenian aun aun haber habia habian habra hemos han hayan haya "
    "decir digo dice dicho hacer hacia hechos cosa cosas vez veces parte partes "
    "tiempo dia dias ano anos vamos vais voy va van"
).split())

ENGLISH_STOPWORDS = set((
    "the a an and or but if of in on for with to from at by as is are was were "
    "be been being have has had do does did this that these those it its they "
    "them their there then so not no yes you your we our i me my mine he she "
    "his her him hers what which who whom when where why how very just only "
    "more most less than too also still even one two three some any all"
).split())


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
                            t_start: float, t_end: float, n_extra: int,
                            min_clip_len: float = 12.0) -> list[dict]:
    """
    Rellena con clips ubicados en los HUECOS mas grandes entre clips existentes,
    SOLO dentro del rango [t_start, t_end]. Cada nuevo clip se alinea al primer
    y ultimo segmento que cabe en su hueco.
    """
    if n_extra <= 0:
        return []
    sorted_existing = sorted(existing, key=lambda c: c["start"])
    gaps: list[tuple[float, float]] = []
    cursor = t_start
    for c in sorted_existing:
        if c["start"] > cursor:
            gaps.append((cursor, c["start"]))
        cursor = c["end"]
    if cursor < t_end:
        gaps.append((cursor, t_end))

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


def equal_cuts(all_segs: list, t_start: float, t_end: float, n: int) -> list[dict]:
    """
    Trocea el rango [t_start, t_end] en N partes iguales pero ALINEA cada
    corte al final de segmento mas cercano (smart cuts).
    """
    duration = t_end - t_start
    if not all_segs:
        return [{"start": t_start + i * (duration / n),
                 "end": t_start + (i + 1) * (duration / n),
                 "score": 0, "words": 0} for i in range(n)]

    target_len = duration / n
    seg_ends = [s[1] for s in all_segs]
    seg_starts = [s[0] for s in all_segs]

    cuts = [t_start]
    for i in range(1, n):
        target = t_start + i * target_len
        # Busca el final de segmento mas cercano a target
        best = min(seg_ends, key=lambda e: abs(e - target))
        cuts.append(best)
    cuts.append(t_end)

    out = []
    for i in range(n):
        s = cuts[i]
        if i > 0:
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


def write_chunk_ass(words: list, ass_path: Path, words_per_chunk: int = 3,
                    style: str = DEFAULT_STYLE, with_hook: bool = True,
                    outro_text: str = "", outro_duration: float = 1.5,
                    clip_len: float = 0.0) -> None:
    """Escribe ASS con chunks de N palabras animados (pop-in scale).
    - with_hook=True: overlay grande arriba con primeras 3 palabras (1.2s).
    - outro_text!='': overlay brandeado en los ultimos outro_duration segundos.
      Si outro_text contiene '\\n' se convierte en salto de linea ASS."""
    header, force_caps = STYLES.get(style, STYLES[DEFAULT_STYLE])
    if not words:
        ass_path.write_text(header, encoding="utf-8")
        return

    lines = []

    # ===== HOOK OVERLAY (primeras palabras grandes arriba) =====
    if with_hook and len(words) >= 1:
        hook_words = words[:3]
        hook_text = " ".join(w["word"] for w in hook_words).upper()
        for ch in [",", ".", "?", "!", ":", ";", "\"", "(", ")"]:
            hook_text = hook_text.replace(ch, "")
        hook_text = hook_text.strip()
        if hook_text:
            hook_end = max(1.2, hook_words[-1]["end"] + 0.2)
            # Hook auto-contenido (no depende del estilo principal):
            # Impact 130, amarillo neon, contorno negro grueso, top-center
            hook_fx = (
                r"{\fnImpact\fs130\an8\pos(540,200)"
                r"\1c&H0000FFFF&\3c&H00000000&\bord10\shad4"
                r"\fad(120,250)\fscx135\fscy135"
                r"\t(0,400,\fscx100\fscy100)}"
            ) + hook_text
            lines.append(
                f"Dialogue: 1,{fmt_ass_time(0.0)},{fmt_ass_time(hook_end)},"
                f"Reels,,0,0,0,,{hook_fx}"
            )

    # ===== CHUNKS NORMALES =====
    i = 0
    while i < len(words):
        chunk = words[i:i + words_per_chunk]
        if not chunk:
            break
        text = " ".join(w["word"] for w in chunk)
        if force_caps:
            text = text.upper()
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

    # ===== OUTRO CARD (ultimos N segundos, branded) =====
    if outro_text and clip_len > outro_duration:
        outro_start = clip_len - outro_duration
        # Convierte saltos de linea de usuario (\n) a sintaxis ASS (\N)
        ass_text = outro_text.replace("\\n", r"\N").replace("\n", r"\N")
        # Sanea caracteres que rompen el .ass
        for ch in ["{", "}"]:
            ass_text = ass_text.replace(ch, "")
        # Estilo outro: centrado en pantalla, blanco con caja semi-transparente.
        # \an5 = centro absoluto. \fad fade in/out. Pop-in scale.
        outro_fx = (
            r"{\fnArial Black\fs78\an5\pos(540,960)"
            r"\1c&H00FFFFFF&\3c&H00000000&\bord6\shad3"
            r"\fad(250,300)\fscx105\fscy105"
            r"\t(0,300,\fscx100\fscy100)}"
        ) + ass_text
        lines.append(
            f"Dialogue: 2,{fmt_ass_time(outro_start)},{fmt_ass_time(clip_len)},"
            f"Reels,,0,0,0,,{outro_fx}"
        )

    ass_path.write_text(header + "\n".join(lines), encoding="utf-8")


# ===== HASHTAGS HEURISTICOS =====

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def generate_hashtags(text: str, language: str = "es", max_n: int = 8) -> list[str]:
    """Hashtags a partir del texto. Heuristica: top palabras 4+ caracteres
    no-stopword, sin acentos, en minusculas."""
    if not text:
        return []
    stops = ENGLISH_STOPWORDS if language.startswith("en") else SPANISH_STOPWORDS
    words = re.findall(r"[a-zA-ZáéíóúñÁÉÍÓÚÑüÜ]{4,}", text)
    words = [w.lower() for w in words]
    words = [w for w in words if w not in stops]
    if not words:
        return []
    counts = Counter(words)
    tops = [w for w, _ in counts.most_common(max_n)]
    return ["#" + _strip_accents(w) for w in tops]


# ===== KEN BURNS + FADES + ENCODING (ITER A) =====

def build_video_filter(clip_len: float, ass_arg: str,
                       grade: str = DEFAULT_GRADE) -> str:
    fade_out_st = max(0.0, clip_len - FADE)
    total_frames = max(1, int(clip_len * FPS))
    zoom_max = 1 + BURNS_ZOOM
    # zoom progresivo 1.0 -> 1+BURNS_ZOOM repartido en total_frames
    zoom_expr = f"min(1+{BURNS_ZOOM}*on/{total_frames}\\,{zoom_max})"
    grade_filter = GRADE_PRESETS.get(grade, GRADE_PRESETS[DEFAULT_GRADE])
    return (
        f"fps={FPS},"
        f"crop=ih*9/16:ih,"
        f"scale=1080:1920:flags=lanczos,"
        f"zoompan=z='{zoom_expr}':d=1:s=1080x1920:fps={FPS}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
        f"{grade_filter},"
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


def build_audio_with_music_complex(clip_len: float, music_volume: float = 0.18) -> str:
    """Filter complex que mezcla voz (input 0) con musica (input 1) a bajo volumen.
    Aplica fades a ambos. Devuelve un filter_complex que sale en [aout]."""
    fade_out_st = max(0.0, clip_len - FADE)
    return (
        f"[1:a]volume={music_volume},"
        f"afade=t=in:st=0:d={FADE},"
        f"afade=t=out:st={fade_out_st}:d={FADE}[m];"
        f"[0:a]afade=t=in:st=0:d={FADE},"
        f"afade=t=out:st={fade_out_st}:d={FADE}[v];"
        f"[v][m]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )


# ===== MAIN =====

def main(video_path: str, n_clips: int, mode: str, target_dur: float,
         words_chunk: int, style: str = DEFAULT_STYLE,
         skip_start: float = 0.0, skip_end: float = 0.0,
         music: str | None = None, with_hook: bool = True,
         music_volume: float = 0.18, grade: str = DEFAULT_GRADE,
         outro_text: str = "", outro_duration: float = 1.5) -> None:
    video = Path(video_path)
    if not video.exists():
        print(f"[ERROR] No existe: {video}")
        sys.exit(1)
    if style not in STYLES:
        print(f"[ERROR] Estilo invalido '{style}'. Validos: {list(STYLES)}")
        sys.exit(1)
    if grade not in GRADE_PRESETS:
        print(f"[ERROR] Grade invalido '{grade}'. Validos: {list(GRADE_PRESETS)}")
        sys.exit(1)

    music_path: str | None = None
    if music:
        mp = Path(music)
        if mp.exists():
            music_path = str(mp.resolve())
            print(f"[..] Musica: {mp.name} (vol {music_volume})")
        else:
            print(f"[!!] Musica no encontrada: {mp}, render sin musica")

    out_dir = Path("output") / f"{video.stem}_pro"
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = get_duration(video)
    usable_start = max(0.0, skip_start)
    usable_end = max(usable_start + 1.0, duration - max(0.0, skip_end))
    skip_info = ""
    if skip_start > 0 or skip_end > 0:
        skip_info = f" | usable: {usable_start:.0f}s -> {usable_end:.0f}s"
    print(f"[..] Video: {duration:.1f}s | modo: {mode} | "
          f"target: {target_dur:.0f}s | chunk: {words_chunk} pal | "
          f"estilo: {style}{skip_info}")

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

    # Filtra segmentos al rango utilizable (skip intro/outro)
    selection_segs = [(s, e, t) for s, e, t in all_segs
                      if s >= usable_start - 0.5 and e <= usable_end + 0.5]
    if len(selection_segs) < len(all_segs):
        print(f"[..] Tras skip: {len(selection_segs)} segmentos en rango usable")

    # Selecciona los clips
    if mode == "equal":
        clips = equal_cuts(selection_segs, usable_start, usable_end, n_clips)
        print(f"[..] Modo equal: {n_clips} cortes alineados a fin de frase")
    else:
        clips = find_highlights(selection_segs, target_dur, n_clips)
        print(f"[..] Modo smart: {len(clips)} highlights detectados")
        if len(clips) < n_clips:
            extra_n = n_clips - len(clips)
            print(f"[..] Faltan {extra_n}, los relleno con clips de los huecos")
            extra = fill_gaps_with_segments(
                clips, selection_segs, usable_start, usable_end, extra_n
            )
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
            write_chunk_ass(
                clip_words, ass_path, words_chunk, style, with_hook,
                outro_text=outro_text, outro_duration=outro_duration,
                clip_len=clip_len,
            )

            out_path = out_dir / f"reel_{clip_id:02d}.mp4"
            ass_arg = ffmpeg_path_for_filter(ass_path)

            vf = build_video_filter(clip_len, ass_arg, grade)

            if music_path:
                # Pipeline con musica: dos inputs, filter_complex
                ac = build_audio_with_music_complex(clip_len, music_volume)
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(t0), "-i", str(video),
                    "-stream_loop", "-1", "-i", music_path,
                    "-t", str(clip_len),
                    "-filter_complex", f"[0:v]{vf}[vout];{ac}",
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
                feats = "KenBurns + hook + chunks + musica + fades"
            else:
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
                feats = "KenBurns + hook + chunks + lanczos + color + fades"
            print(f"[..] Render: {feats}")
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

            # Info + transcripcion + hashtags (texto natural de Whisper)
            info_path = out_dir / f"reel_{clip_id:02d}.txt"
            clip_text = " ".join(
                t.strip() for s, e, t in all_segs
                if s >= t0 - 0.5 and e <= t1 + 0.5
            )
            tags = generate_hashtags(clip_text, info.language, max_n=8)
            tags_line = " ".join(tags) if tags else "(sin hashtags sugeridos)"
            info_path.write_text(
                f"REEL {clip_id} -- {clip_len:.0f}s -- estilo: {style}\n"
                f"Origen: {video.name}\n"
                f"Tiempo en original: {t0:.1f}s -> {t1:.1f}s\n"
                f"Score smart: {clip.get('score', 0):.2f} | "
                f"palabras: {clip.get('words', 0)}\n"
                f"\n"
                f"TRANSCRIPCION (copia-pega para descripcion):\n"
                f"{clip_text}\n"
                f"\n"
                f"HASHTAGS SUGERIDOS (basados en el contenido):\n"
                f"{tags_line}\n",
                encoding="utf-8",
            )
            print(f"     + thumbnail .jpg + transcripcion .txt + {len(tags)} hashtags")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n[OK] {len(clips)} reels listos en {out_dir}")


def parse_args(argv: list):
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
    style = DEFAULT_STYLE
    if "--style" in args:
        idx = args.index("--style")
        style = args[idx + 1]
        del args[idx:idx + 2]
    skip_start = 0.0
    if "--skip-start" in args:
        idx = args.index("--skip-start")
        skip_start = float(args[idx + 1])
        del args[idx:idx + 2]
    skip_end = 0.0
    if "--skip-end" in args:
        idx = args.index("--skip-end")
        skip_end = float(args[idx + 1])
        del args[idx:idx + 2]
    music = None
    if "--music" in args:
        idx = args.index("--music")
        music = args[idx + 1]
        del args[idx:idx + 2]
    music_volume = 0.18
    if "--music-vol" in args:
        idx = args.index("--music-vol")
        music_volume = float(args[idx + 1])
        del args[idx:idx + 2]
    with_hook = True
    if "--no-hook" in args:
        with_hook = False
        args.remove("--no-hook")
    grade = DEFAULT_GRADE
    if "--grade" in args:
        idx = args.index("--grade")
        grade = args[idx + 1]
        del args[idx:idx + 2]
    outro_text = ""
    if "--outro" in args:
        idx = args.index("--outro")
        outro_text = args[idx + 1]
        del args[idx:idx + 2]
    outro_duration = 1.5
    if "--outro-duration" in args:
        idx = args.index("--outro-duration")
        outro_duration = float(args[idx + 1])
        del args[idx:idx + 2]
    if len(args) < 2:
        return None
    return (args[0], int(args[1]), mode, target, chunk, style,
            skip_start, skip_end, music, with_hook, music_volume,
            grade, outro_text, outro_duration)


if __name__ == "__main__":
    parsed = parse_args(sys.argv[1:])
    if not parsed:
        print(__doc__)
        sys.exit(1)
    (vp, n, mode, td, ch, st, ss, se, mu, hk, mv,
     gr, ot, od) = parsed
    main(vp, n, mode, td, ch, st, ss, se,
         music=mu, with_hook=hk, music_volume=mv,
         grade=gr, outro_text=ot, outro_duration=od)
