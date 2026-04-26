"""
auto_reels.py — El "boton magico". Convierte 1 video largo en N Reels listos.

Pipeline completo en 1 comando:
  1. Transcribe el video entero con Whisper (1 sola vez)
  2. Trocea en N clips iguales
  3. Recorta cada clip a 9:16 (centro) -> 1080x1920
  4. Quema subtitulos estilo Reels (grandes, blancos, sombra negra)
  5. Comprime H.264 + faststart -> listo para subir

Uso:
    python scripts/auto_reels.py input/video.mp4 6

Salida: output/<video>_reels/reel_01.mp4 ... reel_NN.mp4
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
    print("[ERROR] Falta faster-whisper. Instala con: pip install -r requirements.txt")
    sys.exit(1)


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


def write_ass(segments: list[dict], ass_path: Path) -> None:
    """Genera un .ass con estilo Reels (1080x1920, fuente grande, sombra)."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Reels,Arial Black,76,&H00FFFFFF,&H00000000,&H80000000,1,0,1,5,2,2,80,80,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for seg in segments:
        text = seg["text"].strip().replace("\n", " ")
        # Saltos de linea cada ~30 chars para que no se desborde
        if len(text) > 35:
            words = text.split()
            mid = len(words) // 2
            text = " ".join(words[:mid]) + r"\N" + " ".join(words[mid:])
        lines.append(
            f"Dialogue: 0,{fmt_ass_time(seg['start'])},{fmt_ass_time(seg['end'])},"
            f"Reels,,0,0,0,,{text}"
        )
    ass_path.write_text(header + "\n".join(lines), encoding="utf-8")


def ffmpeg_path_for_filter(p: Path) -> str:
    """Convierte path Windows a algo que el filtro 'ass=' acepta."""
    return str(p).replace("\\", "/").replace(":", r"\:")


def main(video_path: str, n_clips: int) -> None:
    video = Path(video_path)
    if not video.exists():
        print(f"[ERROR] No existe: {video}")
        sys.exit(1)

    out_dir = Path("output") / f"{video.stem}_reels"
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = get_duration(video)
    clip_len = duration / n_clips
    print(f"[..] Video: {duration:.1f}s -> {n_clips} reels de {clip_len:.1f}s")

    print(f"[..] Cargando Whisper 'small' y transcribiendo TODO el video")
    print(f"     (la 1a vez baja modelo ~470MB; despues queda en cache)")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(video), beam_size=5)
    all_segs = [(s.start, s.end, s.text) for s in segments]
    print(f"[..] Transcrito: {len(all_segs)} segmentos | idioma: {info.language}")

    if not all_segs:
        print("[ERROR] No se detecto audio/voz")
        sys.exit(1)

    work_dir = Path(tempfile.mkdtemp(prefix="reels_"))
    try:
        for i in range(n_clips):
            t0 = i * clip_len
            t1 = (i + 1) * clip_len
            clip_id = i + 1
            print(f"\n=== REEL {clip_id}/{n_clips}  ({t0:.0f}s -> {t1:.0f}s) ===")

            # Filtrar segmentos en este rango y reajustar a tiempo local del clip
            clip_segs = []
            for s, e, t in all_segs:
                if e <= t0 or s >= t1:
                    continue
                clip_segs.append({
                    "start": max(0.0, s - t0),
                    "end": min(clip_len, e - t0),
                    "text": t,
                })

            if not clip_segs:
                print(f"[!!] Sin voz en este tramo, salto")
                continue

            ass_path = work_dir / f"reel_{clip_id}.ass"
            write_ass(clip_segs, ass_path)

            out_path = out_dir / f"reel_{clip_id:02d}.mp4"
            ass_arg = ffmpeg_path_for_filter(ass_path)

            # Un solo ffmpeg: cortar + 9:16 centro + escalar + quemar subs + render
            vf = f"crop=ih*9/16:ih,scale=1080:1920,ass='{ass_arg}'"
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(t0), "-i", str(video),
                "-t", str(clip_len),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out_path),
            ]
            print(f"[..] Cortando + 9:16 + subs + render")
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"[ERROR] ffmpeg fallo en reel {clip_id}:")
                print(res.stderr[-1500:])
                continue
            print(f"[OK] {out_path.name}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n[OK] Listo. Carpeta de salida: {out_dir}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], int(sys.argv[2]))
