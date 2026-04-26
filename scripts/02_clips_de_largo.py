"""
Divide un video largo en N clips iguales.
Con --vertical recorta el centro a 9:16 (Reels/Shorts/TikTok).

Uso:
    python scripts/02_clips_de_largo.py input/podcast.mp4 5
    python scripts/02_clips_de_largo.py input/podcast.mp4 8 --vertical
"""
import json
import subprocess
import sys
from pathlib import Path


def get_duration(video: Path) -> float:
    """Devuelve la duracion del video en segundos via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def main(video_path: str, n_clips: int, vertical: bool = False) -> None:
    video = Path(video_path)
    if not video.exists():
        print(f"[ERROR] No existe: {video}")
        sys.exit(1)

    output_dir = Path("output") / video.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = get_duration(video)
    clip_len = duration / n_clips
    print(f"[..] Duracion: {duration:.1f}s -> {n_clips} clips de {clip_len:.1f}s")

    for i in range(n_clips):
        start = i * clip_len
        out_path = output_dir / f"{video.stem}_clip{i+1:02d}.mp4"

        cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(video),
               "-t", str(clip_len)]

        if vertical:
            # Recorta el centro a 9:16 desde un video horizontal
            cmd += ["-vf", "crop=ih*9/16:ih"]

        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", str(out_path)]

        print(f"[..] Generando clip {i+1}/{n_clips}")
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"[OK] {out_path.name}")

    print(f"\n[OK] Todos los clips en: {output_dir}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    is_vertical = "--vertical" in sys.argv
    main(sys.argv[1], int(sys.argv[2]), is_vertical)
