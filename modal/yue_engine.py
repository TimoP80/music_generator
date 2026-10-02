"""Modal GPU inference service for YuE2, separate from TIMBOR's renderer.

YuE2 model weights are CC BY-NC 4.0 with additional creator terms. This
service is for authorized personal/noncommercial use only unless separately
licensed. Deploy from the repository root with ``modal deploy modal/yue_engine.py``.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import time
import uuid
from pathlib import Path

import modal

APP_NAME = "timbor-yue2"
MODEL = "m-a-p/YuE2-3B"
VAE = "m-a-p/YuE2-Vae"
CACHE = "/cache/huggingface"


def _read_local_env(name: str) -> str:
    """Read one value from the ignored project dotenv without printing it."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return os.environ.get(name, "")
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("\"'")
    return os.environ.get(name, "")


MAX_DURATION = float(_read_local_env("YUE2_MAX_DURATION") or "600")
IDLE_TIMEOUT = int(_read_local_env("YUE2_IDLE_TIMEOUT") or "300")
CPU = float(_read_local_env("YUE2_CPU") or "8")
MEMORY_MB = int(_read_local_env("YUE2_MEMORY") or "32768")
GPU = _read_local_env("YUE2_GPU") or "L40S"

if modal.is_local() and not _read_local_env("YUE2_API_KEY"):
    raise RuntimeError("Set YUE2_API_KEY in the ignored project .env before deploying")
api_secret = modal.Secret.from_dict(
    {"YUE2_API_KEY": _read_local_env("YUE2_API_KEY")} if modal.is_local() else {})
model_cache = modal.Volume.from_name("timbor-yue2-model-cache", create_if_missing=True)
image = (
    modal.Image.from_registry("nvidia/cuda:13.0.0-cudnn-devel-ubuntu22.04", add_python="3.12")
    .apt_install("git", "ffmpeg")
    .run_commands(
        "git clone --branch yue2-v0.1.6 --depth 1 "
        "https://github.com/multimodal-art-projection/YuE.git /opt/yue2",
    )
    .uv_pip_install("/opt/yue2")
    .env({"HF_HOME": CACHE, "HF_HUB_CACHE": f"{CACHE}/hub",
          "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONUNBUFFERED": "1"})
    .entrypoint([])
)
app = modal.App(APP_NAME)


@app.cls(image=image, gpu=GPU, cpu=CPU, memory=MEMORY_MB,
         volumes={"/cache": model_cache}, secrets=[api_secret],
         scaledown_window=IDLE_TIMEOUT, timeout=7200, startup_timeout=1800,
         max_containers=1)
class Yue2Service:
    @modal.enter()
    def load_model(self):
        from yue2 import YuE2Pipeline

        model_cache.reload()
        self.model = YuE2Pipeline.from_pretrained(MODEL, vae=VAE, device="cuda")
        model_cache.commit()

    @modal.asgi_app()
    def web(self):
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import Response

        globals().update(Request=Request)
        api = FastAPI(title="TIMBOR YuE2", docs_url=None, redoc_url=None)

        def authenticate(request: Request) -> None:
            supplied = request.headers.get("authorization", "")
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:].strip()
            expected = os.environ.get("YUE2_API_KEY", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                raise HTTPException(status_code=401, detail="invalid API key")

        @api.get("/health")
        async def health(request: Request):
            authenticate(request)
            return {"service": "yue2", "status": "ok", "model": MODEL}

        @api.post("/generate")
        async def generate(request: Request):
            authenticate(request)
            request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
            try:
                doc = await request.json()
                prompt = str(doc.get("prompt", "")).strip()
                lyrics = str(doc.get("lyrics") or "[Instrumental]").strip()
                duration = float(doc.get("duration", 180))
                seed = int(doc.get("seed", -1))
                model_name = doc.get("model", "yue2-music")
                if not prompt or model_name != "yue2-music":
                    raise HTTPException(status_code=400, detail="prompt and model=yue2-music are required")
                if len(prompt) > 512 or len(lyrics) > 4096:
                    raise HTTPException(status_code=400, detail="YuE2 prompt must be <=512 and lyrics <=4096 characters")
                if not 1 <= duration <= MAX_DURATION:
                    raise HTTPException(status_code=400, detail=f"duration must be 1..{MAX_DURATION:g} seconds")
                if seed < -1 or seed >= 2**32:
                    raise HTTPException(status_code=400, detail="seed must be -1 or a 32-bit unsigned integer")
                effective_seed = secrets.randbelow(2**32) if seed == -1 else seed
                started = time.monotonic()
                song = self.model(style=prompt, lyrics=lyrics, cot="full", seed=effective_seed)
                audio_array = song.audio
                if hasattr(audio_array, "detach"):
                    audio_array = audio_array.detach().cpu().numpy()
                import numpy as np
                audio_array = np.asarray(audio_array)
                sample_rate = int(song.sample_rate)
                if sample_rate != 48000 or audio_array.ndim != 2:
                    raise RuntimeError("YuE2 returned an unexpected audio shape or sample rate")
                if audio_array.shape[-1] == 2:
                    pass
                elif audio_array.shape[0] == 2:
                    audio_array = audio_array.T
                else:
                    raise RuntimeError("YuE2 did not return stereo audio")
                duration_seconds = len(audio_array) / sample_rate
                if duration_seconds <= 0 or duration_seconds > MAX_DURATION:
                    raise RuntimeError("YuE2 output duration is outside the configured service limit")
                with tempfile.TemporaryDirectory(prefix="timbor-yue2-") as temp_dir:
                    audio_path = Path(temp_dir) / "audio.wav"
                    import soundfile as sf
                    sf.write(audio_path, audio_array, sample_rate, subtype="PCM_16")
                    audio = audio_path.read_bytes()
                metadata = {"job_id": request_id, "model": MODEL, "seed": effective_seed,
                            "request_seed": seed, "duration": duration_seconds,
                            "requested_duration": duration, "sample_rate": sample_rate,
                            "channels": 2, "generation_seconds": round(time.monotonic() - started, 3),
                            "truncated": bool(song.truncated)}
                from fastapi.encoders import jsonable_encoder
                from fastapi.responses import Response
                import base64
                headers = {"X-Job-ID": request_id, "X-Model": MODEL,
                           "X-Seed": str(effective_seed),
                           "X-Audio-Metadata": base64.b64encode(
                               json.dumps(jsonable_encoder(metadata), separators=(",", ":")).encode()).decode()}
                return Response(audio, media_type="audio/wav", headers=headers)
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=500, detail="YuE2 generation failed") from exc

        return api
