from __future__ import annotations
import base64
import io
import time
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import get_db
from .middleware import extract_api_token, validate_unified_key

router = APIRouter()

def _err(s,m,t="invalid_request_error"):
    return JSONResponse(status_code=s, content={"error":{"message":m,"type":t}})

@router.post("/v1/images/generations")
async def images_generations(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _err(401, "Invalid API key","authentication_error")
    try:
        body=await request.json()
    except Exception:
        return _err(400, "Invalid JSON")
    prompt=body.get("prompt")
    if not prompt:
        return _err(400, "prompt is required")
    n=min(max(1, body.get("n",1)), 4)
    size=body.get("size") or "1024x1024"
    model=body.get("model") or "FLUX.1-schnell"
    # find image provider
    row=conn.execute("SELECT platform, model_id FROM media_models WHERE modality='image' AND enabled=1 LIMIT 1").fetchone()
    if not row:
        # try siliconflow
        row=conn.execute("SELECT platform FROM api_keys WHERE platform='siliconflow' AND enabled=1 LIMIT 1").fetchone()
        if not row:
            return _err(429, "No image provider configured")
        plat="siliconflow"
        mid=model
    else:
        plat,mid=row
    krow=conn.execute("SELECT encrypted_key, iv, auth_tag FROM api_keys WHERE platform=? AND enabled=1 LIMIT 1", (plat,)).fetchone()
    if not krow:
        return _err(429, f"No key for image provider {plat}")
    from ..crypto import decrypt
    try:
        key=decrypt(krow[0], krow[1], krow[2])
    except Exception:
        return _err(500, "decrypt failed")
    # call upstream: try OpenAI-compatible images endpoint
    from ..providers.registry import get_provider
    prov=get_provider(plat)
    base=getattr(prov,"base_url","") if prov else ""
    if plat=="siliconflow":
        base="https://api.siliconflow.cn/v1"
    headers={"Authorization": f"Bearer {key}","Content-Type":"application/json"}
    payload={"model": mid, "prompt": prompt, "n": n, "size": size, "response_format": body.get("response_format") or "url"}
    async with httpx.AsyncClient(timeout=120) as client:
        resp=await client.post(f"{base}/images/generations", headers=headers, json=payload)
        if not resp.is_success:
            return _err(resp.status_code, resp.text[:500])
        data=resp.json()
        # normalize to openai shape
        if "images" in data and "data" not in data:
            # some providers return {"images": [{"url":...}]}
            data={"created": int(time.time()), "data": [{"url": im.get("url") or im.get("b64_json") and f"data:image/png;base64,{im['b64_json']}"} for im in data["images"]]}
        conn.execute("INSERT INTO requests(platform, model_id, status, input_tokens, output_tokens, latency_ms, request_type) VALUES(?,?, 'success',0,0,0,'image')", (plat, mid))
        conn.commit()
        return JSONResponse(content=data)

@router.post("/v1/videos/generations")
async def videos_generations(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _err(401, "Invalid API key","authentication_error")
    # stub: not yet supported, return 501
    return _err(501, "Video generation not yet implemented in Python port — use image/audio providers or add a custom media endpoint")

@router.post("/v1/audio/speech")
async def audio_speech(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _err(401, "Invalid API key","authentication_error")
    try:
        body=await request.json()
    except Exception:
        return _err(400, "Invalid JSON")
    model=body.get("model") or "FunAudioLLM/CosyVoice2-0.5B"
    inp=body.get("input") or ""
    if not inp:
        return _err(400, "input is required")
    voice=body.get("voice") or "alloy"
    row=conn.execute("SELECT platform FROM media_models WHERE modality='audio' AND enabled=1 LIMIT 1").fetchone()
    plat=row[0] if row else "siliconflow"
    krow=conn.execute("SELECT encrypted_key, iv, auth_tag FROM api_keys WHERE platform=? LIMIT 1", (plat,)).fetchone()
    if not krow:
        return _err(429, f"No key for audio provider {plat}")
    from ..crypto import decrypt
    try:
        key=decrypt(krow[0], krow[1], krow[2])
    except Exception:
        return _err(500, "decrypt failed")
    # call upstream TTS
    async with httpx.AsyncClient(timeout=120) as client:
        resp=await client.post("https://api.siliconflow.cn/v1/audio/speech", headers={"Authorization": f"Bearer {key}","Content-Type":"application/json"}, json={"model": model, "input": inp, "voice": voice, "response_format": body.get("response_format") or "mp3"})
        if not resp.is_success:
            return _err(resp.status_code, resp.text[:500])
        # proxy binary
        from fastapi.responses import Response
        return Response(content=resp.content, media_type=resp.headers.get("content-type","audio/mpeg"))

@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    conn=get_db()
    token=extract_api_token(request)
    if not validate_unified_key(token, conn):
        return _err(401, "Invalid API key","authentication_error")
    form=await request.form()
    file=form.get("file")
    model=form.get("model") or "whisper-1"
    if not file:
        return _err(400, "file is required")
    # find transcription provider
    row=conn.execute("SELECT platform FROM media_models WHERE modality='transcription' AND enabled=1 LIMIT 1").fetchone()
    if not row:
        # try any key
        row=conn.execute("SELECT platform FROM api_keys WHERE enabled=1 LIMIT 1").fetchone()
        if not row:
            return _err(429, "No transcription provider configured")
    plat=row[0]
    krow=conn.execute("SELECT encrypted_key, iv, auth_tag FROM api_keys WHERE platform=? LIMIT 1", (plat,)).fetchone()
    if not krow:
        return _err(429, f"No key for {plat}")
    from ..crypto import decrypt
    try:
        key=decrypt(krow[0], krow[1], krow[2])
    except Exception:
        return _err(500, "decrypt failed")
    # forward multipart
    content=await file.read()
    files={"file": (file.filename or "audio.wav", content, file.content_type or "audio/wav")}
    data={"model": model}
    async with httpx.AsyncClient(timeout=120) as client:
        resp=await client.post("https://api.siliconflow.cn/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"}, files=files, data=data)
        if not resp.is_success:
            return _err(resp.status_code, resp.text[:500])
        return JSONResponse(content=resp.json())
