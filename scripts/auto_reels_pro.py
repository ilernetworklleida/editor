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
import os
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


def generate_copy_for_clips(clips: list[dict], all_segs: list,
                            language: str = "es") -> list[dict] | None:
    """Genera title/description/first_comment/hashtags para cada clip via Claude.
    Devuelve None si falla. Una sola llamada API para los N clips."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not clips:
        return None
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        return None

    clip_blocks = []
    for i, c in enumerate(clips, 1):
        t0 = c.get("start", 0)
        t1 = c.get("end", 0)
        text = " ".join(
            t.strip() for s, e, t in all_segs
            if s >= t0 - 0.5 and e <= t1 + 0.5
        )
        clip_blocks.append(f"--- CLIP {i} ({t1-t0:.0f}s) ---\n{text}")
    transcripts = "\n\n".join(clip_blocks)

    system = (
        "Eres copywriter senior de social media short-form video "
        "(TikTok / Reels / Shorts). Generas copy de alta conversion para "
        "cada clip.\n\n"
        "Para CADA clip me das:\n"
        "- title: gancho corto 40-80 chars (no clickbait barato; "
        "  promete algo que el clip cumple)\n"
        "- description: 80-150 chars; puede llevar 1-2 emojis si encajan\n"
        "- first_comment: pregunta concreta o llamada a interaccion "
        "  (30-100 chars); pensada para que la gente responda\n"
        "- hashtags: 5-8 hashtags (mezcla de nicho + 1-2 genericos del "
        "  idioma); sin '#' al inicio (yo lo anado)\n\n"
        "REGLAS:\n"
        "- Mismo idioma del clip\n"
        "- Si un clip es seco o poco viralizable, di title='' y "
        "  description='' (mejor honesto que inventar)\n"
        "- No uses palabras prohibidas en plataformas (sex, kill, etc.)"
    )
    user_msg = (
        f"Idioma: {language}\n"
        f"Generame copy para estos {len(clips)} clips:\n\n{transcripts}"
    )

    class CopyItem(BaseModel):
        clip_id: int
        title: str
        description: str
        first_comment: str
        hashtags: list[str]

    class CopyBatch(BaseModel):
        clips: list[CopyItem]

    model_id = os.environ.get("HIGHLIGHTS_MODEL", "claude-opus-4-7").strip()
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.parse(
            model=model_id,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            output_format=CopyBatch,
        )
        usage = response.usage
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        print(f"[..] Copy IA: {len(response.parsed_output.clips)} clips")
        print(f"     tokens: in={in_tok} out={out_tok} "
              f"cache_write={cw} cache_read={cr}")
        return [c.dict() for c in response.parsed_output.clips]
    except Exception as e:
        print(f"[!!] generate_copy fallo: {e}")
        return None


def find_highlights_ai(all_segs: list, target_dur: float, n: int,
                       language: str = "es",
                       instructions: str = "") -> list[dict] | None:
    """
    Pide a Claude que seleccione los N momentos mas virales del transcript.
    Si `instructions` no esta vacio, esas instrucciones se usan como criterio
    PRIORITARIO de seleccion (el usuario te dice que tipo de clips quiere).

    Devuelve None si:
      - falta ANTHROPIC_API_KEY
      - falta el SDK anthropic
      - la API falla o devuelve algo invalido
    El caller hace fallback a la heuristica.

    Usa adaptive thinking, prompt caching (transcript = bloque cacheable
    por video) y structured outputs (Pydantic) para garantizar JSON valido.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not all_segs:
        return None
    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        print("[!!] anthropic/pydantic no instalado, fallback a heuristica")
        return None

    transcript_lines = [
        f"[{s:.1f}-{e:.1f}] {t.strip()}" for s, e, t in all_segs
    ]
    transcript = "\n".join(transcript_lines)

    system_prompt = (
        "Eres un editor profesional de video viral para TikTok/Reels/Shorts. "
        "Recibes un transcript con timestamps y eliges los N momentos MAS "
        "ENGANCHANTES para convertir en clips verticales cortos.\n\n"
        "Que hace un clip viral:\n"
        "- Hook fuerte en los primeros 3 segundos (preguntas, datos, "
        "afirmaciones controversiales, cifras, sorpresas)\n"
        "- Auto-contenido: se entiende sin contexto previo ni posterior\n"
        "- Emocional, inspirador o con insight claro\n"
        "- Conclusion / punchline reconocible\n"
        "- Quotable: que provoque ganas de compartir o capturar pantalla\n"
        "- EVITA intros, outros, transiciones entre temas, charla relleno\n\n"
        "Reglas estrictas para los timestamps:\n"
        "- start = inicio EXACTO de un segmento del transcript\n"
        "- end = fin EXACTO de un segmento del transcript\n"
        "- duracion entre 60% y 130% de la duracion objetivo\n"
        "- los clips NO se solapan entre si\n"
        "- ordena por start ascendente"
    )

    user_extras = ""
    if instructions.strip():
        user_extras = (
            f"\nINSTRUCCIONES ESPECIFICAS DEL USUARIO (PRIORIDAD MAXIMA, "
            f"interpretalas literalmente):\n{instructions.strip()}\n"
        )
    user_msg = (
        f"Idioma del video: {language}\n"
        f"Duracion objetivo por clip: {target_dur:.0f} segundos\n"
        f"Numero de clips a elegir: {n}\n"
        f"{user_extras}\n"
        f"Transcript (timestamps en segundos):\n\n{transcript}\n\n"
        f"Elige los {n} MEJORES clips siguiendo los criterios virales y, "
        f"si hay instrucciones del usuario, priorizandolas. Razona "
        f"brevemente cada eleccion (en el mismo idioma del video)."
    )

    class Clip(BaseModel):
        start: float
        end: float
        reason: str

    class Highlights(BaseModel):
        clips: list[Clip]

    model_id = os.environ.get("HIGHLIGHTS_MODEL", "claude-opus-4-7").strip()

    try:
        client = anthropic.Anthropic(api_key=api_key)
        # Top-level cache_control auto-cachea el ultimo bloque cacheable.
        # En este caso cachea system+transcript juntos -> reruns para el
        # mismo video con distinto N van a coste ~10x menor.
        response = client.messages.parse(
            model=model_id,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
            output_format=Highlights,
        )
    except Exception as e:
        print(f"[!!] Claude API fallo: {e}; fallback a heuristica")
        return None

    parsed = response.parsed_output
    if not parsed or not parsed.clips:
        return None

    # Logging util
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    print(f"[..] Claude {model_id}: {len(parsed.clips)} clips elegidos")
    print(f"     tokens: in={in_tok} out={out_tok} "
          f"cache_write={cache_write} cache_read={cache_read}")

    # Snap timestamps a inicios/finales de segmento reales (defensivo)
    seg_starts = sorted({s for s, _, _ in all_segs})
    seg_ends = sorted({e for _, e, _ in all_segs})
    out: list[dict] = []
    for c in parsed.clips:
        s = min(seg_starts, key=lambda x: abs(x - c.start))
        e = min(seg_ends, key=lambda x: abs(x - c.end))
        if e - s < 5.0:  # demasiado corto, skip
            continue
        # Cuenta palabras dentro del rango
        words = sum(
            len(t.split()) for ss, ee, t in all_segs
            if ss >= s - 0.5 and ee <= e + 0.5
        )
        out.append({
            "start": s, "end": e, "score": 0.0,
            "words": words, "reason": c.reason,
        })
    out.sort(key=lambda x: x["start"])
    return out if out else None


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
                    clip_len: float = 0.0, karaoke: bool = False) -> None:
    """Escribe ASS con chunks de N palabras animados (pop-in scale).
    - with_hook=True: overlay grande arriba con primeras 3 palabras (1.2s).
    - outro_text!='': overlay brandeado en los ultimos outro_duration segundos.
      Si outro_text contiene '\\n' se convierte en salto de linea ASS.
    - karaoke=True: en vez de mostrar 1 chunk por bloque, renderiza N
      eventos donde cada palabra se ilumina (estilo TikTok karaoke viral)."""
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

    # ===== CHUNKS NORMALES o KARAOKE =====
    def _clean(t: str) -> str:
        for ch in [",", ".", "?", "!", ":", ";", "\"", "(", ")"]:
            t = t.replace(ch, "")
        return t.strip()

    i = 0
    while i < len(words):
        chunk = words[i:i + words_per_chunk]
        if not chunk:
            break
        words_clean = [_clean(w["word"]) for w in chunk]
        if force_caps:
            words_clean = [w.upper() for w in words_clean]
        words_clean = [w for w in words_clean if w]
        if not words_clean:
            i += words_per_chunk
            continue

        if karaoke and len(chunk) >= 2:
            # Un evento por palabra: la palabra "actual" se pinta amarillo,
            # las demas siguen blancas (color del estilo). Estilo TikTok.
            for k, w_obj in enumerate(chunk):
                if not words_clean[k] if k < len(words_clean) else True:
                    continue
                segs_text = []
                for j, ws in enumerate(words_clean):
                    if j == k:
                        # Amarillo BGR &H0000FFFF& con leve scale up
                        segs_text.append(
                            r"{\1c&H0000FFFF&\fscx108\fscy108}" + ws +
                            r"{\1c&H00FFFFFF&\fscx100\fscy100}"
                        )
                    else:
                        segs_text.append(ws)
                line = " ".join(segs_text)
                start = w_obj["start"]
                end = w_obj["end"]
                lines.append(
                    f"Dialogue: 0,{fmt_ass_time(start)},{fmt_ass_time(end)},"
                    f"Reels,,0,0,0,,{line}"
                )
        else:
            text = " ".join(words_clean)
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


def build_audio_filter(clip_len: float, normalize: bool = True) -> str:
    fade_out_st = max(0.0, clip_len - FADE)
    parts = []
    if normalize:
        # loudnorm single-pass: -16 LUFS integrated, -1.5 dB true peak.
        # Asegura que TODOS los reels suenan al mismo volumen consistente.
        parts.append("loudnorm=I=-16:LRA=11:TP=-1.5")
    parts.append(f"afade=t=in:st=0:d={FADE}")
    parts.append(f"afade=t=out:st={fade_out_st}:d={FADE}")
    return ",".join(parts)


def build_audio_with_music_complex(clip_len: float, music_volume: float = 0.18,
                                   ducking: bool = False) -> str:
    """Filter complex que mezcla voz (input 0) con musica (input 1).
    - Si ducking=False: mezcla simple, musica a volumen fijo bajo.
    - Si ducking=True: la voz dispara un sidechain compressor que baja
      la musica cuando hay voz y la sube cuando no (ducking profesional)."""
    fade_out_st = max(0.0, clip_len - FADE)
    if ducking:
        # La musica empieza a volumen mas alto para que el ducking note diferencia.
        boosted = min(1.0, music_volume * 2.5)
        return (
            f"[1:a]volume={boosted}[m_pre];"
            f"[0:a]asplit=2[v0][v_trig];"
            f"[m_pre][v_trig]sidechaincompress=threshold=0.04:ratio=12:"
            f"attack=10:release=350[m_duck];"
            f"[m_duck]afade=t=in:st=0:d={FADE},"
            f"afade=t=out:st={fade_out_st}:d={FADE}[m];"
            f"[v0]afade=t=in:st=0:d={FADE},"
            f"afade=t=out:st={fade_out_st}:d={FADE}[v];"
            f"[v][m]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
    return (
        f"[1:a]volume={music_volume},"
        f"afade=t=in:st=0:d={FADE},"
        f"afade=t=out:st={fade_out_st}:d={FADE}[m];"
        f"[0:a]afade=t=in:st=0:d={FADE},"
        f"afade=t=out:st={fade_out_st}:d={FADE}[v];"
        f"[v][m]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )


# ===== MAIN =====

WATERMARK_POSITIONS = {
    "br": "x=W-w-30:y=H-h-30",  # bottom right
    "bl": "x=30:y=H-h-30",       # bottom left
    "tr": "x=W-w-30:y=30",       # top right
    "tl": "x=30:y=30",           # top left
}


def srt_time(s: float) -> str:
    """Convierte segundos a formato SRT: HH:MM:SS,mmm"""
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def write_srt_for_range(segs: list, t0: float, t1: float, srt_path: Path) -> None:
    """Filtra segmentos al rango [t0,t1] y escribe un .srt con tiempos relativos."""
    lines: list[str] = []
    idx = 1
    for s, e, t in segs:
        if e <= t0 or s >= t1:
            continue
        rel_s = max(0.0, s - t0)
        rel_e = min(t1 - t0, e - t0)
        if rel_e <= rel_s:
            continue
        lines.append(str(idx))
        lines.append(f"{srt_time(rel_s)} --> {srt_time(rel_e)}")
        lines.append(t.strip())
        lines.append("")
        idx += 1
    srt_path.write_text("\n".join(lines), encoding="utf-8")


def main(video_path: str, n_clips: int, mode: str, target_dur: float,
         words_chunk: int, style: str = DEFAULT_STYLE,
         skip_start: float = 0.0, skip_end: float = 0.0,
         music: str | None = None, with_hook: bool = True,
         music_volume: float = 0.18, grade: str = DEFAULT_GRADE,
         outro_text: str = "", outro_duration: float = 1.5,
         ducking: bool = False,
         watermark: str | None = None,
         watermark_pos: str = "br",
         watermark_scale: float = 12.0,
         translate_en: bool = False,
         ai_highlights: bool = False,
         out_suffix: str = "",
         instructions: str = "",
         normalize_audio: bool = True,
         karaoke: bool = False,
         generate_copy: bool = False) -> None:
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

    watermark_path: str | None = None
    if watermark:
        wp = Path(watermark)
        if wp.exists():
            watermark_path = str(wp.resolve())
            print(f"[..] Watermark: {wp.name} pos={watermark_pos} "
                  f"scale={watermark_scale}%")
        else:
            print(f"[!!] Watermark no encontrado: {wp}, render sin")
    if watermark_pos not in WATERMARK_POSITIONS:
        print(f"[!!] Posicion watermark invalida '{watermark_pos}', uso 'br'")
        watermark_pos = "br"

    out_dir = Path("output") / f"{video.stem}_pro{out_suffix}"
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

    # Smart chunking: si words_chunk == 0 (sentinel "auto"), calcula segun
    # velocidad de habla. Hablantes rapidos -> 2 palabras (cadencia visual
    # alta). Hablantes lentos -> 4 palabras. Estandar -> 3.
    if words_chunk == 0:
        if all_words and len(all_words) > 10:
            speak_dur = max(1.0, all_words[-1]["end"] - all_words[0]["start"])
            wps = len(all_words) / speak_dur
            if wps > 2.5:
                words_chunk = 2
            elif wps < 1.5:
                words_chunk = 4
            else:
                words_chunk = 3
            print(f"[..] Auto-chunk: {wps:.2f} palabras/seg -> "
                  f"{words_chunk} palabras/bocadillo")
        else:
            words_chunk = WORDS_PER_CHUNK

    en_segs: list = []
    if translate_en and info.language != "en":
        print(f"[..] Generando transcripcion EN (Whisper task=translate)")
        en_obj, _ = model.transcribe(str(video), beam_size=5, task="translate")
        en_segs = [(s.start, s.end, s.text) for s in en_obj]
        print(f"[..] {len(en_segs)} segmentos EN traducidos")

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
        clips = []
        if ai_highlights:
            mode_label = "con instrucciones" if instructions.strip() else "modo libre"
            print(f"[..] AI highlights (Claude, {mode_label}): pidiendo seleccion...")
            ai_result = find_highlights_ai(
                selection_segs, target_dur, n_clips, info.language,
                instructions=instructions,
            )
            if ai_result:
                clips = ai_result
                print(f"[..] Modo AI: {len(clips)} highlights elegidos por Claude")
            else:
                print(f"[!!] AI fallback (sin API key o fallo); "
                      f"uso heuristica de densidad")
        if not clips:
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
                clip_len=clip_len, karaoke=karaoke,
            )

            # Persiste palabras/segmentos del reel en JSON para que el editor
            # web pueda mostrarlos y exportar .srt/.ass corregidos.
            segs_json_path = out_dir / f"reel_{clip_id:02d}.segs.json"
            seg_data = {
                "clip_id": clip_id,
                "t0": t0, "t1": t1, "clip_len": clip_len,
                "language": info.language,
                "words": clip_words,  # ya con tiempos relativos al clip
                "segments": [
                    {
                        "start": max(0.0, s - t0),
                        "end": min(clip_len, e - t0),
                        "text": t.strip(),
                    }
                    for s, e, t in all_segs
                    if s >= t0 - 0.5 and e <= t1 + 0.5
                ],
            }
            segs_json_path.write_text(
                json.dumps(seg_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            out_path = out_dir / f"reel_{clip_id:02d}.mp4"
            ass_arg = ffmpeg_path_for_filter(ass_path)

            vf = build_video_filter(clip_len, ass_arg, grade)

            # Pipeline simple si NO hay musica NI watermark; si hay alguno,
            # usamos filter_complex con todos los inputs.
            if not music_path and not watermark_path:
                af = build_audio_filter(clip_len, normalize=normalize_audio)
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
            else:
                # Filter_complex: video con vf -> opcional overlay watermark
                # -> output. Audio: con o sin musica.
                cmd = ["ffmpeg", "-y", "-ss", str(t0), "-i", str(video)]
                next_idx = 1
                music_idx = wm_idx = None
                if music_path:
                    cmd += ["-stream_loop", "-1", "-i", music_path]
                    music_idx = next_idx
                    next_idx += 1
                if watermark_path:
                    cmd += ["-loop", "1", "-i", watermark_path]
                    wm_idx = next_idx
                    next_idx += 1
                cmd += ["-t", str(clip_len)]

                parts = [f"[0:v]{vf}[vbase]"]
                cur_v = "vbase"
                if wm_idx is not None:
                    wm_w = max(40, int(1080 * watermark_scale / 100))
                    pos = WATERMARK_POSITIONS[watermark_pos]
                    parts.append(f"[{wm_idx}:v]scale={wm_w}:-1[wm]")
                    parts.append(f"[{cur_v}][wm]overlay={pos}[vout]")
                    cur_v = "vout"

                if music_idx is not None:
                    fade_out_st = max(0.0, clip_len - FADE)
                    norm = "loudnorm=I=-16:LRA=11:TP=-1.5," if normalize_audio else ""
                    if ducking:
                        boosted = min(1.0, music_volume * 2.5)
                        parts.append(
                            f"[{music_idx}:a]volume={boosted}[m_pre];"
                            f"[0:a]{norm}asplit=2[v0][v_trig];"
                            f"[m_pre][v_trig]sidechaincompress="
                            f"threshold=0.04:ratio=12:attack=10:release=350[m_duck];"
                            f"[m_duck]afade=t=in:st=0:d={FADE},"
                            f"afade=t=out:st={fade_out_st}:d={FADE}[m];"
                            f"[v0]afade=t=in:st=0:d={FADE},"
                            f"afade=t=out:st={fade_out_st}:d={FADE}[v];"
                            f"[v][m]amix=inputs=2:duration=first:"
                            f"dropout_transition=0[aout]"
                        )
                    else:
                        parts.append(
                            f"[{music_idx}:a]volume={music_volume},"
                            f"afade=t=in:st=0:d={FADE},"
                            f"afade=t=out:st={fade_out_st}:d={FADE}[m];"
                            f"[0:a]{norm}afade=t=in:st=0:d={FADE},"
                            f"afade=t=out:st={fade_out_st}:d={FADE}[v];"
                            f"[v][m]amix=inputs=2:duration=first:"
                            f"dropout_transition=0[aout]"
                        )
                    final_a = "[aout]"
                else:
                    fade_out_st = max(0.0, clip_len - FADE)
                    norm = "loudnorm=I=-16:LRA=11:TP=-1.5," if normalize_audio else ""
                    parts.append(
                        f"[0:a]{norm}afade=t=in:st=0:d={FADE},"
                        f"afade=t=out:st={fade_out_st}:d={FADE}[aout]"
                    )
                    final_a = "[aout]"

                fc = ";".join(parts)
                cmd += [
                    "-filter_complex", fc,
                    "-map", f"[{cur_v}]", "-map", final_a,
                    "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    str(out_path),
                ]
                bits = ["KenBurns", "hook", "chunks", "color", "fades"]
                if music_path:
                    bits.append("musica" + (" (ducking)" if ducking else ""))
                if watermark_path:
                    bits.append("watermark")
                feats = " + ".join(bits)
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
            ai_reason = clip.get("reason", "").strip()
            ai_block = (
                f"\nMOTIVO IA (Claude): {ai_reason}\n" if ai_reason else ""
            )
            info_path.write_text(
                f"REEL {clip_id} -- {clip_len:.0f}s -- estilo: {style}\n"
                f"Origen: {video.name}\n"
                f"Tiempo en original: {t0:.1f}s -> {t1:.1f}s\n"
                f"Score smart: {clip.get('score', 0):.2f} | "
                f"palabras: {clip.get('words', 0)}\n"
                f"{ai_block}"
                f"\n"
                f"TRANSCRIPCION (copia-pega para descripcion):\n"
                f"{clip_text}\n"
                f"\n"
                f"HASHTAGS SUGERIDOS (basados en el contenido):\n"
                f"{tags_line}\n",
                encoding="utf-8",
            )
            extras_msg = f"thumbnail .jpg + .txt + {len(tags)} hashtags"
            if en_segs:
                en_srt_path = out_dir / f"reel_{clip_id:02d}_en.srt"
                write_srt_for_range(en_segs, t0, t1, en_srt_path)
                extras_msg += " + .srt EN"
            print(f"     + {extras_msg}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Genera copy con IA al final si esta activo (1 sola llamada para los N)
    if generate_copy and clips:
        print(f"\n[..] Generando copy con IA para los {len(clips)} reels...")
        copies = generate_copy_for_clips(clips, all_segs, info.language)
        if copies:
            for c in copies:
                cid = int(c.get("clip_id", 0))
                if cid < 1:
                    continue
                copy_path = out_dir / f"reel_{cid:02d}.copy.json"
                copy_path.write_text(
                    json.dumps(c, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                # Append al .txt para acceso rapido
                txt_path = out_dir / f"reel_{cid:02d}.txt"
                if txt_path.exists():
                    tags_line = " ".join(
                        ("#" + h.lstrip("#") for h in c.get("hashtags", []))
                    )
                    extra = (
                        f"\nCOPY GENERADO CON IA (Claude):\n"
                        f"TITULO: {c.get('title','')}\n"
                        f"DESCRIPCION: {c.get('description','')}\n"
                        f"PRIMER COMENTARIO: {c.get('first_comment','')}\n"
                        f"HASHTAGS IA: {tags_line}\n"
                    )
                    txt_path.write_text(
                        txt_path.read_text(encoding="utf-8") + extra,
                        encoding="utf-8",
                    )
            print(f"[OK] Copy generado y guardado en .copy.json + .txt")
        else:
            print(f"[!!] Copy fallo (sin API key o error)")

    print(f"\n[OK] {len(clips)} reels listos en {out_dir}")


def _load_profile(name: str) -> list:
    """Lee profiles/<name>.json y devuelve la lista de args. Devuelve []
    si no existe o no es valido."""
    profile_path = Path("profiles") / f"{name}.json"
    if not profile_path.exists():
        print(f"[!!] Perfil no encontrado: {profile_path}")
        return []
    try:
        with profile_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            print(f"[..] Perfil cargado: {name} ({len(data) // 2} flags)")
            return [str(x) for x in data]
        if isinstance(data, dict) and "args" in data:
            args_list = [str(x) for x in data["args"]]
            print(f"[..] Perfil cargado: {name} ({len(args_list) // 2} flags)")
            return args_list
        print(f"[!!] Formato de perfil invalido: {profile_path}")
    except Exception as e:
        print(f"[!!] Error leyendo perfil {profile_path}: {e}")
    return []


def parse_args(argv: list):
    args = list(argv)

    # Carga perfil (si --profile esta presente). Los args del perfil van
    # AL FINAL para que los flags pasados por usuario tengan prioridad.
    while "--profile" in args:
        idx = args.index("--profile")
        if idx + 1 < len(args):
            profile_name = args[idx + 1]
            del args[idx:idx + 2]
            args = args + _load_profile(profile_name)
        else:
            del args[idx:idx + 1]

    def extract_first(name, has_value=True):
        """Extrae PRIMERA ocurrencia y elimina TODAS. Devuelve la primera."""
        first = None
        while name in args:
            i = args.index(name)
            if has_value:
                v = args[i + 1] if i + 1 < len(args) else None
                del args[i:i + 2]
            else:
                v = True
                del args[i:i + 1]
            if first is None:
                first = v
        return first

    mode = "equal" if extract_first("--equal", has_value=False) else "smart"
    target = float(extract_first("--duration") or TARGET_CLIP_LEN)
    chunk_raw = extract_first("--chunk")
    if chunk_raw == "auto":
        chunk = 0  # sentinel para auto-chunk basado en WPS
    else:
        chunk = int(chunk_raw or WORDS_PER_CHUNK)
    style = extract_first("--style") or DEFAULT_STYLE
    skip_start = float(extract_first("--skip-start") or 0)
    skip_end = float(extract_first("--skip-end") or 0)
    music = extract_first("--music")
    music_volume = float(extract_first("--music-vol") or 0.18)
    with_hook = not extract_first("--no-hook", has_value=False)
    grade = extract_first("--grade") or DEFAULT_GRADE
    outro_text = extract_first("--outro") or ""
    outro_duration = float(extract_first("--outro-duration") or 1.5)
    ducking = bool(extract_first("--duck", has_value=False))
    watermark = extract_first("--watermark")
    watermark_pos = extract_first("--watermark-pos") or "br"
    watermark_scale = float(extract_first("--watermark-scale") or 12.0)
    translate_en = bool(extract_first("--translate-en", has_value=False))
    ai_highlights = bool(extract_first("--ai-highlights", has_value=False))
    out_suffix = extract_first("--out-suffix") or ""
    instructions = extract_first("--instructions") or ""
    normalize_audio = not extract_first("--no-normalize", has_value=False)
    karaoke = bool(extract_first("--karaoke", has_value=False))
    generate_copy = bool(extract_first("--generate-copy", has_value=False))

    if len(args) < 2:
        return None
    return (args[0], int(args[1]), mode, target, chunk, style,
            skip_start, skip_end, music, with_hook, music_volume,
            grade, outro_text, outro_duration, ducking,
            watermark, watermark_pos, watermark_scale, translate_en,
            ai_highlights, out_suffix, instructions, normalize_audio,
            karaoke, generate_copy)


if __name__ == "__main__":
    parsed = parse_args(sys.argv[1:])
    if not parsed:
        print(__doc__)
        sys.exit(1)
    (vp, n, mode, td, ch, st, ss, se, mu, hk, mv,
     gr, ot, od, dk, wm, wp, ws, te, ai, osfx, ins, nrm, kar, gc) = parsed
    main(vp, n, mode, td, ch, st, ss, se,
         music=mu, with_hook=hk, music_volume=mv,
         grade=gr, outro_text=ot, outro_duration=od,
         ducking=dk, watermark=wm, watermark_pos=wp,
         watermark_scale=ws, translate_en=te,
         ai_highlights=ai, out_suffix=osfx,
         instructions=ins, normalize_audio=nrm,
         karaoke=kar, generate_copy=gc)
