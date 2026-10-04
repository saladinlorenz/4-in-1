from __future__ import annotations
import json
import time
import hashlib
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..db import get_db
from ..services.model_listing import build_model_listing
from ..lib.tokens import estimate_tokens
from ..services.router import route_request

router = APIRouter()

def _is_loopback(request: Request) -> bool:
    host=(request.client.host if request.client else "")
    host=host.replace("::ffff:","")
    if host in ("127.0.0.1","::1","localhost","testclient","test"):
        return True
    xff=request.headers.get("x-forwarded-for")
    if xff:
        first=xff.split(",")[0].strip().replace("::ffff:","")
        if first not in ("127.0.0.1","::1"):
            return False
    # allow testclient to be considered loopback when ollama emulation is open
    if host=="testclient":
        return True
    return False

def _mode(conn) -> str:
    row=conn.execute("SELECT value FROM settings WHERE key='ollama_emulation'").fetchone()
    return (row[0] if row else "off")

def _check_auth(request: Request, conn) -> bool:
    mode=_mode(conn)
    if mode=="off":
        return False
    if mode=="open-loopback" and _is_loopback(request):
        return True
    if mode=="key-required":
        from .middleware import extract_api_token, validate_unified_key
        tok=extract_api_token(request)
        if tok and validate_unified_key(tok, conn):
            return True
        return False
    # open-loopback but not loopback -> need key
    from .middleware import extract_api_token, validate_unified_key
    tok=extract_api_token(request)
    if tok and validate_unified_key(tok, conn):
        return True
    return _is_loopback(request) if mode=="open-loopback" else False

def _strip_latest(model: str) -> str:
    if model.endswith(":latest"):
        return model[:-7]
    return model

@router.get("/api/version")
async def ollama_version(request: Request):
    conn=get_db()
    if not _check_auth(request, conn):
        return JSONResponse(status_code=401, content={"error":"Unauthorized"})
    return JSONResponse(content={"version":"0.9.9"})

@router.get("/api/tags")
async def ollama_tags(request: Request):
    conn=get_db()
    if not _check_auth(request, conn):
        return JSONResponse(status_code=401, content={"error":"Unauthorized"})
    objs,_=build_model_listing(conn, available_only=True)
    models=[]
    for o in objs:
        models.append({
            "name": f"{o['id']}:latest",
            "model": o["id"],
            "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "size": 0,
            "digest": hashlib.sha256(o["id"].encode()).hexdigest()[:12],
            "details":{"format":"freellmapi","family": o["platform"],"parameter_size":"remote","quantization_level":"remote"},
        })
    # add auto
    models.insert(0, {"name":"auto:latest","model":"auto","modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),"size":0,"digest":"auto","details":{"format":"freellmapi","family":"freellmapi","parameter_size":"remote","quantization_level":"remote"}})
    return JSONResponse(content={"models": models})

@router.post("/api/show")
async def ollama_show(request: Request):
    conn=get_db()
    if not _check_auth(request, conn):
        return JSONResponse(status_code=401, content={"error":"Unauthorized"})
    try:
        body=await request.json()
    except Exception:
        body={}
    model=_strip_latest(body.get("model") or body.get("name") or "auto")
    if model=="auto":
        return JSONResponse(content={"model":"auto","modelfile":"","parameters":"","template":"","details":{"format":"freellmapi","family":"freellmapi"},"model_info":{"general.architecture":"auto"},"capabilities":["completion"],"num_ctx":128000})
    row=conn.execute("SELECT context_window, display_name FROM models WHERE model_id=? LIMIT 1", (model,)).fetchone()
    ctx=row[0] if row else 128000
    return JSONResponse(content={"model":model,"modelfile":"","parameters":"","template":"","details":{"format":"freellmapi","family":"auto"},"capabilities":["completion","tools"],"num_ctx": ctx or 128000})

@router.post("/api/chat")
async def ollama_chat(request: Request):
    conn=get_db()
    if not _check_auth(request, conn):
        return JSONResponse(status_code=401, content={"error":"Unauthorized"})
    try:
        body=await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error":"invalid json"})
    if not body.get("messages"):
        # load/unload probe
        return JSONResponse(content={"model": body.get("model","auto"),"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),"message":{"role":"assistant","content":""},"done":True,"done_reason":"load"})
    raw_model=_strip_latest(body.get("model") or "auto")
    stream=body.get("stream", True)
    opts={}
    options=body.get("options") or {}
    if "num_predict" in options:
        opts["max_tokens"]=options["num_predict"]
    if "temperature" in options:
        opts["temperature"]=options["temperature"]
    if "top_p" in options:
        opts["top_p"]=options["top_p"]
    if "top_k" in options:
        opts["top_k"]=options["top_k"]
    if "stop" in options:
        opts["stop"]=options["stop"]
    # format json
    fmt=body.get("format")
    if fmt=="json":
        opts["response_format"]={"type":"json_object"}
    # convert messages: ollama messages already chat-like but may have images
    messages=[]
    for m in body.get("messages") or []:
        role=m.get("role","user")
        content=m.get("content") or ""
        # images array -> image_url blocks
        imgs=m.get("images") or []
        if imgs:
            parts=[{"type":"text","text": content}]
            for b64 in imgs:
                parts.append({"type":"image_url","image_url":{"url": f"data:image/png;base64,{b64}"}})
            messages.append({"role": role, "content": parts})
        else:
            messages.append({"role": role, "content": content})
    est=estimate_tokens(messages)
    try:
        route=route_request(conn, estimated_tokens=est, messages=messages, requested_model=raw_model if raw_model!="auto" else "auto", output_reserve=opts.get("max_tokens") or 1024)
    except Exception as e:
        return JSONResponse(status_code=getattr(e,"status",429), content={"error": str(e)})
    provider=route["provider"]
    if not stream:
        try:
            res=await provider.chat_completion(route["apiKey"], messages, route["modelId"], opts)
            text=(res["choices"][0]["message"].get("content") or "")
            tcs=res["choices"][0]["message"].get("tool_calls") or []
            # map tool_calls to ollama format if needed
            if tcs:
                # ollama tool_calls in message
                msg={"role":"assistant","content": text, "tool_calls": [{"function":{"name": tc["function"]["name"],"arguments": tc["function"]["arguments"]}} for tc in tcs]}
            else:
                msg={"role":"assistant","content": text}
            conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms) VALUES(?,?,?,?,?,?,?)",
                         (route["platform"], route["modelId"], route["keyId"], "success", est, len(text)//4, 0))
            conn.commit()
            return JSONResponse(content={
                "model": raw_model, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "message": msg, "done": True, "done_reason":"stop",
                "total_duration": 1000000000, "load_duration": 100000000, "prompt_eval_count": est, "eval_count": len(text)//4
            })
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": str(e)[:500]})
    else:
        from fastapi.responses import StreamingResponse
        import json as js
        async def gen():
            created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            t0=time.time()
            text_acc=""
            try:
                async for chunk in provider.stream_chat_completion(route["apiKey"], messages, route["modelId"], opts):
                    for ch in chunk.get("choices") or []:
                        delta=ch.get("delta") or {}
                        if delta.get("content"):
                            txt=delta["content"]
                            text_acc+=txt
                            yield js.dumps({"model": raw_model, "created_at": created, "message":{"role":"assistant","content": txt},"done": False})+"\n"
                        if delta.get("tool_calls"):
                            # buffer tool calls, emit at end as full message?
                            pass
                        if ch.get("finish_reason"):
                            total=int((time.time()-t0)*1e9) or 1
                            yield js.dumps({"model": raw_model, "created_at": created, "message":{"role":"assistant","content":""},"done": True, "done_reason": "stop","total_duration": total,"load_duration": int(total*0.2),"prompt_eval_count": est,"eval_count": len(text_acc)//4})+"\n"
                            conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms) VALUES(?,?,?,?,?,?,?)",
                                         (route["platform"], route["modelId"], route["keyId"], "success", est, len(text_acc)//4, 0))
                            conn.commit()
                            return
                # fallback if no finish
                total=int((time.time()-t0)*1e9) or 1
                yield js.dumps({"model": raw_model, "created_at": created, "message":{"role":"assistant","content":""},"done": True,"done_reason":"stop","total_duration": total,"load_duration": int(total*0.2),"prompt_eval_count": est,"eval_count": len(text_acc)//4})+"\n"
            except Exception as e:
                yield js.dumps({"error": str(e)[:300]})+"\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")

@router.post("/api/generate")
async def ollama_generate(request: Request):
    conn=get_db()
    if not _check_auth(request, conn):
        return JSONResponse(status_code=401, content={"error":"Unauthorized"})
    try:
        body=await request.json()
    except Exception:
        body={}
    prompt=body.get("prompt") or ""
    system=body.get("system") or ""
    suffix=body.get("suffix") or ""
    if suffix:
        prompt+= f"\nComplete the text before this suffix:\n{suffix}"
    raw_model=_strip_latest(body.get("model") or "auto")
    stream=body.get("stream", True)
    opts={}
    options=body.get("options") or {}
    if "num_predict" in options:
        opts["max_tokens"]=options["num_predict"]
    if "temperature" in options:
        opts["temperature"]=options["temperature"]
    messages=[]
    if system:
        messages.append({"role":"system","content": system})
    messages.append({"role":"user","content": prompt})
    # context history (ollama generate context is opaque — ignore)
    est=estimate_tokens(messages)
    try:
        route=route_request(conn, estimated_tokens=est, messages=messages, requested_model=raw_model if raw_model!="auto" else "auto", output_reserve=opts.get("max_tokens") or 1024)
    except Exception as e:
        return JSONResponse(status_code=getattr(e,"status",429), content={"error": str(e)})
    provider=route["provider"]
    if not stream:
        try:
            res=await provider.chat_completion(route["apiKey"], messages, route["modelId"], opts)
            text=(res["choices"][0]["message"].get("content") or "")
            conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms) VALUES(?,?,?,?,?,?,?)",
                         (route["platform"], route["modelId"], route["keyId"], "success", est, len(text)//4, 0))
            conn.commit()
            return JSONResponse(content={"model": raw_model,"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),"response": text,"done": True,"context":[],"total_duration":1000000000,"load_duration":100000000,"prompt_eval_count": est,"eval_count": len(text)//4})
        except Exception as e:
            return JSONResponse(status_code=502, content={"error": str(e)[:500]})
    else:
        from fastapi.responses import StreamingResponse
        async def gen():
            created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            t0=time.time()
            text_acc=""
            try:
                async for chunk in provider.stream_chat_completion(route["apiKey"], messages, route["modelId"], opts):
                    for ch in chunk.get("choices") or []:
                        delta=ch.get("delta") or {}
                        if delta.get("content"):
                            txt=delta["content"]
                            text_acc+=txt
                            yield json.dumps({"model": raw_model,"created_at": created,"response": txt,"done": False})+"\n"
                        if ch.get("finish_reason"):
                            total=int((time.time()-t0)*1e9) or 1
                            yield json.dumps({"model": raw_model,"created_at": created,"response":"","done": True,"context":[],"total_duration": total,"load_duration": int(total*0.2),"prompt_eval_count": est,"eval_count": len(text_acc)//4})+"\n"
                            conn.execute("INSERT INTO requests(platform, model_id, key_id, status, input_tokens, output_tokens, latency_ms) VALUES(?,?,?,?,?,?,?)",
                                         (route["platform"], route["modelId"], route["keyId"], "success", est, len(text_acc)//4, 0))
                            conn.commit()
                            return
                total=int((time.time()-t0)*1e9) or 1
                yield json.dumps({"model": raw_model,"created_at": created,"response":"","done": True,"context":[],"total_duration": total,"load_duration": int(total*0.2),"prompt_eval_count": est,"eval_count": len(text_acc)//4})+"\n"
            except Exception as e:
                yield json.dumps({"error": str(e)[:300]})+"\n"
        return StreamingResponse(gen(), media_type="application/x-ndjson")

@router.post("/api/embed")
@router.post("/api/embeddings")
async def ollama_embed(request: Request):
    conn=get_db()
    if not _check_auth(request, conn):
        return JSONResponse(status_code=401, content={"error":"Unauthorized"})
    try:
        body=await request.json()
    except Exception:
        body={}
    model=_strip_latest(body.get("model") or "auto")
    inp=body.get("input") or body.get("prompt") or ""
    inputs=[inp] if isinstance(inp,str) else inp
    # try embedding path: use first embedding provider
    row=conn.execute("SELECT platform, model_id FROM embedding_models WHERE enabled=1 LIMIT 1").fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error":"no embedding models configured"})
    plat,mid=row
    krow=conn.execute("SELECT encrypted_key, iv, auth_tag FROM api_keys WHERE platform=? AND enabled=1 LIMIT 1", (plat,)).fetchone()
    if not krow:
        return JSONResponse(status_code=429, content={"error": f"no key for {plat}"})
    from ..crypto import decrypt
    from ..providers.registry import get_provider
    try:
        key=decrypt(krow[0], krow[1], krow[2])
    except Exception:
        return JSONResponse(status_code=500, content={"error":"decrypt failed"})
    prov=get_provider(plat)
    base=getattr(prov,"base_url","")
    import httpx
    headers={"Authorization": f"Bearer {key}","Content-Type":"application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp=await client.post(f"{base}/embeddings", headers=headers, json={"model": mid, "input": inputs})
        if not resp.is_success:
            return JSONResponse(status_code=resp.status_code, content={"error": resp.text[:500]})
        data=resp.json()
        # normalize to ollama shape: embeddings[]
        if "data" in data:
            embs=[d["embedding"] for d in data["data"]]
            return JSONResponse(content={"embeddings": embs, "prompt_eval_count": sum(len(s)//4 for s in inputs)})
        return JSONResponse(content=data)
