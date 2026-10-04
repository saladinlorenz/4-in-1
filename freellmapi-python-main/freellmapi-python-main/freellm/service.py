from __future__ import annotations
import os
import sys
import subprocess
import time
from pathlib import Path

def _python_exe() -> str:
    return sys.executable

def start_background(port: int | None = None, host: str | None = None) -> int:
    """Lance le serveur en arrière-plan (détaché) et retourne le PID."""
    env = os.environ.copy()
    if port:
        env["PORT"] = str(port)
    if host:
        env["HOST"] = host
    # utilise pythonw sur Windows pour sans console, sinon python
    py = _python_exe()
    # si pythonw existe à côté de python.exe, l'utiliser
    pyw = str(Path(py).with_name("pythonw.exe"))
    if os.name == "nt" and Path(pyw).exists():
        py = pyw
    cmd = [py, "-m", "freellm", "--no-tray"]
    # détaché
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    # logs vers data/logs/service.log
    from .config import get_config
    cfg = get_config()
    db_path = cfg.db_path or str(Path(__file__).resolve().parent.parent / "data" / "freeapi.db")
    log_dir = Path(db_path).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "service.log"
    # ouvre en append
    lf = open(log_file, "a", encoding="utf-8")
    lf.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} pid detaching ---\n")
    lf.flush()
    proc = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT, **kwargs)
    # écrit PID
    pid_file = Path(db_path).parent / "freellm.pid"
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    print(f"[service] lancé en arrière-plan PID={proc.pid} logs={log_file}")
    print(f"[service] Dashboard: http://localhost:{port or 3001}")
    return proc.pid

def stop_background() -> bool:
    from .config import get_config
    cfg = get_config()
    db_path = cfg.db_path or str(Path(__file__).resolve().parent.parent / "data" / "freeapi.db")
    pid_file = Path(db_path).parent / "freellm.pid"
    if not pid_file.exists():
        print("[service] aucun PID trouvé (pas en arrière-plan ?)")
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        pid_file.unlink(missing_ok=True)
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
        print(f"[service] stop PID {pid}")
    except Exception as e:
        print(f"[service] stop error: {e}")
    pid_file.unlink(missing_ok=True)
    return True

def status_background() -> dict:
    from .config import get_config
    cfg = get_config()
    db_path = cfg.db_path or str(Path(__file__).resolve().parent.parent / "data" / "freeapi.db")
    pid_file = Path(db_path).parent / "freellm.pid"
    if not pid_file.exists():
        return {"running": False}
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        if os.name == "nt":
            # check via tasklist
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
            running = str(pid) in out.stdout
        else:
            os.kill(pid, 0)
            running = True
        return {"running": running, "pid": pid}
    except Exception:
        return {"running": False}
