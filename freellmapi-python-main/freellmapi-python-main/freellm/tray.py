from __future__ import annotations
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# --- autostart (Windows Startup) ---
def _startup_bat_path() -> Path:
    appdata = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "FreeLLMAPI.bat"

def _executable_cmd() -> str:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return f'start "" "{exe}" --tray'
    py = Path(sys.executable).resolve()
    pyw = py.with_name("pythonw.exe")
    if pyw.exists():
        py = pyw
    # projet = dossier parent de freellm/
    proj = Path(__file__).resolve().parent.parent
    # si lancÃ© depuis "freellmapi python" avec espace, le proj contient l'espace â€” garder les guillemets
    return f'start "" "{py}" -m freellm --tray'

def is_autostart_enabled() -> bool:
    try:
        return _startup_bat_path().exists()
    except Exception:
        return False

def set_autostart(enabled: bool) -> bool:
    p = _startup_bat_path()
    try:
        if enabled:
            p.parent.mkdir(parents=True, exist_ok=True)
            cmd = _executable_cmd()
            if getattr(sys, "frozen", False):
                content = f"@echo off\r\n{cmd}\r\n"
            else:
                proj = Path(__file__).resolve().parent.parent
                content = f"@echo off\r\ncd /d \"{proj}\"\r\n{cmd}\r\n"
            p.write_text(content, encoding="utf-8")
            print(f"[autostart] active -> {p}")
        else:
            if p.exists():
                p.unlink()
                print(f"[autostart] desactive (supprime {p})")
        return True
    except Exception as e:
        print(f"[autostart] erreur: {e}")
        return False

def _get_stats():
    try:
        from .db import get_db
        conn = get_db()
        models = conn.execute("SELECT COUNT(*) FROM models WHERE enabled=1").fetchone()[0]
        keys = conn.execute("SELECT COUNT(*) FROM api_keys WHERE enabled=1").fetchone()[0]
        reqs = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        succ = conn.execute("SELECT COUNT(*) FROM requests WHERE status='success'").fetchone()[0]
        return {"models": models, "keys": keys, "requests": reqs, "success": succ}
    except Exception:
        return {"models": 0, "keys": 0, "requests": 0, "success": 0}

def _create_image(size=64):
    for cand in [Path(__file__).resolve().parent.parent / "freellm.ico", Path(__file__).resolve().parent / "static" / "icon.png", Path(r"C:\Users\saladin\Downloads\Gemini_Generated_Image_415dlp415dlp415d.jpg")]:
        try:
            if cand.exists():
                from PIL import Image  # type: ignore
                img = Image.open(cand).convert("RGBA")
                w, h = img.size
                m = min(w, h)
                img = img.crop(((w-m)//2, (h-m)//2, (w+m)//2, (h+m)//2))
                return img.resize((size, size), Image.LANCZOS)
        except Exception:
            continue
    try:
        from PIL import Image, ImageDraw  # type: ignore
        img = Image.new("RGBA", (size, size), (99, 102, 241, 255))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, size - 4, size - 4), fill=(255, 255, 255, 255))
        d.ellipse((8, 8, size - 8, size - 8), fill=(99, 102, 241, 255))
        d.rectangle((20, 18, 44, 22), fill=(255, 255, 255, 255))
        d.rectangle((20, 18, 24, 44), fill=(255, 255, 255, 255))
        d.rectangle((20, 30, 36, 34), fill=(255, 255, 255, 255))
        return img
    except Exception:
        return None

# --- tray principal ---
def run_tray(server_thread=None, port: int = 3001):
    """IcÃ´ne systÃ¨me permanente dans les icÃ´nes cachÃ©es + fenÃªtre rÃ©duite vers tray."""
    try:
        import pystray  # type: ignore
        from pystray import MenuItem as Item, Menu
    except ImportError:
        print("[tray] pystray non installÃ© â€” fallback Tk (icÃ´nes cachÃ©es Ã©mulÃ©es)")
        print(f"[tray] Dashboard: http://localhost:{port}")
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass
        return _run_tk_fallback(port)

    # Ã©tat partagÃ© pour le fallback Tk
    tk_root = None
    tk_visible = {"value": False}

    def _open_dashboard(icon, item):
        webbrowser.open(f"http://localhost:{port}")

    def _show_logs(icon, item):
        try:
            from .config import get_config
            cfg = get_config()
            db_path = cfg.db_path or str(Path(__file__).resolve().parent.parent / "data" / "freeapi.db")
            p = Path(db_path).parent / "logs"
            folder = str(p if p.exists() else Path(db_path).parent)
            # ouvre l'explorateur sur le dossier (Windows)
            if os.name == "nt":
                os.startfile(folder)  # type: ignore
            else:
                webbrowser.open(folder)
        except Exception as e:
            print(f"[tray] logs: {e}")

    def _status_text():
        s = _get_stats()
        return f"ModÃ¨les: {s['models']} | ClÃ©s: {s['keys']} | RequÃªtes: {s['requests']} ({s['success']} ok)"

    def _toggle_autostart(icon, item):
        new = not is_autostart_enabled()
        set_autostart(new)
        # le menu se rafraÃ®chit via _menu() au prochain clic (pystray recrÃ©e le menu Ã  chaque ouverture si on le redÃ©finit)
        try:
            icon.menu = _menu()
            icon.update_menu()
        except Exception:
            pass
        # petit feedback
        icon.notify(f"Lancement au dÃ©marrage {'activÃ©' if new else 'dÃ©sactivÃ©'}", "FreeLLMAPI")

    def _show_window(icon, item):
        # si Tk fallback actif, dÃ©-minimise la fenÃªtre, sinon ouvre dashboard
        if tk_root is not None:
            try:
                tk_root.deiconify()
                tk_root.lift()
                tk_root.attributes("-topmost", True)
                tk_root.after(500, lambda: tk_root.attributes("-topmost", False))
                tk_visible["value"] = True
                return
            except Exception:
                pass
        webbrowser.open(f"http://localhost:{port}")

    def _quit(icon, item):
        # dÃ©sactive le hook de fermeture qui minimise vers tray
        try:
            if tk_root is not None:
                tk_root.destroy()
        except Exception:
            pass
        icon.visible = False
        icon.stop()
        print("[tray] arrÃªt â€” fermeture serveur...")
        os._exit(0)

    def _menu():
        stats = _status_text()
        autostart_on = is_autostart_enabled()
        return Menu(
            Item(f"FreeLLMAPI â€” {stats}", None, enabled=False),
            Item("Ouvrir Dashboard", _open_dashboard, default=True),
            Item("Afficher fenÃªtre", _show_window),
            Item("Ouvrir dossier logs", _show_logs),
            Menu.SEPARATOR,
            Item("Lancer au dÃ©marrage", _toggle_autostart, checked=lambda i: is_autostart_enabled()),
            Item("RÃ©duire vers icÃ´nes cachÃ©es (minimiser)", _hide_window, checked=lambda i: True),
            Menu.SEPARATOR,
            Item("Quitter", _quit),
        )

    def _hide_window(icon, item):
        if tk_root is not None:
            try:
                tk_root.withdraw()
                tk_visible["value"] = False
                icon.notify("FreeLLMAPI rÃ©duit vers les icÃ´nes cachÃ©es", "Cliquez sur l'icÃ´ne pour restaurer")
            except Exception:
                pass

    image = _create_image()
    if image is None:
        try:
            from PIL import Image  # type: ignore
            image = Image.new("RGBA", (64, 64), (99, 102, 241, 255))
        except Exception:
            print("[tray] Pillow manquant â€” tray sans icÃ´ne")
            return

    icon = pystray.Icon("freellmapi", image, "FreeLLMAPI", _menu())

    # updater titre + menu
    def _updater():
        while True:
            time.sleep(5)
            try:
                icon.title = f"FreeLLMAPI â€” {_status_text()}"
                # ne pas Ã©craser le menu pendant qu'il est ouvert (pystray le gÃ¨re)
                icon.menu = _menu()
                try:
                    icon.update_menu()
                except Exception:
                    pass
            except Exception:
                break

    threading.Thread(target=_updater, daemon=True).start()
    print(f"[tray] icÃ´ne dans les icÃ´nes cachÃ©es â€” Dashboard http://localhost:{port}")

    # lance la fenÃªtre Tk en parallÃ¨le mais cachÃ©e (withdraw) â€” elle ne s'affiche que sur "Afficher fenÃªtre"
    # et se rÃ©duit automatiquement vers tray quand minimisÃ©e (voir _run_tk_with_tray)
    def _tk_thread():
        nonlocal tk_root
        try:
            tk_root = _create_tk_window(port, icon, tk_visible)
            # dÃ©marre cachÃ©e â€” l'utilisateur l'affiche via le menu
            tk_root.withdraw()
            tk_visible["value"] = False
            tk_root.mainloop()
        except Exception as e:
            print(f"[tray] Tk window error: {e}")

    threading.Thread(target=_tk_thread, daemon=True).start()

    try:
        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        pass
    icon.run()

def _create_tk_window(port: int, pystray_icon, visible_flag: dict):
    import tkinter as tk
    root = tk.Tk()
    root.title("FreeLLMAPI")
    root.geometry("400x260")
    # icÃ´ne fenÃªtre si possible
    try:
        img = _create_image(32)
        if img:
            from PIL import ImageTk  # type: ignore
            tk_img = ImageTk.PhotoImage(img)
            root.iconphoto(True, tk_img)
            root._tk_img = tk_img  # type: ignore
    except Exception:
        pass

    lbl_title = tk.Label(root, text="FreeLLMAPI â€” Python", font=("Segoe UI", 14, "bold"), fg="#6366f1")
    lbl_title.pack(pady=10)
    lbl_info = tk.Label(root, text=f"Serveur: http://localhost:{port}\nEn arriÃ¨re-plan â€” icÃ´ne cachÃ©e", justify="center")
    lbl_info.pack()
    lbl_stats = tk.Label(root, text="", fg="#333", justify="center")
    lbl_stats.pack(pady=6)

    # autostart checkbox dans la fenÃªtre aussi
    autostart_var = tk.BooleanVar(value=is_autostart_enabled())
    def _on_autostart():
        set_autostart(autostart_var.get())
        # rafraÃ®chit le menu tray
        try:
            from pystray import MenuItem as Item, Menu  # type: ignore
            pystray_icon.menu = pystray_icon.menu  # trigger refresh next open
        except Exception:
            pass
    chk = tk.Checkbutton(root, text="Lancer au dÃ©marrage Windows", variable=autostart_var, command=_on_autostart)
    chk.pack()

    # hint
    lbl_hint = tk.Label(root, text="Fermer (Ã—) ou Minimiser â†’ rÃ©duit vers les icÃ´nes cachÃ©es\nQuitter via le menu de l'icÃ´ne", fg="#888", font=("Segoe UI", 8), justify="center")
    lbl_hint.pack(pady=4)

    def refresh():
        s = _get_stats()
        lbl_stats.config(text=f"ModÃ¨les: {s['models']} | ClÃ©s: {s['keys']}\nRequÃªtes: {s['requests']}  SuccÃ¨s: {s['success']}")
        if root.winfo_exists():
            root.after(3000, refresh)
    btn_open = tk.Button(root, text="Ouvrir Dashboard", command=lambda: webbrowser.open(f"http://localhost:{port}"), bg="#6366f1", fg="white", padx=16, pady=6)
    btn_open.pack(pady=4)
    btn_hide = tk.Button(root, text="RÃ©duire vers icÃ´nes cachÃ©es", command=lambda: _minimize_to_tray())
    btn_hide.pack()

    def _minimize_to_tray():
        root.withdraw()
        visible_flag["value"] = False
        try:
            pystray_icon.notify("FreeLLMAPI en arriÃ¨re-plan", "Retrouvez-le dans les icÃ´nes cachÃ©es â†’ clic pour restaurer")
        except Exception:
            pass

    def _on_close():
        # Croix â†’ minimise vers tray, pas quitter
        _minimize_to_tray()

    def _on_minimize(event):
        # Quand l'utilisateur minimise via la barre des tÃ¢ches, intercepte et cache vers tray
        if root.state() == "iconic":
            root.after(100, _minimize_to_tray)

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.bind("<Unmap>", _on_minimize)
    root.bind("<Minimize>", _on_minimize)

    refresh()
    return root

def _run_tk_fallback(port: int):
    # fallback sans pystray : fenÃªtre Tk seule qui se minimise vers tray Ã©mulÃ© (juste cachÃ©e)
    import tkinter as tk
    root = tk.Tk()
    root.title("FreeLLMAPI")
    root.geometry("400x240")
    lbl_title = tk.Label(root, text="FreeLLMAPI â€” Python", font=("Segoe UI", 14, "bold"), fg="#6366f1")
    lbl_title.pack(pady=12)
    lbl_info = tk.Label(root, text=f"Serveur: http://localhost:{port}\nMode sans pystray â€” installez pystray pour les icÃ´nes cachÃ©es", wraplength=360, justify="center")
    lbl_info.pack()
    lbl_stats = tk.Label(root, text="", fg="#333", justify="center")
    lbl_stats.pack(pady=8)
    autostart_var = tk.BooleanVar(value=is_autostart_enabled())
    tk.Checkbutton(root, text="Lancer au dÃ©marrage", variable=autostart_var, command=lambda: set_autostart(autostart_var.get())).pack()
    def refresh():
        s = _get_stats()
        lbl_stats.config(text=f"ModÃ¨les: {s['models']} | ClÃ©s: {s['keys']}\nRequÃªtes: {s['requests']}  SuccÃ¨s: {s['success']}")
        root.after(3000, refresh)
    tk.Button(root, text="Ouvrir Dashboard", command=lambda: webbrowser.open(f"http://localhost:{port}"), bg="#6366f1", fg="white", padx=16, pady=6).pack(pady=6)
    # minimise vers cachÃ© : withdraw + bouton restaurer via raccourci
    hidden = {"minimized": False}
    def _to_hidden():
        root.withdraw()
        hidden["minimized"] = True
        # crÃ©e une petite fenÃªtre de notification pour restaurer
        top = tk.Toplevel()
        top.title("FreeLLMAPI cachÃ©")
        top.geometry("300x100+20+20")
        top.attributes("-topmost", True)
        tk.Label(top, text="FreeLLMAPI rÃ©duit vers icÃ´nes cachÃ©es\n(Fallback sans pystray)").pack(pady=10)
        tk.Button(top, text="Restaurer", command=lambda: (top.destroy(), root.deiconify(), setattr(hidden, "minimized", False))).pack()
        top.protocol("WM_DELETE_WINDOW", lambda: (top.destroy(), root.deiconify()))
    root.protocol("WM_DELETE_WINDOW", _to_hidden)
    # intercepte minimise
    def _on_unmap(e):
        if root.state() == "iconic":
            root.after(100, _to_hidden)
    root.bind("<Unmap>", _on_unmap)
    tk.Button(root, text="RÃ©duire vers icÃ´nes cachÃ©es", command=_to_hidden).pack()
    tk.Button(root, text="Quitter", command=lambda: (root.destroy(), __import__("os")._exit(0))).pack(pady=4)
    refresh()
    root.mainloop()

