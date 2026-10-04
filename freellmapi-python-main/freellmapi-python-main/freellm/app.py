from __future__ import annotations
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .config import get_config
from .db import connect_db, get_db
from .schema import init_schema
from .seeds import seed_db
from .crypto import init_encryption_key

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg=get_config()
    # init DB
    conn=connect_db(cfg.db_path or None)
    init_schema(conn)
    # seeds
    seed_db(conn)
    # crypto
    try:
        init_encryption_key(cfg.db_path, conn)
    except Exception as e:
        print(f"[crypto] init failed: {e}")
    # ensure unified key
    row=conn.execute("SELECT value FROM settings WHERE key='unified_api_key'").fetchone()
    if not row or not row[0]:
        from .crypto import generate_unified_key
        uk=generate_unified_key()
        conn.execute("INSERT INTO settings(key,value) VALUES('unified_api_key',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (uk,))
        conn.commit()
        print(f"[init] generated unified key: {uk}")
    print(f"[freellm] ready on {cfg.host}:{cfg.port} db={cfg.db_path or 'data/freeapi.db'}")
    yield

def create_app() -> FastAPI:
    cfg=get_config()
    app=FastAPI(title="FreeLLMAPI", version="1.0.0", lifespan=lifespan)

    # CORS
    cors_origins=["http://localhost:5173","http://127.0.0.1:5173","http://[::1]:5173"] + cfg.dashboard_origins
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    # rate limit middleware for /v1 and /api
    from .routes.middleware import _is_rate_limited, _windows, _admin_windows
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path=request.url.path
        ip=request.client.host if request.client else "unknown"
        if path.startswith("/v1") or path.startswith("/v1beta") or path.startswith("/mcp"):
            if _is_rate_limited(ip, cfg.proxy_rate_limit_rpm, _windows):
                return JSONResponse(status_code=429, content={"error":{"message": f"Rate limit exceeded: more than {cfg.proxy_rate_limit_rpm} requests per minute. Retry in 60s.","type":"rate_limit_error"}}, headers={"Retry-After":"60"})
        elif path.startswith("/api"):
            if path.startswith("/api/tags") or path.startswith("/api/version") or path.startswith("/api/show") or path.startswith("/api/chat") or path.startswith("/api/generate") or path.startswith("/api/embed"):
                # ollama paths have their own limiter (skip here)
                pass
            else:
                if _is_rate_limited(ip, cfg.admin_rate_limit_rpm, _admin_windows):
                    return JSONResponse(status_code=429, content={"error":{"message": f"Admin rate limit exceeded","type":"rate_limit_error"}}, headers={"Retry-After":"60"})
        return await call_next(request)

    # error handler for 413 etc.
    @app.exception_handler(Exception)
    async def exc_handler(request: Request, exc: Exception):
        # if it's JSONResponse already?
        return JSONResponse(status_code=500, content={"error":{"message": str(exc)[:500],"type":"server_error"}})

    # routers
    from .routes.proxy import router as proxy_router
    from .routes.embeddings import router as emb_router
    from .routes.media import router as media_router
    from .routes.responses_api import router as resp_router
    from .routes.anthropic import router as anth_router
    from .routes.gemini import router as gem_router
    from .routes.ollama import router as ollama_router
    from .routes.mcp import router as mcp_router
    from .routes.admin import router as admin_router

    # anthropic must be before proxy for GET /v1/models negotiation
    app.include_router(anth_router)
    app.include_router(proxy_router)
    app.include_router(emb_router)
    app.include_router(media_router)
    app.include_router(resp_router)
    app.include_router(gem_router)
    app.include_router(ollama_router)
    app.include_router(mcp_router)
    app.include_router(admin_router)

    # docs at /v1/docs, /v1/openapi.json
    @app.get("/v1/openapi.json")
    async def openapi_json():
        return JSONResponse(content=app.openapi())

    @app.get("/v1/docs", include_in_schema=False)
    async def v1_docs():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content="""
<!DOCTYPE html>
<html>
<head>
  <title>FreeLLMAPI Py - API Docs</title>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css\">
  <style>
    body{margin:0;background:#f8fafc;font-family:Inter,system-ui,sans-serif;color:#0f172a}
    .topbar{display:none}
    #header{max-width:1100px;margin:0 auto;padding:28px 16px 0}
    #header h1{font-size:24px;margin:0}
    #header p{color:#475569;margin:8px 0 0;line-height:1.5}
    .card{max-width:1100px;margin:16px auto;padding:16px;background:white;border:1px solid #e2e8f0;border-radius:12px}
    .card h2{font-size:15px;margin:0 0 8px}
    .card p{color:#475569;font-size:13px;line-height:1.6}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
    @media(max-width:800px){.grid{grid-template-columns:1fr}}
    .api{padding:12px;border:1px solid #e2e8f0;border-radius:10px;background:#f8fafc}
    .api h3{font-size:13px;margin:0;font-family:JetBrains Mono,monospace}
    .api h3 span{font-weight:400;color:#64748b}
    .api p{font-size:12px;margin:6px 0 0}
    .api code{font-family:JetBrains Mono,monospace;font-size:11px;background:#0f172a;color:#a5b4fc;padding:2px 6px;border-radius:6px}
    .compat{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
    .compat span{font-size:11px;padding:6px 10px;border-radius:999px;background:#eef2ff;border:1px solid #c7d2fe;color:#4338ca;font-family:JetBrains Mono,monospace}
    #swagger-ui{max-width:1100px;margin:0 auto}
    .notice{max-width:1100px;margin:16px auto;padding:12px 16px;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;color:#92400e;font-size:13px}
    .notice a{color:#b45309;font-weight:600}
  </style>
</head>
<body>
  <div id=\"header\">
    <h1>FreeLLMAPI Py — API Docs</h1>
    <p>Utilisez cette passerelle <strong>comme si c'était OpenAI</strong> (ou Anthropic / Gemini / Ollama). Le router choisit le meilleur modèle gratuit disponible et gère le fallback automatique.</p>
    <div style=\"margin-top:12px;padding:12px;background:white;border:1px solid #e2e8f0;border-radius:10px;display:flex;gap:12px;flex-wrap:wrap;align-items:center\">
      <div style=\"flex:1;min-width:220px\"><div style=\"font-size:11px;color:#64748b;font-weight:700;letter-spacing:0.06em;text-transform:uppercase\">BASE URL</div><code id=\"doc-base\" style=\"font-size:13px\">http://localhost:3001/v1</code> <button onclick=\"navigator.clipboard.writeText(document.getElementById('doc-base').textContent)\" style=\"font-size:11px;padding:4px 8px\">Copier</button></div>
      <div style=\"flex:1;min-width:220px\"><div style=\"font-size:11px;color:#64748b;font-weight:700;letter-spacing:0.06em;text-transform:uppercase\">API KEY</div><code id=\"doc-key\" style=\"font-size:13px\">freellmapi-...</code> <button onclick=\"navigator.clipboard.writeText(document.getElementById('doc-key').textContent)\" style=\"font-size:11px;padding:4px 8px\">Copier</button></div>
      <a href=\"/\" style=\"font-size:12px;color:#4f46e5;font-weight:600\">→ Dashboard pour récupérer la clé</a>
    </div>
    <div class=\"compat\">
      <span>OpenAI</span><span>Anthropic</span><span>Gemini</span><span>Ollama</span><span>MCP</span>
    </div>
  </div>

  <div class=\"card\">
    <h2>APIs publiques à utiliser dans vos apps</h2>
    <p>Ce sont les seules routes à appeler depuis vos applications. Les routes <code>/api/*</code> internes (gestion des clés, analytics) ne sont pas documentées ici — utilisez le dashboard.</p>
    <div class=\"grid\">
      <div class=\"api\">
        <h3>POST /v1/chat/completions <span>— OpenAI (recommandé)</span></h3>
        <p>Chat streaming et non-streaming, tools, vision, json. <code>model: \"auto\"</code> laisse le router choisir, ou <code>auto:fast</code>/<code>auto:smart</code>/<code>fusion</code>.</p>
        <p><code>curl -H \"Authorization: Bearer freellmapi-...\" http://localhost:3001/v1/chat/completions -d '{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'</code></p>
      </div>
      <div class=\"api\">
        <h3>POST /v1/completions <span>— OpenAI legacy</span></h3>
        <p>Autocomplétion pour éditeurs (ghost text). Même auth, <code>prompt</code> au lieu de <code>messages</code>.</p>
      </div>
      <div class=\"api\">
        <h3>POST /v1/responses <span>— Codex CLI</span></h3>
        <p>Format Responses d'OpenAI utilisé par Codex. Streaming <code>response.output_text.delta</code>.</p>
      </div>
      <div class=\"api\">
        <h3>POST /v1/messages <span>— Anthropic</span></h3>
        <p>Wire Anthropic natif : <code>x-api-key: freellmapi-...</code> ou <code>Authorization: Bearer</code>. Supporte <code>tools</code>, <code>stream</code>, images base64.</p>
        <p><code>curl -H \"x-api-key: freellmapi-...\" http://localhost:3001/v1/messages -d '{\"model\":\"auto\",\"max_tokens\":512,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'</code></p>
      </div>
      <div class=\"api\">
        <h3>POST /v1beta/models/:model:generateContent <span>— Gemini</span></h3>
        <p>Wire Gemini natif : <code>?key=freellmapi-...</code> ou <code>x-goog-api-key</code>. <code>streamGenerateContent?alt=sse</code> pour le streaming.</p>
        <p><code>curl \"http://localhost:3001/v1beta/models/auto:generateContent?key=freellmapi-...\" -d '{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"hi\"}]}]}'</code></p>
      </div>
      <div class=\"api\">
        <h3>POST /api/chat · /api/generate <span>— Ollama</span></h3>
        <p>Émulation Ollama NDJSON pour Zed/JetBrains. Activez dans <code>Paramètres → Ollama emulation: open-loopback</code>. <code>POST /api/tags</code> liste les modèles.</p>
      </div>
      <div class=\"api\">
        <h3>POST /v1/embeddings</h3>
        <p>Embeddings OpenAI. <code>model: \"auto\"</code> va vers la famille par défaut, sinon épingle une famille.</p>
      </div>
      <div class=\"api\">
        <h3>POST /v1/images/generations & POST /v1/audio/*</h3>
        <p>Images (FLUX via SiliconFlow) et audio (TTS/STT). Vidéo <code>/v1/videos/generations</code> <strong>n'a pas de modèle gratuit</strong> actuellement — la route existe mais renvoie 501 ; utilisez un endpoint custom si vous en avez un.</p>
      </div>
      <div class=\"api\">
        <h3>GET /v1/models · POST /mcp</h3>
        <p>Liste des modèles disponibles (filtre <code>?available=true</code>) et serveur MCP pour les agents.</p>
      </div>
    </div>
    <div class=\"notice\">💡 <strong>Vraie doc de l'app</strong> : <a href=\"/\" target=\"_blank\">Dashboard</a> (Playground, Clés, Agents, Analytique) · <a href=\"/docs\" target=\"_blank\">/docs</a> (Swagger interne complet) · <a href=\"/v1/redoc\" target=\"_blank\">/v1/redoc</a> · <a href=\"/v1/openapi.json\" target=\"_blank\">openapi.json</a></div>
  </div>

  <div style=\"max-width:1100px;margin:16px auto;padding:0 16px\"><p style=\"color:#64748b;font-size:12px\">Astuce : toutes les réponses portent <code>X-Routed-Via: &lt;platform&gt;/&lt;model&gt;</code> pour voir quel provider a servi la requête. Base URL unique : <code>http://localhost:3001/v1</code> + <code>Authorization: Bearer freellmapi-...</code>.</p></div>

  <div id=\"swagger-ui\"></div>
  <script>
    // affiche l'URL et la clé réelles de cette instance
    (async()=>{
      try{
        document.getElementById('doc-base').textContent = window.location.origin + '/v1';
        const t = localStorage.getItem('freellm_token');
        if(t){
          const r = await fetch('/api/keys', {headers: {Authorization: 'Bearer '+t}});
          if(r.ok){ const j=await r.json(); if(j.unifiedKey) document.getElementById('doc-key').textContent=j.unifiedKey; }
        }
      }catch(e){}
    })();
  </script>
  <script src=\"https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js\"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({
        url: '/v1/openapi.json',
        dom_id: '#swagger-ui',
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
        layout: 'BaseLayout',
        deepLinking: true
      });
    };
  </script>
</body>
</html>
        """, media_type="text/html")

    @app.get("/v1/redoc", include_in_schema=False)
    async def v1_redoc():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content="""
<!DOCTYPE html>
<html>
<head><title>FreeLLMAPI Py - ReDoc</title><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>body{margin:0}</style>
</head>
<body>
  <redoc spec-url=\"/v1/openapi.json\"></redoc>
  <script src=\"https://cdn.jsdelivr.net/npm/redoc@next/bundles/redoc.standalone.js\"></script>
</body>
</html>
        """, media_type="text/html")

    @app.get("/livez")
    async def livez():
        return {"status":"ok"}

    @app.get("/readyz")
    async def readyz():
        try:
            conn=get_db()
            conn.execute("SELECT 1").fetchone()
            return {"status":"ok"}
        except Exception as e:
            return JSONResponse(status_code=503, content={"status":"error","error": str(e)})

    @app.get("/api/ping")
    async def api_ping():
        import time as _t
        return {"status":"ok","timestamp": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}

    # static serve dashboard (embedded HTML)
    dash_dir = Path(__file__).resolve().parent / "static"
    if dash_dir.exists():
        app.mount("/", StaticFiles(directory=dash_dir, html=True), name="dashboard")
    else:
        # fallback: serve index.html directly
        @app.get("/")
        async def root():
            from fastapi.responses import FileResponse
            return FileResponse(dash_dir / "index.html")

    # also serve favicon if exists
    favicon = dash_dir / "favicon.ico"
    if favicon.exists():
        @app.get("/favicon.ico")
        async def favicon_ico():
            from fastapi.responses import FileResponse
            return FileResponse(favicon)

    return app

app=create_app()
