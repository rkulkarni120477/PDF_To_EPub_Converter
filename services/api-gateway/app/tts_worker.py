"""Standalone worker that synthesizes one page of narration to a WAV file.

Run as a subprocess (not a thread) so the caller can enforce a hard,
killable timeout. pyttsx3's SAPI5 driver on Windows is known to hang
indefinitely inside engine.runAndWait() under some COM/threading
conditions; a thread cannot be forcibly terminated once stuck in native
COM code, but a subprocess can be killed by the parent on timeout.
"""
import sys
from pathlib import Path

import pyttsx3


def main() -> int:
    text_path, audio_path = Path(sys.argv[1]), Path(sys.argv[2])
    text = text_path.read_text(encoding="utf-8")
    engine = pyttsx3.init()
    engine.setProperty("rate", 165)
    engine.save_to_file(text, str(audio_path))
    engine.runAndWait()
    return 0 if audio_path.exists() and audio_path.stat().st_size > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
