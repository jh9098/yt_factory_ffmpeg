import argparse
import sys
from datetime import datetime
from pathlib import Path

from .edge_tts import EdgeTTSConfig
from .renderer import render_timeline_service
from .timeline_builder import build_timeline_service
from .utils import log_print


def _cli_error(message: str) -> str:
    return f"[CLI][ERROR] {message}"


def _cli_info(message: str) -> str:
    return f"[CLI][INFO] {message}"


def main():
    parser = argparse.ArgumentParser(description="Master Audio Timeline Shorts Editor")
    parser.add_argument("--base", type=str, default=".")
    parser.add_argument("--json", type=str, default=None)
    parser.add_argument("--images-dir", type=str, default=None)
    parser.add_argument("--tts-dir", type=str, default=None)
    parser.add_argument("--timeline", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--edge-tts", action="store_true")
    parser.add_argument("--edge-tts-overwrite", action="store_true")
    parser.add_argument("--edge-voice", type=str, default="ko-KR-SunHiNeural")
    parser.add_argument("--edge-rate", type=str, default="+0%")
    parser.add_argument("--edge-volume", type=str, default="+0%")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()

    if args.gui or len(sys.argv) == 1:
        try:
            import customtkinter as ctk
            root = ctk.CTk()
        except Exception:
            import tkinter as tk
            root = tk.Tk()

        from .gui import TimelineEditorGUI

        TimelineEditorGUI(root)
        root.mainloop()
        return

    base = Path(args.base)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = Path(args.json) if args.json else (base / "data" / "shorts.json")
    images_dir = Path(args.images_dir) if args.images_dir else (base / "assets" / "images" / "shorts")
    tts_dir = Path(args.tts_dir) if args.tts_dir else (base / "assets" / "tts" / "shorts")
    timeline_path = Path(args.timeline) if args.timeline else (base / "data" / "timeline.json")
    output_path = Path(args.output) if args.output else (base / "output" / f"{stamp}.mp4")

    try:
        if not args.render_only:
            build_timeline_service(
                project_dir=base,
                json_path=json_path,
                images_dir=images_dir,
                tts_dir=tts_dir,
                out_timeline_path=timeline_path,
                logger=log_print,
                edge_tts_config=EdgeTTSConfig(
                    enabled=args.edge_tts,
                    overwrite=args.edge_tts_overwrite,
                    voice=args.edge_voice,
                    rate=args.edge_rate,
                    volume=args.edge_volume,
                ),
            )
        if not args.build_only:
            render_timeline_service(timeline_path=timeline_path, output_path=output_path, logger=log_print)
        print(_cli_info(f"done | timeline={timeline_path} | output={output_path}"))
    except Exception as e:
        print(_cli_error(str(e)))
        sys.exit(1)


if __name__ == "__main__":
    main()
