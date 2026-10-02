"""Serverless Modal deployment for the official Stability AI Stable Audio 3.

Small Music uses the official CPU workflow unless STABLE_AUDIO_GPU is set.
Weights are cached in a persistent Modal Volume; a loaded model is reused for
requests in the same on-demand container.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import secrets
import stat
import struct
import tempfile
import time
import uuid
from pathlib import Path

import modal

LOG = logging.getLogger("stable_audio_3.modal")
MODEL = os.environ.get("STABLE_AUDIO_MODEL", "small-music").strip()
GPU = os.environ.get("STABLE_AUDIO_GPU", "").strip()
CPU = float(os.environ.get("STABLE_AUDIO_CPU", "4"))
MEMORY_MB = int(os.environ.get("STABLE_AUDIO_MEMORY", "8192"))
IDLE_TIMEOUT = int(os.environ.get("STABLE_AUDIO_IDLE_TIMEOUT", "60"))
MAX_DURATION = float(os.environ.get("STABLE_AUDIO_MAX_DURATION", "120"))
DEFAULT_DURATION = float(os.environ.get("STABLE_AUDIO_DEFAULT_DURATION", "30"))
MAX_UPLOAD_BYTES = int(os.environ.get("STABLE_AUDIO_MAX_UPLOAD_BYTES", str(32 * 1024 * 1024)))
HF_SECRET_NAME = os.environ.get("STABLE_AUDIO_HF_SECRET", "huggingface-token")
HF_HOME = "/cache/huggingface"

if MODEL != "small-music":
    raise ValueError("STABLE_AUDIO_MODEL must be small-music for this deployment")
if GPU:
    raise ValueError("this deployment currently supports CPU-only Small Music")

if not 1 <= CPU <= 64 or not 2048 <= MEMORY_MB <= 262144:
    raise ValueError("STABLE_AUDIO_CPU or STABLE_AUDIO_MEMORY is outside safe limits")
if (not 2 <= IDLE_TIMEOUT <= 1200 or not 1 <= MAX_DURATION <= 120
        or not 0 < DEFAULT_DURATION <= MAX_DURATION):
    raise ValueError("invalid Stable Audio idle timeout or duration configuration")
if not 1 <= MAX_UPLOAD_BYTES <= 256 * 1024 * 1024:
    raise ValueError("STABLE_AUDIO_MAX_UPLOAD_BYTES must be 1..256MiB")
# Pin the official repository to a reviewed commit, as opposed to installing an
# unrelated or mutable audio implementation. The checkpoint remains the official
# gated stabilityai/stable-audio-3-small-music HF repo.
OFFICIAL_COMMIT = "3a82c807b69cf4b7c5c05270011a5d5e47abac18"
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("uv")
    .run_commands(
        "uv pip install --system --index-url https://download.pytorch.org/whl/cpu torch==2.7.1 torchaudio==2.7.1",
        "uv pip install --system --no-deps "
        f"https://github.com/Stability-AI/stable-audio-3/archive/{OFFICIAL_COMMIT}.zip",
        "uv pip install --system 'einops>=0.8.2' 'einops-exts>=0.0.4' 'numpy>=2.2.6' 'packaging>=26.0' 'safetensors>=0.7.0' 'tqdm>=4.67.3' 'huggingface-hub>=1.7.1' 'transformers>=5.8.0' 'soundfile>=0.13.1' 'fastapi[standard]' python-multipart",
    )
    .env({"HF_HOME": HF_HOME, "HF_HUB_CACHE": f"{HF_HOME}/hub",
          "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONUNBUFFERED": "1"})
)


def _local_api_key() -> str:
    """Create or reuse the ignored local .env key without logging its value."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RuntimeError("could not read .env for Stable Audio credentials") from exc
        for line in lines:
            name, sep, value = line.partition("=")
            if sep and name.strip() == "STABLE_AUDIO_API_KEY" and value.strip():
                return value.strip().strip("\\\"'")
        raise RuntimeError(".env exists but STABLE_AUDIO_API_KEY is missing; add it without replacing other settings")

    api_key = secrets.token_urlsafe(32)
    try:
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(f"STABLE_AUDIO_API_KEY={api_key}\n")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            descriptor = wintypes.LPVOID()
            convert = ctypes.windll.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
            convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
            convert.restype = wintypes.BOOL
            if not convert("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
                raise OSError(ctypes.get_last_error(), "could not secure .env ACL")
            set_security = ctypes.windll.advapi32.SetFileSecurityW
            set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
            set_security.restype = wintypes.BOOL
            set_attributes = ctypes.windll.kernel32.SetFileAttributesW
            set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
            set_attributes.restype = wintypes.BOOL
            try:
                if not set_security(str(env_path), 0x00000004, descriptor):
                    raise OSError(ctypes.get_last_error(), "could not secure .env ACL")
                if not set_attributes(str(env_path), 0x00000002):
                    raise OSError(ctypes.get_last_error(), "could not secure .env ACL")
            finally:
                ctypes.windll.kernel32.LocalFree(descriptor)
    except FileExistsError:
        return _local_api_key()
    except OSError as exc:
        raise RuntimeError("could not securely create .env for Stable Audio credentials") from exc
    return api_key


app = modal.App("timbor-stable-audio-3")
model_cache = modal.Volume.from_name("timbor-stable-audio-model-cache", create_if_missing=True)
hf_secret = modal.Secret.from_name(HF_SECRET_NAME, required_keys=["HF_TOKEN"])
# Modal transmits this Secret object separately from the deployed source code.
# The credential is never logged or supplied as a CLI argument.
api_secret = modal.Secret.from_dict(
    {"STABLE_AUDIO_API_KEY": _local_api_key()} if modal.is_local() else {})


class ModelUnavailable(RuntimeError):
    """The service is up but its checkpoint never loaded."""


def _describe_load_failure(exc: BaseException) -> str:
    """Explain a checkpoint load failure without leaking the HF token.

    The dominant real-world failure is the gated checkpoint: the token in the
    Modal secret authenticates (so the response is 403, not 401) but the account
    has not been granted access to the repository.
    """
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "gatedrepo" in lowered or "gated repo" in lowered or "403" in lowered:
        return (
            "Hugging Face denied access to the gated model "
            "stabilityai/stable-audio-3-small-music (403). Accept its license on "
            "Hugging Face with the account whose token is stored in the Modal "
            f"'{HF_SECRET_NAME}' secret, then redeploy.")
    if "401" in lowered or "unauthorized" in lowered or "invalid" in lowered and "token" in lowered:
        return (
            f"Hugging Face rejected the token in the Modal '{HF_SECRET_NAME}' "
            "secret (401). Replace it with a valid read token, then redeploy.")
    return text[:500]


@app.cls(image=image, gpu=None, cpu=CPU, memory=MEMORY_MB,
         volumes={"/cache": model_cache}, secrets=[hf_secret, api_secret],
         scaledown_window=IDLE_TIMEOUT, timeout=max(600, int(MAX_DURATION * 10)),
         startup_timeout=1200, max_containers=4, single_use_containers=True)
class StableAudioService:
    @modal.enter()
    def load_model(self):
        import torch

        self.boot_started = time.monotonic()
        self.model = None
        self.device = "unloaded"
        self.model_load_seconds = None
        self.load_error = None
        if not os.environ.get("HF_TOKEN") or not os.environ.get("STABLE_AUDIO_API_KEY"):
            self.load_error = ("Modal HF_TOKEN and STABLE_AUDIO_API_KEY secrets must "
                               "be configured")
            LOG.error(json.dumps({"event": "model_load_failed", "model": MODEL,
                                  "error": self.load_error}))
            return
        try:
            from stable_audio_3 import StableAudioModel

            torch.set_num_threads(max(1, int(CPU)))
            device = "cuda" if torch.cuda.is_available() else "cpu"
            LOG.info(json.dumps({"event": "model_loading", "model": MODEL,
                                 "device": device, "gpu": GPU or "cpu"}))
            # Reload the latest committed model snapshot when an existing
            # container may have predated a different cold-start download.
            model_cache.reload()
            started = time.monotonic()
            self.model = StableAudioModel.from_pretrained(MODEL, device=device)
            self.model_load_seconds = round(time.monotonic() - started, 3)
            # Persist model downloads on cold start so subsequent containers reuse
            # the same HF cache snapshot rather than downloading the checkpoint.
            model_cache.commit()
            self.device = str(self.model.device)
            LOG.info(json.dumps({"event": "model_ready", "model": MODEL,
                                 "device": self.device,
                                 "model_load_seconds": self.model_load_seconds}))
        except Exception as exc:  # noqa: BLE001
            # A raising @modal.enter() restarts the container forever, so every
            # caller only ever sees a transport timeout with no explanation.
            # Record the reason and serve it over HTTP instead of crash-looping.
            self.load_error = _describe_load_failure(exc)
            LOG.exception(json.dumps({"event": "model_load_failed", "model": MODEL,
                                      "error": self.load_error}))

    def _generate(self, prompt, duration, seed, mode, model_name, negative_prompt,
                  strength, input_path, starts, ends):
        import numpy as np
        import soundfile as sf
        import torch

        request_started = time.monotonic()
        if self.model is None:
            raise ModelUnavailable(self.load_error or "model is not loaded")
        if model_name != MODEL:
            raise ValueError(f"deployment serves {MODEL!r}, not {model_name!r}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must not be empty")
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
            raise ValueError(f"duration must be > 0 and <= {MAX_DURATION:g} seconds")
        if mode not in {"text-to-audio", "audio-to-audio", "inpaint"}:
            raise ValueError("unsupported inference mode")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < -1 or seed >= 2**32:
            raise ValueError("seed must be -1 or a 32-bit unsigned integer")
        if mode in {"audio-to-audio", "inpaint"} and not input_path:
            raise ValueError(f"{mode} requires an input WAV")
        if mode == "audio-to-audio":
            strength = 0.9 if strength is None else float(strength)
            if not math.isfinite(strength) or not 0 <= strength <= 1:
                raise ValueError("audio strength must be in [0, 1]")
        if mode == "inpaint":
            if not starts or not ends or len(starts) != len(ends):
                raise ValueError("inpainting requires equally sized start and end masks")
            if any(not math.isfinite(float(a)) or not math.isfinite(float(b))
                   or a < 0 or b <= a or b > duration for a, b in zip(starts, ends)):
                raise ValueError("inpaint masks must satisfy 0 <= start < end <= duration")
        elif starts or ends:
            raise ValueError("inpaint masks are only valid in inpaint mode")

        effective_seed = seed if seed >= 0 else int.from_bytes(os.urandom(4), "little")
        call = {"prompt": prompt.strip(), "duration": duration,
                "seed": effective_seed}
        if negative_prompt:
            call["negative_prompt"] = str(negative_prompt)
        if mode in {"audio-to-audio", "inpaint"}:
            samples, source_rate = sf.read(input_path, dtype="float32", always_2d=True)
            input_audio = (int(source_rate), torch.from_numpy(
                np.ascontiguousarray(samples.T)))
        if mode == "audio-to-audio":
            call["init_audio"] = input_audio
            call["init_noise_level"] = strength
        elif mode == "inpaint":
            call["inpaint_audio"] = input_audio
            call["inpaint_mask_start_seconds"] = starts if len(starts) > 1 else starts[0]
            call["inpaint_mask_end_seconds"] = ends if len(ends) > 1 else ends[0]

        started = time.monotonic()
        with torch.inference_mode():
            generated = self.model.generate(**call)
        generation_seconds = round(time.monotonic() - started, 3)
        if not isinstance(generated, torch.Tensor) or generated.ndim != 3 or generated.shape[0] != 1:
            raise ValueError("official model returned an unexpected audio tensor shape")
        waveform = generated[0].detach().to(device="cpu", dtype=torch.float32).numpy()
        if waveform.shape[0] != 2:
            raise ValueError("official model did not return stereo audio")
        sample_rate = int(self.model.model.sample_rate)
        output = io.BytesIO()
        sf.write(output, waveform.T, sample_rate, format="WAV", subtype="PCM_16")
        audio = output.getvalue()
        diagnostics = _inspect_bytes(audio, duration)
        if diagnostics["sample_rate"] != 44100 or diagnostics["channels"] != 2:
            raise ValueError("official model output must be 44.1 kHz stereo")
        metadata = {"job_id": str(uuid.uuid4()), "model": model_name,
                    "seed": effective_seed, "request_seed": seed, "mode": mode,
                    "duration": diagnostics["duration"],
                    "sample_rate": sample_rate, "channels": 2,
                    "audio_bytes": len(audio), "generation_seconds": generation_seconds,
                    "total_seconds": round(time.monotonic() - request_started, 3),
                    "diagnostics": diagnostics}
        LOG.info(json.dumps({"event": "generation_complete", **metadata}))
        return audio, metadata

    @modal.asgi_app()
    def web(self):
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse, Response

        # `from __future__ import annotations` turns `request: Request` into the
        # string "Request", and FastAPI resolves string annotations against the
        # handler's module globals. `Request` is imported here, not at module
        # scope, so without publishing it every route would treat `request` as a
        # required query parameter and answer 422 instead of running.
        globals().update(Request=Request)

        http_app = FastAPI(title="TIMBOR Stable Audio 3", docs_url=None, redoc_url=None)

        def authenticate(request: Request):
            import hmac
            supplied = request.headers.get("authorization", "")
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:].strip()
            expected = os.environ.get("STABLE_AUDIO_API_KEY", "")
            if not expected or not hmac.compare_digest(supplied, expected):
                raise HTTPException(status_code=401, detail="invalid API key")

        @http_app.get("/health")
        async def health(request: Request):
            authenticate(request)
            if self.model is None:
                return JSONResponse(
                    {"service": "stable-audio-3", "status": "unavailable",
                     "error": self.load_error or "model is not loaded"},
                    status_code=503)
            return {"service": "stable-audio-3", "status": "ok"}

        @http_app.get("/diagnostic")
        async def diagnostic(request: Request):
            authenticate(request)
            import torch
            loaded = self.model is not None
            return JSONResponse({
                "service": "stable-audio-3",
                "status": "ready" if loaded else "unavailable",
                "model": MODEL, "device": self.device, "cuda": torch.version.cuda,
                "cuda_available": torch.cuda.is_available(), "model_loaded": loaded,
                "model_load_seconds": self.model_load_seconds,
                "model_load_error": self.load_error,
                "container_uptime_seconds": round(time.monotonic() - self.boot_started, 3),
                "cold_start_seconds": round(time.monotonic() - self.boot_started, 3),
                "max_duration": MAX_DURATION,
                "sample_rate": int(self.model.model.sample_rate) if loaded else None,
                "gpu": "cpu",
                "idle_timeout_seconds": IDLE_TIMEOUT,
            })

        @http_app.post("/generate")
        async def generate(request: Request):
            authenticate(request)
            request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
            input_path = None
            try:
                if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
                    form = await request.form()

                    def field(name, default=None):
                        value = form.get(name)
                        return str(value) if value is not None else default

                    prompt, duration = field("prompt", ""), float(field("duration", DEFAULT_DURATION))
                    seed, model_name = int(field("seed", -1)), field("model", MODEL)
                    mode, negative_prompt = field("mode", "text-to-audio"), field("negative_prompt")
                    raw_strength = field("strength")
                    strength = float(raw_strength) if raw_strength not in (None, "") else None
                    try:
                        starts = json.loads(field("inpaint_starts", "null"))
                        ends = json.loads(field("inpaint_ends", "null"))
                    except (ValueError, TypeError) as exc:
                        raise HTTPException(status_code=400, detail="inpaint ranges must be JSON") from exc
                    upload = form.get("audio")
                    if upload is not None:
                        if not hasattr(upload, "read") or not hasattr(upload, "filename"):
                            raise HTTPException(status_code=400, detail="audio must be a WAV upload")
                        raw_audio = await upload.read(MAX_UPLOAD_BYTES + 1)
                        await upload.close()
                        if len(raw_audio) > MAX_UPLOAD_BYTES:
                            raise HTTPException(status_code=413, detail="input WAV exceeds upload limit")
                        _inspect_bytes(raw_audio, None)
                        with tempfile.NamedTemporaryFile(prefix="stable-audio-input-", suffix=".wav", delete=False) as handle:
                            handle.write(raw_audio)
                            input_path = handle.name
                else:
                    doc = await request.json()
                    prompt, duration = doc.get("prompt", ""), float(doc.get("duration", DEFAULT_DURATION))
                    seed, model_name = int(doc.get("seed", -1)), doc.get("model", MODEL)
                    mode, negative_prompt = doc.get("mode", "text-to-audio"), doc.get("negative_prompt")
                    strength = doc.get("strength")
                    starts, ends = doc.get("inpaint_starts"), doc.get("inpaint_ends")
                starts = [starts] if starts is not None and not isinstance(starts, list) else starts
                ends = [ends] if ends is not None and not isinstance(ends, list) else ends
                if any(not math.isfinite(float(v)) for v in (starts or []) + (ends or [])):
                    raise HTTPException(status_code=400, detail="inpaint ranges must be finite")
                LOG.info(json.dumps({"event": "request_received", "request_id": request_id,
                                     "model": model_name, "duration": duration, "seed": seed,
                                     "mode": mode, "input_audio": input_path is not None}))
                audio, metadata = self._generate(
                    prompt, duration, seed, mode, model_name, negative_prompt,
                    strength, input_path, starts, ends)
                metadata["request_id"] = request_id
                headers = {"X-Job-ID": metadata["job_id"], "X-Model": model_name,
                           "X-Seed": str(metadata["seed"]),
                           "X-Generation-Seconds": str(metadata["generation_seconds"]),
                           "X-Total-Seconds": str(metadata["total_seconds"]),
                           "X-Output-Bytes": str(len(audio)),
                           "X-Audio-Metadata": base64.b64encode(
                               json.dumps(metadata, separators=(",", ":")).encode()).decode()}
                return Response(audio, media_type="audio/wav", headers=headers)
            except HTTPException:
                raise
            except ModelUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                LOG.exception(json.dumps({"event": "generation_failed", "request_id": request_id,
                                          "model": MODEL, "error_category": "inference_error",
                                          "error": str(exc)}))
                raise HTTPException(status_code=500, detail="Stable Audio inference failed") from exc
            finally:
                if input_path:
                    try:
                        os.remove(input_path)
                    except OSError:
                        pass

        return http_app


def _inspect_bytes(payload: bytes, expected_duration: float | None) -> dict:
    """Validate WAV structure and signal before return or use as conditioning."""
    import numpy as np

    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise ValueError("audio is not a RIFF/WAVE file")
    declared = struct.unpack_from("<I", payload, 4)[0] + 8
    if declared > len(payload):
        raise ValueError("WAV is truncated")
    pos, fmt, raw = 12, None, None
    while pos + 8 <= min(declared, len(payload)):
        cid, size = struct.unpack_from("<4sI", payload, pos)
        start, end = pos + 8, pos + 8 + size
        if end > len(payload):
            raise ValueError("WAV chunk is truncated")
        if cid == b"fmt ":
            if size < 16:
                raise ValueError("invalid WAV fmt chunk")
            fmt = struct.unpack_from("<HHIIHH", payload, start)
        elif cid == b"data":
            raw = payload[start:end]
        pos = end + (size & 1)
    if fmt is None or raw is None:
        raise ValueError("WAV requires fmt and data chunks")
    tag, channels, rate, _bps, align, bits = fmt
    if tag not in (1, 3) or channels not in (1, 2) or rate < 8000 or align < 1:
        raise ValueError("unsupported WAV encoding")
    frames = len(raw) // align
    if frames < 1 or len(raw) % align:
        raise ValueError("WAV contains no complete frames")
    duration = frames / rate
    if expected_duration and duration < expected_duration * 0.9:
        raise ValueError("WAV shorter than requested duration")
    if tag == 3:
        x = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8").astype(np.float64)
    elif bits == 8:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
    elif bits == 16:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768
    elif bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | b[:, 1] << 8 | b[:, 2] << 16
        v = np.where(v & 0x800000, v - (1 << 24), v)
        x = v.astype(np.float64) / float(1 << 23)
    elif bits == 32:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
    else:
        raise ValueError(f"unsupported WAV bit depth {bits}")
    if not np.isfinite(x).all():
        raise ValueError("WAV contains NaN or Inf")
    peak, rms = float(np.max(np.abs(x))), float(np.sqrt(np.mean(x * x)))
    if rms < 1e-5 or peak < 1e-4:
        raise ValueError("WAV is silent or near-silent")
    frame = x[:x.size // channels * channels].reshape(-1, channels)
    silent = float(np.mean(np.sqrt(np.mean(frame * frame, axis=1)) < 1e-4))
    if silent >= 0.99:
        raise ValueError("WAV is effectively silent")
    return {"duration": round(duration, 4), "sample_rate": rate,
            "channels": channels, "bit_depth": bits, "format_tag": tag,
            "rms": round(rms, 8), "peak": round(peak, 8),
            "clipped_fraction": round(float(np.mean(np.abs(x) >= 0.999)), 8),
            "silent_fraction": round(silent, 6), "samples": int(x.size)}
