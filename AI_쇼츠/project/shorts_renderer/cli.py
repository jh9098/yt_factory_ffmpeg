import argparse
import sys
import traceback
from pathlib import Path

from .renderer import render_timeline_to_video
from .timeline_builder import build_master_audio_and_timeline
from .utils import log_print

def main():
    parser = argparse.ArgumentParser(description="Master Audio Timeline Shorts Editor")
    parser.add_argument("--base", type=str, default=r"P:\AI_shorts")
    parser.add_argument("--json", type=str, default=None)
    parser.add_argument("--images-dir", type=str, default=None)
    parser.add_argument("--tts-dir", type=str, default=None)
    parser.add_argument("--timeline", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()

    if args.gui or len(sys.argv) == 1:
        import tkinter as tk

        from .gui import TimelineEditorGUI

        root = tk.Tk()
        TimelineEditorGUI(root)
        root.mainloop()
        return

    base = Path(args.base)
    json_path = Path(args.json) if args.json else (base / "data" / "shorts.json")
    images_dir = Path(args.images_dir) if args.images_dir else (base / "assets" / "images" / "shorts" / "sample")
    tts_dir = Path(args.tts_dir) if args.tts_dir else (base / "assets" / "audio" / "tts" / "shorts")
    timeline_path = Path(args.timeline) if args.timeline else (base / "data" / "timeline.json")
    output_path = Path(args.output) if args.output else (base / "output" / "timeline_final.mp4")

    try:
        if not args.render_only:
            build_master_audio_and_timeline(
                project_dir=base,
                json_path=json_path,
                images_dir=images_dir,
                tts_dir=tts_dir,
                out_timeline_path=timeline_path,
                logger=log_print
            )
        if not args.build_only:
            render_timeline_to_video(
                timeline_path=timeline_path,
                output_path=output_path,
                logger=log_print
            )
        print(f"\n완료\nTimeline: {timeline_path}\nOutput: {output_path}")
    except Exception as e:
        print(f"[ERROR] {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
