"""
auto_montage.py — Crea un montaje/teaser de los reels generados.

Toma una carpeta de reels (output/<X>_pro/) y construye un teaser
seleccionando los primeros M segundos de cada reel con transiciones
crossfade. Util para colgar como "aviso" en el feed o como story que
manda trafico a los reels individuales.

Uso:
    python scripts/auto_montage.py output/test_es_pro
    python scripts/auto_montage.py output/test_es_pro --per-clip 6
    python scripts/auto_montage.py output/test_es_pro --per-clip 5 --xfade 0.5

Salida: output/<carpeta>_montage.mp4
"""
import re
import subprocess
import sys
from pathlib import Path

PER_CLIP = 6.0   # segundos a tomar de cada reel
XFADE = 0.4      # duracion del crossfade en segundos
REEL_RE = re.compile(r"reel_\d+\.mp4$")


def collect_reels(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and REEL_RE.search(p.name))


def build_filter_complex(n: int, per_clip: float, xfade: float) -> str:
    """Construye el filter_complex con N reels, trim + crossfade encadenado."""
    parts = []
    # Trim cada input a [v_i]/[a_i]
    for i in range(n):
        parts.append(f"[{i}:v]trim=0:{per_clip},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[{i}:a]atrim=0:{per_clip},asetpts=PTS-STARTPTS[a{i}]")

    # Cadena de crossfades video + audio
    prev_v, prev_a = "v0", "a0"
    for i in range(1, n):
        offset = i * (per_clip - xfade)
        is_last = (i == n - 1)
        out_v = "vout" if is_last else f"vc{i}"
        out_a = "aout" if is_last else f"ac{i}"
        parts.append(
            f"[{prev_v}][v{i}]xfade=transition=fade:duration={xfade}:"
            f"offset={offset}[{out_v}]"
        )
        parts.append(f"[{prev_a}][a{i}]acrossfade=d={xfade}[{out_a}]")
        prev_v, prev_a = out_v, out_a

    return ";".join(parts)


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 1:
        print(__doc__)
        sys.exit(1)

    folder = Path(args[0])
    if not folder.is_dir():
        print(f"[ERROR] No existe la carpeta: {folder}")
        sys.exit(1)

    per_clip = PER_CLIP
    xfade = XFADE
    rest = args[1:]
    if "--per-clip" in rest:
        idx = rest.index("--per-clip")
        per_clip = float(rest[idx + 1])
    if "--xfade" in rest:
        idx = rest.index("--xfade")
        xfade = float(rest[idx + 1])

    if xfade >= per_clip:
        print(f"[ERROR] xfade ({xfade}) debe ser < per-clip ({per_clip})")
        sys.exit(1)

    reels = collect_reels(folder)
    if len(reels) < 2:
        print(f"[ERROR] Necesito >=2 reels en {folder}, encontre {len(reels)}")
        sys.exit(1)

    total_dur = len(reels) * per_clip - (len(reels) - 1) * xfade
    print(f"[..] {len(reels)} reels detectados:")
    for r in reels:
        print(f"     - {r.name}")
    print(f"[..] Objetivo: ~{total_dur:.1f}s "
          f"({per_clip:.1f}s/reel, crossfade {xfade}s)")

    fc = build_filter_complex(len(reels), per_clip, xfade)
    out_path = folder.parent / f"{folder.name}_montage.mp4"

    cmd = ["ffmpeg", "-y"]
    for r in reels:
        cmd += ["-i", str(r)]
    cmd += [
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]

    print(f"[..] Renderizando montage con {len(reels)-1} crossfades...")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[ERROR] ffmpeg fallo:")
        print(res.stderr[-2000:])
        sys.exit(1)

    if out_path.exists():
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"[OK] {out_path} ({size_mb:.1f} MB)")
    else:
        print(f"[ERROR] No se genero el archivo de salida")
        sys.exit(1)


if __name__ == "__main__":
    main()
