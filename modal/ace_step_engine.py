"""Modal GPU inference service for ACE-Step 1.5.

The inference stack follows Modal's ACE-Step 1.5 example and uses the official
MIT-licensed project. Deploy from the repository root with
``modal deploy modal/ace_step_engine.py``.
"""
from __future__ import annotations

import hmac
import json
import os
import tempfile
import time
import uuid
import base64
from pathlib import Path

import modal

APP_NAME = "timbor-acestep"
PROJECT_ROOT = "/opt/ace-step"
CHECKPOINTS = f"{PROJECT_ROOT}/checkpoints"


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


MAX_DURATION = float(_read_local_env("ACESTEP_MAX_DURATION") or "600")
IDLE_TIMEOUT = int(_read_local_env("ACESTEP_IDLE_TIMEOUT") or "300")
CPU = float(_read_local_env("ACESTEP_CPU") or "8")
MEMORY_MB = int(_read_local_env("ACESTEP_MEMORY") or "32768")
GPU = _read_local_env("ACESTEP_GPU") or "L40S"
LM_MODEL = _read_local_env("ACESTEP_LM_MODEL") or "acestep-5Hz-lm-0.6B"

if modal.is_local() and not _read_local_env("ACESTEP_API_KEY"):
    raise RuntimeError("Set ACESTEP_API_KEY in the ignored project .env before deploying")
api_secret = modal.Secret.from_dict(
    {"ACESTEP_API_KEY": _read_local_env("ACESTEP_API_KEY")} if modal.is_local() else {})
model_cache = modal.Volume.from_name("timbor-acestep-model-cache", create_if_missing=True)
image = (
    modal.Image.from_registry("nvidia/cuda:13.0.0-cudnn-devel-ubuntu22.04", add_python="3.12")
    .apt_install("git", "ffmpeg")
    .run_commands(
        "git clone --branch v0.1.8 --depth 1 "
        "https://github.com/ace-step/ACE-Step-1.5.git /opt/ace-step",
    )
    .uv_pip_install("/opt/ace-step", "hf_transfer==0.1.9", "torchcodec==0.10.0", "torch~=2.10.0")
    .env({"ACESTEP_PROJECT_ROOT": PROJECT_ROOT, "HF_HUB_ENABLE_HF_TRANSFER": "1",
          "HF_HOME": f"{CHECKPOINTS}/hf", "TOKENIZERS_PARALLELISM": "false",
          "ACESTEP_DISABLE_TQDM": "true", "PYTHONUNBUFFERED": "1"})
    .entrypoint([])
)
app = modal.App(APP_NAME)


@app.cls(image=image, gpu=GPU, cpu=CPU, memory=MEMORY_MB,
         volumes={CHECKPOINTS: model_cache}, secrets=[api_secret],
         scaledown_window=IDLE_TIMEOUT, timeout=1800, startup_timeout=1800,
         max_containers=1)
class AceStepService:
    @modal.enter()
    def load_model(self):
        from acestep.handler import AceStepHandler
        from acestep.llm_inference import LLMHandler
        from acestep.model_downloader import ensure_lm_model, ensure_main_model

        model_cache.reload()
        ensure_main_model(checkpoints_dir=CHECKPOINTS)
        ensure_lm_model(model_name=LM_MODEL, checkpoints_dir=CHECKPOINTS)
        self.dit_handler = AceStepHandler()
        status, ready = self.dit_handler.initialize_service(
            project_root=PROJECT_ROOT, config_path="acestep-v15-turbo", device="cuda")
        if not ready:
            raise RuntimeError(f"ACE-Step DiT initialization failed: {status}")
        self.llm_handler = LLMHandler()
        status, ready = self.llm_handler.initialize(
            checkpoint_dir=CHECKPOINTS, lm_model_path=LM_MODEL,
            backend="vllm", device="cuda")
        if not ready:
            raise RuntimeError(f"ACE-Step language-model initialization failed: {status}")
        model_cache.commit()

    @modal.asgi_app()
    def web(self):
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import Response

        globals().update(Request=Request)
        api = FastAPI(title="TIMBOR ACE-Step 1.5", docs_url=None, redoc_url=None)

        def authenticate(request: Request) -> None:
            supplied = request.headers.get("authorization", "")
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:].strip()
            expected = os.environ.get("ACESTEP_API_KEY", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                raise HTTPException(status_code=401, detail="invalid API key")

        @api.get("/health")
        async def health(request: Request):
            authenticate(request)
            return {"service": "acestep", "status": "ok", "model": "acestep-1.5"}

        @api.post("/generate")
        async def generate(request: Request):
            authenticate(request)
            request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
            try:
                if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
                    form = await request.form()
                    doc = {key: form.get(key) for key in form.keys()}
                else:
                    doc = await request.json()
                prompt = str(doc.get("prompt", "")).strip()
                lyrics = str(doc.get("lyrics") or "[Instrumental]").strip()
                duration = float(doc.get("duration", 30))
                seed = int(doc.get("seed", -1))
                if not prompt or doc.get("model", "acestep-1.5") != "acestep-1.5":
                    raise HTTPException(status_code=400, detail="prompt and model=acestep-1.5 are required")
                if len(prompt) > 512 or len(lyrics) > 4096:
                    raise HTTPException(status_code=400, detail="ACE-Step prompt must be <=512 and lyrics <=4096 characters")
                if not 10 <= duration <= MAX_DURATION:
                    raise HTTPException(status_code=400, detail=f"duration must be 10..{MAX_DURATION:g} seconds")
                if seed < -1 or seed >= 2**32:
                    raise HTTPException(status_code=400, detail="seed must be -1 or a 32-bit unsigned integer")
                try:
                    bpm = float(doc["bpm"]) if doc.get("bpm") not in (None, "") else None
                except (ValueError, TypeError) as exc:
                    raise HTTPException(status_code=400, detail="BPM must be numeric") from exc
                if bpm is not None and (not 40 <= bpm <= 300):
                    raise HTTPException(status_code=400, detail="BPM must be between 40 and 300")
                from acestep.inference import GenerationConfig, GenerationParams, generate_music

                started = time.monotonic()
                params = GenerationParams(
                    caption=prompt, lyrics=lyrics,
                    bpm=int(bpm) if bpm is not None else None,
                    keyscale=str(doc.get("key") or ""), duration=duration,
                    seed=seed, thinking=True, shift=3.0)
                config = GenerationConfig(
                    batch_size=1, audio_format="wav", use_random_seed=seed < 0,
                    seeds=None if seed < 0 else [seed])

                with tempfile.TemporaryDirectory(prefix="timbor-acestep-") as output_dir:
                    result = generate_music(self.dit_handler, self.llm_handler, params,
                                            config, save_dir=output_dir)
                    if not result.success or not result.audios:
                        raise RuntimeError(result.error or "ACE-Step returned no audio")
                    audio_doc = result.audios[0]
                    audio = Path(audio_doc["path"]).read_bytes()
                    audio_seed = (audio_doc.get("params") or {}).get("seed", seed)
                    metadata = {"job_id": request_id, "model": "acestep-1.5",
                                "seed": audio_seed, "request_seed": seed,
                                "duration": 0.0,
                                "sample_rate": int(audio_doc.get("sample_rate", 48000)),
                                "channels": 2,
                                "generation_seconds": round(time.monotonic() - started, 3)}
                    # The WAV itself is authoritative for output duration.
                    import wave
                    with wave.open(str(audio_doc["path"]), "rb") as wav:
                        metadata["duration"] = wav.getnframes() / wav.getframerate()
                headers = {"X-Job-ID": request_id, "X-Model": "acestep-1.5",
                           "X-Seed": str(metadata["seed"]),
                           "X-Generation-Seconds": str(metadata["generation_seconds"]),
                           "X-Audio-Metadata": base64.b64encode(
                               json.dumps(metadata, separators=(",", ":")).encode()).decode()}
                return Response(audio, media_type="audio/wav", headers=headers)
            except HTTPException:
                raise
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=500, detail="ACE-Step generation failed") from exc

        return api
