from __future__ import annotations
import json
from typing import Dict, Any

DEFAULT_MODE = "off"
MODE_MAP = {
    "Desactivee": "off", "Sans perte": "lossless", "Standard": "standard", "Agressive": "aggressive",
    "off": "off", "sans perte": "lossless", "lossless": "lossless", "standard": "standard", "aggressive": "aggressive"
}

ENGINES = [
    {"id": "dedup", "label": "Blocs repetes", "sub": "dedup - Sans perte", "lossy": False},
    {"id": "lite", "label": "Nettoyage des espaces", "sub": "lite - Sans perte", "lossy": False},
    {"id": "read-lifecycle", "label": "Lectures de fichiers remplaces", "sub": "read-lifecycle - Avec perte", "lossy": True},
    {"id": "toolfilter", "label": "Filtre de sortie des outils", "sub": "toolfilter - Avec perte", "lossy": True},
    {"id": "jsoncompact", "label": "Tables JSON", "sub": "jsoncompact - Sans perte", "lossy": False},
    {"id": "relevance", "label": "Filtre de pertinence", "sub": "relevance - Avec perte", "lossy": True},
    {"id": "aging", "label": "Echanges plus anciens", "sub": "aging - Avec perte", "lossy": True},
    {"id": "hard-budget", "label": "Plafond de tokens", "sub": "hard-budget - Avec perte", "lossy": True},
]

DEFAULT_ENGINES_STATE = {e["id"]: True for e in ENGINES}

def get_config(conn) -> Dict[str, Any]:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='compression_config'").fetchone()
        if row and row[0]:
            cfg = json.loads(row[0])
            mode = cfg.get("mode", DEFAULT_MODE)
            engines = cfg.get("engines", DEFAULT_ENGINES_STATE)
            for eid in DEFAULT_ENGINES_STATE:
                engines.setdefault(eid, True)
            return {"mode": mode, "engines": engines}
    except Exception:
        pass
    return {"mode": DEFAULT_MODE, "engines": DEFAULT_ENGINES_STATE.copy()}

def set_config(conn, mode: str, engines: Dict[str, bool] | None = None):
    cur = get_config(conn)
    mode_norm = MODE_MAP.get(mode, MODE_MAP.get(mode.lower(), DEFAULT_MODE)) if isinstance(mode, str) else DEFAULT_MODE
    if engines is not None:
        for k, v in engines.items():
            if k in DEFAULT_ENGINES_STATE:
                cur["engines"][k] = bool(v)
    cur["mode"] = mode_norm
    conn.execute("INSERT INTO settings(key,value) VALUES('compression_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(cur),))
    conn.commit()
    return cur

def get_stats():
    return {"enabled": False, "mode": DEFAULT_MODE, "engines": ENGINES}

def compress(messages, mode="off"):
    if mode == "off" or mode == "Desactivee":
        return messages
    return messages
