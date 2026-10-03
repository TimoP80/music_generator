"""TIMBOR Ace Step 1.5 client — text-to-audio via the Ace Step 1.5 model.

This provider is an opt-in alternative output path; downloaded WAVs pass local
validation before they are exposed to the user.  The procedural TIMBOR backbone
remains unchanged; Ace Step renders the full stereo mix.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("timbor.ace_step_engine")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AceStepEngineError(RuntimeError):
    """A configuration, transport, or audio validation failure."""


def _normalize_ace_step_url(raw: str) -> str:
    """Return the Ace Step invocation base URL without a trailing route or slash."""
    url = raw.strip().rstrip("/")
    if url.endswith("/generate"):
        url = url[:-len("/generate")].rstrip("/")
    return url


def _load_project_env(target) -> None:
    """Load the project's ignored dotenv file for process-environment reads."""
    if target is not os.environ:
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not Path(env_path).is_file():
        return
    try:
        lines = Path(env_path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AceStepEngineError("could not read project .env configuration") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name.startswith("ACESTEP_") and not target.get(name):
            target[name] = value


SUPPORTED_MODELS = {"acestep-1.5"}
SUPPORTED_MODES = {"text-to-audio"}


@dataclass
class EngineConfig:
    enabled: bool = False
    modal_url: str = ""
    api_key: str = ""
    model: str = "acestep-1.5"
    default_duration: float = 30.0
    timeout: float = 1800.0
    max_retries: int = 0
    max_duration: float = 600.0
    sample_rate: int = 48000
    max_memory_mb: int = 32768
    max_cpu: float = 8.0
    gpu: str = "L40S"
    idle_timeout: int = 300

    @classmethod
    def from_env(cls, environ=None) -> "EngineConfig":
        import os
        env = os.environ if environ is None else environ
        _load_project_env(env)
        def number(name, default, cast):
            value = env.get(name)
            if value in (None, ""):
                return default
            try:
                return cast(value)
            except (ValueError, TypeError) as exc:
                raise AceStepEngineError(f"invalid {name}") from exc
        return cls(
            enabled=str(env.get("ACESTEP_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"},
            modal_url=_normalize_ace_step_url(str(env.get("ACESTEP_MODAL_URL", ""))),
            api_key=str(env.get("ACESTEP_API_KEY", "")),
            model=str(env.get("ACESTEP_MODEL", "acestep-1.5")).strip(),
            default_duration=number("ACESTEP_DEFAULT_DURATION", 30.0, float),
            timeout=number("ACESTEP_TIMEOUT", 1800.0, float),
            max_duration=number("ACESTEP_MAX_DURATION", 600.0, float),
            max_retries=number("ACESTEP_MAX_RETRIES", 0, int),
            max_memory_mb=number("ACESTEP_MEMORY", 32768, int),
            max_cpu=number("ACESTEP_CPU", 8.0, float),
            gpu=str(env.get("ACESTEP_GPU", "L40S")).strip(),
            idle_timeout=number("ACESTEP_IDLE_TIMEOUT", 300, int),
        )

    def validate(self) -> None:
        if self.enabled and not self.modal_url:
            raise AceStepEngineError("ACESTEP_MODAL_URL is required when Ace Step is enabled")
        if self.enabled and not self.api_key:
            raise AceStepEngineError("ACESTEP_API_KEY is required when Ace Step is enabled")
        if self.enabled and not self.modal_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise AceStepEngineError("ACESTEP_MODAL_URL must be HTTPS (or local loopback)")
        if "modal.com/apps/" in self.modal_url:
            raise AceStepEngineError(
                "ACESTEP_MODAL_URL must be the deployed Modal invocation endpoint, "
                "not the Modal dashboard page; run `modal deploy modal/ace_step_engine.py` and copy the "
                "*.modal.run URL it prints"
            )
        if self.timeout <= 0 or self.max_duration < 10 or self.default_duration < 10:
            raise AceStepEngineError("timeout must be positive and Ace Step duration must be at least 10 seconds")
        if self.sample_rate != 48000:
            raise AceStepEngineError("ACE-Step service output must be configured at 48000 Hz")
        if self.default_duration > self.max_duration:
            raise AceStepEngineError("default duration must not exceed maximum duration")
        if self.model not in SUPPORTED_MODELS:
            raise AceStepEngineError(f"unsupported model {self.model!r}; this service supports acestep-1.5 only")
        if not 0 <= self.max_retries <= 3:
            raise AceStepEngineError("ACESTEP_MAX_RETRIES must be between 0 and 3")
        if self.max_memory_mb < 1024 or self.max_cpu <= 0 or self.idle_timeout < 2:
            raise AceStepEngineError("invalid Modal CPU, memory, or idle timeout configuration")


@dataclass
class GenerationRequest:
    prompt: str
    duration: float
    seed: int = -1
    model: str | None = None
    negative_prompt: str | None = None
    genre: str | None = None
    era: str | None = None
    bpm: float | None = None
    key: str | None = None
    mood: str | None = None
    elements: list = field(default_factory=list)
    mode: str = "text-to-audio"
    audio_path: str | None = None
    sample_id: str | None = None
    strength: float | None = None
    inpaint_starts: list[float] | None = None
    inpaint_ends: list[float] | None = None
    lyrics: str | None = None

    def stable_prompt(self) -> str:
        core = self.prompt.strip()
        if not core:
            raise AceStepEngineError("prompt must not be empty")
        parts: list[str] = [core]
        context: list[str] = []
        if self.genre:
            context.append(f"{self.era + ' ' if self.era else ''}{self.genre} music")
        elif self.era:
            context.append(f"{self.era} era")
        if self.bpm is not None:
            context.append(f"{self.bpm:g} BPM")
        if self.key:
            context.append(f"in {self.key}")
        if self.mood:
            context.append(f"{self.mood} mood")
        if context:
            parts.append(", ".join(context))
        if self.elements:
            parts.append("featuring " + ", ".join(self.elements))
        return ". ".join(p for p in parts if p) + "."

    def validate(self, config: EngineConfig) -> None:
        model = self.model or config.model
        if model not in SUPPORTED_MODELS:
            raise AceStepEngineError(f"unsupported model {model!r}; this deployment supports acestep-1.5 only")
        if not math.isfinite(self.duration) or not 10 <= self.duration <= config.max_duration:
            raise AceStepEngineError(f"duration must be 10..{config.max_duration:g} seconds")
        stable_prompt = self.stable_prompt()
        if len(stable_prompt) > 512:
            raise AceStepEngineError("ACE-Step caption must be at most 512 characters")
        if self.mode not in SUPPORTED_MODES:
            raise AceStepEngineError(f"unsupported mode {self.mode!r}; ACE-Step currently supports text-to-audio only")
        if self.mode != "text-to-audio":
            raise AceStepEngineError("ACE-Step currently supports text-to-audio only")
        if self.audio_path or self.sample_id or self.inpaint_starts or self.inpaint_ends or self.strength is not None:
            raise AceStepEngineError("ACE-Step currently supports text-to-audio only; audio conditioning is unavailable")
        if (isinstance(self.seed, bool) or not isinstance(self.seed, int)
                or self.seed < -1 or self.seed >= 2**32):
            raise AceStepEngineError("seed must be -1 or a 32-bit unsigned integer")


@dataclass
class AudioDiagnostics:
    duration: float
    sample_rate: int
    channels: int
    bit_depth: int
    format_tag: int
    rms: float
    peak: float
    clipped_fraction: float
    silent_fraction: float
    samples: int

    def to_json(self) -> dict:
        return {**self.__dict__, "duration": round(self.duration, 4),
                "rms": round(self.rms, 8), "peak": round(self.peak, 8),
                "clipped_fraction": round(self.clipped_fraction, 8),
                "silent_fraction": round(self.silent_fraction, 6)}


def inspect_wav(payload: bytes, expected_duration: float | None = None,
                min_duration_ratio: float = 0.90) -> AudioDiagnostics:
    """Strict structural + signal validation of a complete generated WAV."""
    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise AceStepEngineError("generated output is not a RIFF/WAVE file")
    declared_size = struct.unpack_from("<I", payload, 4)[0] + 8
    if declared_size > len(payload) or declared_size < 44:
        raise AceStepEngineError("generated WAV is truncated or has an invalid RIFF length")
    offset, fmt, audio = 12, None, None
    limit = min(len(payload), declared_size)
    while offset + 8 <= limit:
        chunk_id, chunk_size = struct.unpack_from("<4sI", payload, offset)
        start, end = offset + 8, offset + 8 + chunk_size
        if end > limit:
            raise AceStepEngineError("generated WAV contains a truncated chunk")
        chunk = payload[start:end]
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise AceStepEngineError("generated WAV fmt chunk is too short")
            tag, channels, rate, _byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", chunk)
            fmt = (tag, channels, rate, block_align, bits)
        elif chunk_id == b"data":
            audio = chunk
        offset = end + (chunk_size & 1)
    if fmt is None or audio is None:
        raise AceStepEngineError("generated WAV is missing fmt or data chunk")
    tag, channels, rate, block_align, bits = fmt
    if tag not in (1, 3) or channels not in (1, 2) or rate < 8000 or block_align <= 0:
        raise AceStepEngineError(f"unsupported WAV encoding (tag={tag}, channels={channels}, rate={rate})")
    if tag == 1 and bits not in (8, 16, 24, 32) or tag == 3 and bits not in (32, 64):
        raise AceStepEngineError(f"unsupported WAV bit depth {bits}")
    if len(audio) % block_align:
        raise AceStepEngineError("generated WAV data is not frame aligned")
    frames = len(audio) // block_align
    if frames <= 0:
        raise AceStepEngineError("generated WAV contains no audio frames")
    duration = frames / rate
    if expected_duration is not None and duration < expected_duration * min_duration_ratio:
        raise AceStepEngineError(f"generated audio is truncated ({duration:.2f}s, expected {expected_duration:.2f}s)")
    import numpy as np
    if tag == 3:
        dtype = "<f4" if bits == 32 else "<f8"
        samples = np.frombuffer(audio, dtype=dtype).astype(np.float64)
        if not np.isfinite(samples).all():
            raise AceStepEngineError("generated WAV contains NaN or Inf samples")
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    elif bits == 8:
        samples = np.frombuffer(audio, dtype=np.uint8).astype(np.float64)
        samples = (samples - 128.0) / 128.0
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    elif bits == 16:
        samples = np.frombuffer(audio, dtype="<i2").astype(np.float64) / 32768.0
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    elif bits == 32:
        samples = np.frombuffer(audio, dtype="<i4").astype(np.float64) / float(1 << 31)
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    else:
        raw = np.frombuffer(audio, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = raw[:, 0] | raw[:, 1] << 8 | raw[:, 2] << 16
        values = np.where(values & 0x800000, values - (1 << 24), values)
        samples = values.astype(np.float64) / float(1 << 23)
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    if not math.isfinite(rms) or not math.isfinite(peak) or rms < 1e-5 or peak < 1e-4:
        raise AceStepEngineError(f"generated WAV is silent or near-silent (rms={rms:.3g}, peak={peak:.3g})")
    frame_samples = samples[:samples.size // channels * channels].reshape(-1, channels)
    silence_mask = np.abs(frame_samples) < 1e-4
    if frame_samples.size and float(np.mean(silence_mask)) >= 0.99:
        raise AceStepEngineError("generated WAV is effectively silent")
    return AudioDiagnostics(duration, rate, channels, bits, tag, rms, peak, clipped,
                            float(np.mean(silence_mask)) if frame_samples.size else 1.0,
                            int(samples.size))


class AceStepEngineProvider:
    """Synchronous HTTP provider for the Ace Step 1.5 service."""

    def __init__(self, config: EngineConfig | None = None, opener=None):
        self.config = config or EngineConfig.from_env()
        self.config.validate()
        self._open = opener or urllib.request.urlopen

    def _build_body(self, request: GenerationRequest) -> tuple[bytes, str, dict]:
        request.validate(self.config)
        if request.lyrics is not None and len(request.lyrics) > 4096:
            raise AceStepEngineError("ACE-Step lyrics must be at most 4096 characters")
        job_id = str(uuid.uuid4())
        fields: dict[str, Any] = {
            "job_id": job_id,
            "prompt": request.stable_prompt(),
            "lyrics": request.lyrics or "[Instrumental]",
            "duration": float(request.duration),
            "bpm": request.bpm,
            "key": request.key,
            "mood": request.mood,
            "genre": request.genre,
            "era": request.era,
            "model": request.model or self.config.model,
            "seed": int(request.seed),
            "mode": request.mode,
        }
        return json.dumps(fields, separators=(",", ":")).encode(), "application/json", {"job_id": job_id}

    @staticmethod
    def _header_value(headers, name: str):
        try:
            value = headers.get(name)
        except AttributeError:
            value = None
        if value:
            return value
        try:
            items = headers.items()
        except AttributeError:
            return None
        lower = name.lower()
        for key, value in items:
            if key.lower() == lower:
                return value
        return None

    def _decode_response(self, body: bytes, content_type: str,
                         response_headers) -> tuple[bytes, dict]:
        if content_type.lower().startswith(("audio/wav", "audio/x-wav")):
            encoded_metadata = self._header_value(response_headers, "X-Audio-Metadata")
            if not encoded_metadata:
                return body, {}
            try:
                import base64
                return body, json.loads(base64.b64decode(encoded_metadata, validate=True))
            except (ValueError, TypeError, json.JSONDecodeError):
                raise AceStepEngineError("Ace Step returned malformed audio metadata")
        try:
            doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AceStepEngineError("Ace Step returned neither a WAV nor valid JSON") from exc
        if not doc.get("success"):
            error = doc.get("error") or doc.get("detail") or "Ace Step generation failed"
            raise AceStepEngineError(str(error))
        metadata = {key: value for key, value in doc.items()
                    if key not in {"audio_base64", "audio_url"}}
        if "audio_base64" in doc:
            import base64
            try:
                return base64.b64decode(doc["audio_base64"], validate=True), metadata
            except (ValueError, TypeError) as exc:
                raise AceStepEngineError("Ace Step returned invalid base64 audio") from exc
        audio_url = doc.get("audio_url")
        if not audio_url:
            raise AceStepEngineError("Ace Step success response did not include audio")
        parsed = urllib.parse.urlparse(urllib.parse.urljoin(self.config.modal_url + "/", audio_url))
        base = urllib.parse.urlparse(self.config.modal_url)
        if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
            raise AceStepEngineError("Ace Step audio_url must point to the configured service")
        req = urllib.request.Request(parsed.geturl(),
                                     headers={"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {})
        with self._open(req, timeout=self.config.timeout) as response:
            return response.read(), metadata

    def generate(self, request: GenerationRequest) -> tuple[bytes, dict]:
        if not self.config.enabled:
            raise AceStepEngineError("Ace Step is disabled; set ACESTEP_ENABLED=true")
        if not self.config.modal_url:
            raise AceStepEngineError("set ACESTEP_MODAL_URL to your deployed Ace Step Modal endpoint")
        body, content_type, meta = self._build_body(request)
        job_id = meta["job_id"]
        headers = {"Content-Type": content_type, "Accept": "audio/wav, application/json",
                   "X-Request-ID": job_id}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        last_error = None
        start = time.monotonic()
        for attempt in range(self.config.max_retries + 1):
            req = urllib.request.Request(self.config.modal_url + "/generate", data=body, headers=headers, method="POST")
            logger.info(json.dumps({"event": "request_received", "request_id": job_id,
                                    "model": request.model or self.config.model,
                                    "duration": request.duration, "seed": request.seed,
                                    "mode": request.mode, "attempt": attempt + 1}))
            try:
                with self._open(req, timeout=self.config.timeout) as response:
                    response_body = response.read()
                    audio, metadata = self._decode_response(
                        response_body, response.headers.get("Content-Type", ""), response.headers)
                diagnostics = inspect_wav(audio, expected_duration=request.duration)
                if diagnostics.sample_rate != self.config.sample_rate:
                    raise AceStepEngineError(f"unexpected sample rate {diagnostics.sample_rate}; expected {self.config.sample_rate}")
                if diagnostics.channels != 2:
                    raise AceStepEngineError(f"unexpected channel count {diagnostics.channels}; expected stereo")
                elapsed = round(time.monotonic() - start, 3)
                service_metadata = metadata if isinstance(metadata, dict) else {}
                result = {"job_id": service_metadata.get("job_id", job_id),
                          "model": request.model or self.config.model,
                          "seed": service_metadata.get("seed", request.seed),
                          "prompt": request.stable_prompt(),
                          "duration": diagnostics.duration, "requested_duration": request.duration,
                          "sample_rate": diagnostics.sample_rate, "channels": diagnostics.channels,
                          "format": "wav", "audio_bytes": len(audio),
                          "total_seconds": elapsed, "diagnostics": diagnostics.to_json(),
                          "generation_seconds": service_metadata.get("generation_seconds"),
                          "metadata": service_metadata}
                logger.info(json.dumps({"event": "output_validation", "request_id": job_id,
                                        "ok": True, "duration": diagnostics.duration,
                                        "sample_rate": diagnostics.sample_rate,
                                        "rms": diagnostics.rms, "peak": diagnostics.peak,
                                        "generation_seconds": service_metadata.get("generation_seconds"),
                                        "total_seconds": elapsed, "output_bytes": len(audio)}))
                return audio, result
            except urllib.error.HTTPError as exc:
                body = exc.read(2048).decode(errors="replace")
                message = f"ACE STEP HTTP {exc.code}: {body}"
                if exc.code == 405:
                    message = f"{message} (Ace Step Modal URL hint)"
                elif "crash-loop" in body.lower() or "gated" in body.lower():
                    message = f"{message} (Ace Step container may be crash-looping; check Modal logs)"
                if exc.code not in (429, 502, 503, 504) or attempt >= self.config.max_retries:
                    raise AceStepEngineError(message) from exc
                last_error = AceStepEngineError(message)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                    detail = (f"ACE Step did not respond within "
                              f"{self.config.timeout:g}s and was not retried to "
                              f"avoid duplicate paid inference; "
                              f"check Modal container logs")
                else:
                    detail = (f"ACE Step could not be reached ({exc}); "
                              f"check Modal container logs")
                raise AceStepEngineError(detail) from exc
            except AceStepEngineError:
                raise
            delay = min(2 ** attempt, 8)
            logger.warning(json.dumps({"event": "retry_started", "request_id": job_id,
                                       "attempt": attempt + 2, "delay_seconds": delay,
                                       "error": str(last_error)}))
            time.sleep(delay)
        raise last_error or AceStepEngineError("generation failed")