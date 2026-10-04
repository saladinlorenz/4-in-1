#!/usr/bin/env python3
"""
Vérifie les modèles offerts dans l'offre gratuite pour chaque provider configuré.
Usage:
  python scripts/verify_free_tiers.py
  python scripts/verify_free_tiers.py --provider groq
  python scripts/verify_free_tiers.py --json > report.json

Même principe que server/src/scripts/test-all-models.ts de l'original :
- pour chaque clé stockée (api_keys), teste chaque modèle activé de ce provider avec un mini chat
- classe : OK (200), rate_limited (429), payment_required (402), not_found (404), auth_error (401/403), error
"""
import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

# ajoute le parent au path pour importer freellm
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from freellm.db import connect_db
from freellm.crypto import decrypt, init_encryption_key
from freellm.providers.registry import get_provider

async def test_one(provider, api_key: str, model_id: str, timeout=30):
    t0 = time.time()
    try:
        res = await provider.chat_completion(api_key, [{"role":"user","content":"hi"}], model_id, {"max_tokens": 5, "temperature": 0})
        dt = int((time.time()-t0)*1000)
        # check for empty
        content = (res.get("choices", [{}])[0].get("message", {}).get("content") or "")
        return {"status": "ok", "latency_ms": dt, "content": content[:80]}
    except Exception as e:
        dt = int((time.time()-t0)*1000)
        status = getattr(e, "status", None)
        msg = str(e)[:300]
        low = msg.lower()
        if status == 429 or "rate" in low or "quota" in low or "too many" in low:
            cat = "rate_limited"
        elif status == 402 or "payment" in low or "insufficient balance" in low or "requires payment" in low:
            cat = "payment_required"
        elif status == 404 or "not found" in low or "unknown model" in low:
            cat = "not_found"
        elif status in (401,403) or "unauthorized" in low or "invalid" in low and "key" in low or "forbidden" in low:
            cat = "auth_error"
        elif "timeout" in low or "timed out" in low:
            cat = "timeout"
        else:
            cat = f"error_{status}" if status else "error"
        return {"status": cat, "latency_ms": dt, "error": msg, "http_status": status}

async def verify_provider(platform: str, rows, conn, args):
    # rows: list of (key_id, label, enc, iv, tag, base_url, models)
    results = []
    for key_id, label, enc, iv, tag, base_url, models in rows:
        try:
            api_key = decrypt(enc, iv, tag)
        except Exception as e:
            results.append({"key_id": key_id, "label": label, "error": f"decrypt failed: {e}", "models": []})
            continue
        # provider instance (custom needs base_url)
        from freellm.providers.registry import resolve_provider
        prov = resolve_provider(platform, base_url)
        if not prov:
            results.append({"key_id": key_id, "label": label, "error": f"no provider for {platform}", "models": []})
            continue
        # filter models: only those for this platform and enabled, or for custom per key
        if platform == "custom":
            q = "SELECT model_id FROM models WHERE platform='custom' AND key_id=? AND enabled=1"
            mids = [r[0] for r in conn.execute(q, (key_id,)).fetchall()]
        else:
            # respect model_scope if any? for now all
            mids = [r[0] for r in conn.execute("SELECT model_id FROM models WHERE platform=? AND enabled=1", (platform,)).fetchall()]
        if args.limit:
            mids = mids[:args.limit]
        key_res = {"key_id": key_id, "label": label, "base_url": base_url, "provider": platform, "models": []}
        for mid in mids:
            if args.dry_run:
                key_res["models"].append({"model": mid, "status": "dry_run"})
            else:
                res = await test_one(prov, api_key, mid, timeout=args.timeout)
                key_res["models"].append({"model": mid, **res})
                # petite pause pour ne pas spammer les free tiers
                await asyncio.sleep(0.5)
        results.append(key_res)
    return results

async def main():
    ap = argparse.ArgumentParser(description="Vérifie les modèles gratuits par provider")
    ap.add_argument("--provider", help="ne tester que ce platform (ex: groq)")
    ap.add_argument("--limit", type=int, default=0, help="limite modèles par clé (0 = tous)")
    ap.add_argument("--timeout", type=int, default=30, help="timeout s par requête")
    ap.add_argument("--json", action="store_true", help="sortie JSON brute")
    ap.add_argument("--dry-run", action="store_true", help="liste sans appeler les providers")
    args = ap.parse_args()

    # init db + crypto
    from freellm.config import get_config
    cfg = get_config()
    db_path = cfg.db_path or str(Path(__file__).resolve().parent.parent / "data" / "freeapi.db")
    # connect
    conn = connect_db(db_path)
    try:
        init_encryption_key(db_path, conn)
    except Exception:
        pass

    # list keys
    if args.provider:
        keys = conn.execute("SELECT id, label, encrypted_key, iv, auth_tag, base_url FROM api_keys WHERE platform=? AND enabled=1", (args.provider,)).fetchall()
        platforms = [args.provider]
    else:
        keys = conn.execute("SELECT id, platform, label, encrypted_key, iv, auth_tag, base_url FROM api_keys WHERE enabled=1").fetchall()
        # group by platform
        platforms = sorted(set(r[1] for r in keys))

    if not keys:
        print("Aucune clé configurée. Ajoutez des clés dans le dashboard -> Clés, ou via FREEAPI_CONFIG.")
        print("Providers supportés (40) :", ", ".join(p.platform for p in __import__('freellm.providers.registry', fromlist=['get_all_providers']).get_all_providers()))
        return

    # group rows by platform
    from collections import defaultdict
    by_plat = defaultdict(list)
    for r in keys:
        if args.provider:
            # r is (id, label, enc, iv, tag, base_url)
            by_plat[args.provider].append((r[0], r[1], r[2], r[3], r[4], None, None))
        else:
            # r is (id, platform, label, enc, iv, tag, base_url)
            by_plat[r[1]].append((r[0], r[2], r[3], r[4], r[5], r[6], None))

    all_results = {}
    for plat in platforms:
        rows = by_plat.get(plat, [])
        if not rows:
            continue
        print(f"\n=== {plat} ({len(rows)} clé(s)) ===", file=sys.stderr)
        res = await verify_provider(plat, rows, conn, args)
        all_results[plat] = res
        if not args.json:
            for kr in res:
                print(f"  clé #{kr['key_id']} {kr['label'] or ''} {kr.get('base_url','')}", file=sys.stderr)
                for mr in kr['models']:
                    icon = {"ok":"✓","rate_limited":"⏳","payment_required":"💳","not_found":"∅","auth_error":"🔑","timeout":"⏱"}.get(mr['status'], "✗")
                    print(f"    {icon} {mr['model']:45} {mr['status']:18} {mr.get('latency_ms','')}ms", file=sys.stderr)
                    if mr.get('error'):
                        print(f"       {mr['error'][:120]}", file=sys.stderr)

    if args.json:
        print(json.dumps(all_results, ensure_ascii=False, indent=2))
    else:
        # résumé
        print("\n--- Résumé ---", file=sys.stderr)
        total_ok = sum(1 for plat in all_results for kr in all_results[plat] for m in kr['models'] if m['status']=='ok')
        total = sum(len(kr['models']) for plat in all_results for kr in all_results[plat])
        print(f"{total_ok}/{total} modèles OK (free) sur {len(all_results)} providers testés", file=sys.stderr)
        # providers sans clé
        from freellm.providers.registry import get_all_providers
        all_plats = set(p.platform for p in get_all_providers() if p.platform!='custom')
        missing = sorted(all_plats - set(all_results.keys()))
        if missing:
            print(f"Sans clé (non testés): {', '.join(missing)}", file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(main())
