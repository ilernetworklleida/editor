"""
reburn_reel.py — Re-renderiza UN solo reel con subtitulos editados.

Reutiliza la pipeline ffmpeg de auto_reels_pro pero solo para un clip,
sin Whisper. Util tras editar transcripciones desde el web UI.

Uso (lo invoca el web app, no se llama a mano normalmente):
    python scripts/reburn_reel.py \
        --source path/al/video.mp4 \
        --t0 12.5 --t1 45.2 \
        --segs path/al/reel_03.segs.json \
        --output path/al/reel_03.mp4 \
        [--style hype --grade cinematic --music ... etc.]
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_reels_pro import (  # noqa: E402
    FADE,
    WATERMARK_POSITIONS,
    build_audio_filter,
    build_video_filter,
    ffmpeg_path_for_filter,
    write_chunk_ass,
)


def words_from_segments(segments: list) -> list:
    """Distribuye palabras uniformemente dentro de cada segmento editado."""
    out = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        words = text.split()
        if not words:
            continue
        start = float(seg["start"])
        end = float(seg["end"])
        dur = max(0.1, end - start)
        per = dur / len(words)
        for i, w in enumerate(words):
            out.append({
                "start": start + i * per,
                "end": start + (i + 1) * per,
                "word": w,
            })
    return out


def build_cmd(args, ass_arg: str, words: list) -> list:
    """Construye el cmd ffmpeg con la misma logica que auto_reels_pro main()."""
    clip_len = args.t1 - args.t0
    vf = build_video_filter(clip_len, ass_arg, args.grade)
    normalize = not args.no_normalize
    out_path = Path(args.output)

    if not args.music and not args.watermark:
        af = build_audio_filter(clip_len, normalize=normalize)
        return [
            "ffmpeg", "-y",
            "-ss", str(args.t0), "-i", args.source,
            "-t", str(clip_len),
            "-vf", vf, "-af", af,
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]

    cmd = ["ffmpeg", "-y", "-ss", str(args.t0), "-i", args.source]
    next_idx = 1
    music_idx = wm_idx = None
    if args.music:
        cmd += ["-stream_loop", "-1", "-i", args.music]
        music_idx = next_idx
        next_idx += 1
    if args.watermark:
        cmd += ["-loop", "1", "-i", args.watermark]
        wm_idx = next_idx
    cmd += ["-t", str(clip_len)]

    parts = [f"[0:v]{vf}[vbase]"]
    cur_v = "vbase"
    if wm_idx is not None:
        wm_w = max(40, int(1080 * args.watermark_scale / 100))
        pos = WATERMARK_POSITIONS.get(args.watermark_pos, WATERMARK_POSITIONS["br"])
        parts.append(f"[{wm_idx}:v]scale={wm_w}:-1[wm]")
        parts.append(f"[{cur_v}][wm]overlay={pos}[vout]")
        cur_v = "vout"

    fade_out_st = max(0.0, clip_len - FADE)
    norm = "loudnorm=I=-16:LRA=11:TP=-1.5," if normalize else ""
    if music_idx is not None:
        if args.duck:
            boosted = min(1.0, args.music_vol * 2.5)
            parts.append(
                f"[{music_idx}:a]volume={boosted}[m_pre];"
                f"[0:a]{norm}asplit=2[v0][v_trig];"
                f"[m_pre][v_trig]sidechaincompress="
                f"threshold=0.04:ratio=12:attack=10:release=350[m_duck];"
                f"[m_duck]afade=t=in:st=0:d={FADE},"
                f"afade=t=out:st={fade_out_st}:d={FADE}[m];"
                f"[v0]afade=t=in:st=0:d={FADE},"
                f"afade=t=out:st={fade_out_st}:d={FADE}[v];"
                f"[v][m]amix=inputs=2:duration=first[aout]"
            )
        else:
            parts.append(
                f"[{music_idx}:a]volume={args.music_vol},"
                f"afade=t=in:st=0:d={FADE},"
                f"afade=t=out:st={fade_out_st}:d={FADE}[m];"
                f"[0:a]{norm}afade=t=in:st=0:d={FADE},"
                f"afade=t=out:st={fade_out_st}:d={FADE}[v];"
                f"[v][m]amix=inputs=2:duration=first[aout]"
            )
    else:
        parts.append(
            f"[0:a]{norm}afade=t=in:st=0:d={FADE},"
            f"afade=t=out:st={fade_out_st}:d={FADE}[aout]"
        )

    fc = ";".join(parts)
    cmd += [
        "-filter_complex", fc,
        "-map", f"[{cur_v}]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--t0", type=float, required=True)
    ap.add_argument("--t1", type=float, required=True)
    ap.add_argument("--segs", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--style", default="clean")
    ap.add_argument("--grade", default="none")
    ap.add_argument("--chunk", type=int, default=3)
    ap.add_argument("--music", default="")
    ap.add_argument("--music-vol", type=float, default=0.18)
    ap.add_argument("--duck", action="store_true")
    ap.add_argument("--watermark", default="")
    ap.add_argument("--watermark-pos", default="br")
    ap.add_argument("--watermark-scale", type=float, default=12.0)
    ap.add_argument("--outro", default="")
    ap.add_argument("--outro-duration", type=float, default=1.5)
    ap.add_argument("--no-hook", action="store_true")
    ap.add_argument("--no-normalize", action="store_true")
    ap.add_argument("--karaoke", action="store_true")
    args = ap.parse_args()

    seg_data = json.loads(Path(args.segs).read_text(encoding="utf-8"))
    segments = seg_data.get("segments", [])
    if not segments:
        print("[ERROR] No hay segmentos editados en el JSON")
        sys.exit(1)

    words = words_from_segments(segments)
    clip_len = args.t1 - args.t0

    work_dir = Path(tempfile.mkdtemp(prefix="reburn_"))
    try:
        ass_path = work_dir / "reel.ass"
        write_chunk_ass(
            words, ass_path, args.chunk, args.style,
            with_hook=not args.no_hook,
            outro_text=args.outro,
            outro_duration=args.outro_duration,
            clip_len=clip_len,
            karaoke=args.karaoke,
        )
        ass_arg = ffmpeg_path_for_filter(ass_path)
        cmd = build_cmd(args, ass_arg, words)

        print(f"[..] Re-renderizando {Path(args.output).name}...")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[ERROR] ffmpeg fallo (codigo {res.returncode}):")
            print(res.stderr[-2000:])
            sys.exit(1)

        # Re-generar miniatura
        thumb_path = Path(args.output).with_suffix(".jpg")
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(clip_len / 2), "-i", args.output,
            "-frames:v", "1", "-q:v", "2", str(thumb_path),
        ], capture_output=True)

        print(f"[OK] {Path(args.output).name} re-renderizado con subs editados")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
