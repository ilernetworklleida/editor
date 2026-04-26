"""
Subtitulado automatico de un video a formato .srt usando faster-whisper.
Autodetecta el idioma del audio.

Uso:
    python scripts/01_subtitular.py input/mi_video.mp4
    python scripts/01_subtitular.py input/mi_video.mp4 medium
    python scripts/01_subtitular.py input/mi_video.mp4 small es   # forzar idioma

Modelos disponibles (peso/precision crecientes):
    tiny, base, small (default), medium, large
"""
import sys
from pathlib import Path

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("[ERROR] Falta faster-whisper. Instala con: pip install -r requirements.txt")
    sys.exit(1)


def format_time(seconds: float) -> str:
    """Convierte segundos a formato SRT: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main(video_path: str, model_size: str = "small", language: str | None = None) -> None:
    video = Path(video_path)
    if not video.exists():
        print(f"[ERROR] No existe el archivo: {video}")
        sys.exit(1)

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    srt_path = output_dir / f"{video.stem}.srt"

    print(f"[..] Cargando modelo Whisper '{model_size}' (la 1a vez descarga ~MB)")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    modo = f"forzando idioma '{language}'" if language else "autodetectando idioma"
    print(f"[..] Transcribiendo {video.name} ({modo})")
    segments, info = model.transcribe(str(video), language=language, beam_size=5)
    print(f"[..] Idioma detectado: {info.language} (prob {info.language_probability:.2f})")

    with srt_path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n")
            f.write(f"{format_time(seg.start)} --> {format_time(seg.end)}\n")
            f.write(f"{seg.text.strip()}\n\n")
            print(f"  [{format_time(seg.start)}] {seg.text.strip()[:70]}")

    print(f"\n[OK] Subtitulos generados: {srt_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    video_arg = sys.argv[1]
    model_arg = sys.argv[2] if len(sys.argv) > 2 else "small"
    lang_arg = sys.argv[3] if len(sys.argv) > 3 else None
    main(video_arg, model_arg, lang_arg)
