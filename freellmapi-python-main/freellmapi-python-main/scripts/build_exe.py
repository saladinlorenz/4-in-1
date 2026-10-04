#!/usr/bin/env python3
"""Build un exécutable standalone avec PyInstaller (Windows .exe)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
def run(cmd):
    print(f"> {' '.join(cmd)}")
    subprocess.check_call(cmd)

def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller manquant — installation...")
        run([sys.executable, "-m", "pip", "install", "pyinstaller"])
    # icône: génère une icône simple si Pillow dispo
    icon = ROOT / "freellm.ico"
    if not icon.exists():
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (256, 256), (99, 102, 241, 255))
            d = ImageDraw.Draw(img)
            d.ellipse((16, 16, 240, 240), fill=(255, 255, 255))
            d.ellipse((32, 32, 224, 224), fill=(99, 102, 241))
            d.rectangle((80, 72, 176, 88), fill="white")
            d.rectangle((80, 72, 96, 176), fill="white")
            d.rectangle((80, 120, 144, 136), fill="white")
            img.save(icon, sizes=[(256, 256)])
            print(f"icône générée: {icon}")
        except Exception as e:
            print(f"icône non générée ({e}) — build sans icône")
            icon = None

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "FreeLLMAPI",
        "--onefile",
        "--console",  # console pour logs; use --windowed pour sans console (tray seul)
        "--paths", str(ROOT),
        "--hidden-import", "freellm.app",
        "--hidden-import", "freellm.providers.registry",
        "--collect-all", "httpx",
        "--collect-all", "uvicorn",
        str(ROOT / "freellm" / "__main__.py"),
    ]
    if icon and icon.exists():
        cmd.extend(["--icon", str(icon)])
    # séparer build console vs windowed : par défaut console, l'utilisateur peut refaire avec --windowed
    run(cmd)
    exe = ROOT / "dist" / ("FreeLLMAPI.exe" if sys.platform == "win32" else "FreeLLMAPI")
    if exe.exists():
        print(f"\n✓ Exécutable: {exe} ({exe.stat().st_size/1_000_000:.1f} MB)")
        print("  Lancement: ./dist/FreeLLMAPI --tray")
        print("  Arrière-plan: ./dist/FreeLLMAPI --background  puis  --stop / --status")
        print("  Ouvrir dashboard: http://localhost:3001")
    else:
        print("Build terminé mais exe non trouvé — voir dist/")

if __name__ == "__main__":
    main()
