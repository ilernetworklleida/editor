"""
auto_batch.py — Procesa multiples videos en serie con el mismo pipeline pro.

Acepta una carpeta entera, una lista separada por comas, o un solo video.
Pasa los flags adicionales tal cual a auto_reels_pro.

Uso:
    python scripts/auto_batch.py input/ 4
    python scripts/auto_batch.py input/ 4 --style hype --skip-start 30
    python scripts/auto_batch.py input/ 4 --music music/cancion.mp3 --music-vol 0.15
    python scripts/auto_batch.py "input/v1.mp4,input/v2.mp4" 4 --style money

Salida: cada video procesa en output/<video>_pro/ (igual que auto_reels_pro).
"""
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


def collect_videos(target: str) -> list[Path]:
    """Devuelve la lista de videos a procesar."""
    if "," in target:
        paths = [Path(p.strip()) for p in target.split(",")]
        return [p for p in paths if p.exists() and p.suffix.lower() in VIDEO_EXTS]
    p = Path(target)
    if p.is_dir():
        return sorted(v for v in p.iterdir()
                      if v.is_file() and v.suffix.lower() in VIDEO_EXTS)
    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
        return [p]
    return []


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    target = args[0]
    n_clips = args[1]
    rest = args[2:]

    videos = collect_videos(target)
    if not videos:
        print(f"[ERROR] No se encontro ningun video en: {target}")
        print(f"        Extensiones validas: {sorted(VIDEO_EXTS)}")
        sys.exit(1)

    extras = " ".join(rest) if rest else "(sin flags extra)"
    print(f"[..] {len(videos)} videos a procesar | N={n_clips} | {extras}\n")
    for v in videos:
        size_mb = v.stat().st_size / (1024 * 1024)
        print(f"     - {v.name}  ({size_mb:.1f} MB)")

    here = Path(__file__).resolve().parent
    fails: list[str] = []
    for i, video in enumerate(videos, 1):
        sep = "=" * 60
        print(f"\n{sep}\n=== {i}/{len(videos)}: {video.name} ===\n{sep}")
        cmd = [
            sys.executable,
            str(here / "auto_reels_pro.py"),
            str(video),
            n_clips,
        ] + rest
        res = subprocess.run(cmd)
        if res.returncode != 0:
            fails.append(video.name)
            print(f"[!!] {video.name} fallo (codigo {res.returncode})")

    print(f"\n{'=' * 60}")
    print(f"[OK] Batch terminado: {len(videos) - len(fails)}/{len(videos)} OK")
    if fails:
        print(f"[!!] Fallaron: {', '.join(fails)}")
    sys.exit(len(fails))


if __name__ == "__main__":
    main()
