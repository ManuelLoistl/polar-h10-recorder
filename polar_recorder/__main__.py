"""Entry point: python -m polar_recorder [--port 8765] [--no-browser] [--fake] [--requalify [FOLDER]]"""

import argparse
import logging
import threading
import webbrowser

import uvicorn

from .server import PROJECT_DIR, create_app


def main() -> None:
    p = argparse.ArgumentParser(description="Polar H10 recorder")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="do not open the page automatically")
    p.add_argument("--fake", action="store_true",
                   help="simulated strap, no Bluetooth; for developing the page")
    p.add_argument("--requalify", nargs="?", const="", metavar="FOLDER",
                   help="add the quality block to existing sessions (default: the configured output folder) and exit")
    args = p.parse_args()
    if args.requalify is not None:
        from pathlib import Path
        from .recorder import default_output_dir, requalify
        folder = Path(args.requalify).expanduser() if args.requalify else default_output_dir(PROJECT_DIR)
        for session_id, what in requalify(folder):
            print(f"{what:8} {session_id}")
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(PROJECT_DIR / "recorder.log")],
    )
    logging.getLogger("bleak").setLevel(logging.WARNING)
    if args.fake:
        from .fake import FakePolarLink as link_factory
        print("FAKE STRAP: no Bluetooth, synthetic signals")
    else:
        from .ble import PolarLink as link_factory
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    print(f"Polar H10 recorder at {url}  (Ctrl-C to quit)")
    uvicorn.run(create_app(link_factory=link_factory), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
