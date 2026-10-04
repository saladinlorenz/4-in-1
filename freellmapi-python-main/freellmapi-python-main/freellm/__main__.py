from __future__ import annotations
import argparse
import os
import sys
import uvicorn
from .config import get_config

def main():
    p = argparse.ArgumentParser(description="FreeLLMAPI Python — serveur OpenAI-compatible agrégateur de tiers gratuits")
    p.add_argument("--port", type=int, default=None, help="port (défaut 3001 ou $PORT)")
    p.add_argument("--host", type=str, default=None, help="host (défaut 0.0.0.0)")
    p.add_argument("--tray", action="store_true", help="lance avec icône système (tray) + dashboard auto")
    p.add_argument("--no-tray", action="store_true", help="sans tray (serveur seul)")
    p.add_argument("--background", action="store_true", help="lance en arrière-plan détaché et quitte")
    p.add_argument("--stop", action="store_true", help="arrête le serveur en arrière-plan")
    p.add_argument("--status", action="store_true", help="statut du serveur en arrière-plan")
    args = p.parse_args()

    if args.stop:
        from .service import stop_background
        ok = stop_background()
        sys.exit(0 if ok else 1)
    if args.status:
        from .service import status_background
        st = status_background()
        print(st)
        sys.exit(0)

    if args.background:
        from .service import start_background
        start_background(port=args.port, host=args.host)
        sys.exit(0)

    cfg = get_config()
    port = args.port if args.port is not None else (int(cfg.port) if isinstance(cfg.port, int) or str(cfg.port).isdigit() else 3001)
    host = args.host if args.host is not None else (str(cfg.host) if cfg.host != "::" else "0.0.0.0")
    use_tray = args.tray
    if not args.no_tray and not use_tray and os.name == "nt" and sys.stdout is not None and sys.stdout.isatty():
        try:
            import pystray  # noqa: F401
            use_tray = True
        except ImportError:
            use_tray = False

    if use_tray:
        import threading
        def _serve():
            print(f"Starting FreeLLMAPI Python on {host}:{port} (tray)")
            uvicorn.run("freellm.app:create_app", factory=True, host=host, port=port, reload=False, log_level="info")
        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        import time
        time.sleep(1.2)
        from .tray import run_tray
        run_tray(port=port)
        return

    print(f"Starting FreeLLMAPI Python on {host}:{port}")
    uvicorn.run("freellm.app:create_app", factory=True, host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
