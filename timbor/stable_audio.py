"""TIMBOR client for the official Stable Audio 3 Modal inference service.

The existing procedural renderer and project/timeline pipeline are unchanged.
This provider is an opt-in alternative output path; downloaded WAVs pass local
validation before they are exposed to the user.
"""
from __future__ import annotations

import io
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

logger = logging.getLogger("timbor.stable_audio")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StableAudioError(RuntimeError):
    """A configuration, transport, service, or audio validation failure."""


def _load_project_env(target) -> None:
    """Load the project's ignored dotenv file (stdlib only).

    The project ``.env`` is the authoritative configuration surface for Stable
    Audio: the deploy tooling writes the credential there and the README directs
    users to set the endpoint there. Its values therefore win over an inherited
    process environment variable, which otherwise silently shadows a corrected
    endpoint and makes a 405 look persistent.
    """
    if target is not os.environ and isinstance(target, dict):
        # A supplied mapping is an explicit, isolated test/config surface.
        return
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StableAudioError("could not read project .env configuration") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[name] = value


def _header_value(headers, name: str):
    """Case-insensitive header lookup for HTTPMessage and plain mappings.

    urllib exposes response headers as a case-insensitive ``HTTPMessage``, but
    tests and proxies may hand back a plain ``dict``; both are supported so the
    service metadata header is never silently dropped because of its casing.
    """
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


SUPPORTED_MODELS = {"small-music"}
SUPPORTED_MODES = {"text-to-audio", "audio-to-audio", "inpaint"}
_MAX_SAMPLE_BYTES = 32 * 1024 * 1024
_MODAL_INVOCATION_HINT = (
    "STABLE_AUDIO_MODAL_URL must be the deployed Modal invocation endpoint "
    "(https://<workspace>--<app>-<class>-<method>.modal.run), not the Modal "
    "dashboard page; run `modal deploy modal/stable_audio.py` and copy the "
    "*.modal.run URL it prints")
# A deployed-but-unhealthy service answers every request with a transport
# timeout or a crash-loop 5xx because @modal.enter() never finishes. The
# dominant cause is the gated checkpoint, so name it instead of returning a
# bare "timed out" that reads as an unexplained failure.
_MODAL_CRASH_LOOP_HINT = (
    "the Modal container is probably crash-looping (run `modal app logs`); "
    "the usual cause is that the Hugging Face token in the Modal "
    "'huggingface-token' secret lacks access to the gated model "
    "stabilityai/stable-audio-3-small-music (403 GatedRepoError) -- accept its "
    "license on Hugging Face, then update the secret and redeploy")


def _normalize_modal_url(raw: str) -> str:
    """Return the Modal invocation base URL without a trailing route or slash.

    Users frequently paste the full ``.../generate`` route or a trailing slash;
    the client appends ``/generate`` itself, so either would double the path and
    produce a 404/405.
    """
    url = raw.strip().rstrip("/")
    if url.endswith("/generate"):
        url = url[: -len("/generate")].rstrip("/")
    return url


@dataclass
class StableAudioConfig:
    enabled: bool = False
    provider: str = "modal"
    modal_url: str = ""
    api_key: str = ""
    model: str = "small-music"
    default_duration: float = 45.0
    timeout: float = 600.0
    max_duration: float = 120.0
    max_retries: int = 1
    sample_rate: int = 44100
    max_audio_upload_bytes: int = _MAX_SAMPLE_BYTES
    max_memory_mb: int = 8192
    max_cpu: float = 4.0
    gpu: str = ""
    gpu_memory: str = ""
    idle_timeout: int = 60
    hf_secret_name: str = "huggingface-token"

    @classmethod
    def from_env(cls, environ=None) -> "StableAudioConfig":
        env = os.environ if environ is None else environ
        _load_project_env(env)

        def number(name, default, cast):
            value = env.get(name)
            if value in (None, ""):
                return default
            try:
                return cast(value)
            except (ValueError, TypeError) as exc:
                raise StableAudioError(f"invalid {name}") from exc

        gpu = str(env.get("STABLE_AUDIO_GPU", "")).strip()
        gpu_memory = str(env.get("STABLE_AUDIO_GPU_MEMORY", "")).strip()
        return cls(
            enabled=str(env.get("STABLE_AUDIO_ENABLED", "false")).strip().lower()
                    in {"1", "true", "yes", "on"},
            provider=str(env.get("STABLE_AUDIO_PROVIDER", "modal")).strip().lower(),
            modal_url=_normalize_modal_url(str(env.get("STABLE_AUDIO_MODAL_URL", ""))),
            api_key=str(env.get("STABLE_AUDIO_API_KEY", "")),
            model=str(env.get("STABLE_AUDIO_MODEL", "small-music")).strip(),
            default_duration=number("STABLE_AUDIO_DEFAULT_DURATION", 30.0, float),
            timeout=number("STABLE_AUDIO_TIMEOUT", 600.0, float),
            max_duration=number("STABLE_AUDIO_MAX_DURATION", 120.0, float),
            max_retries=number("STABLE_AUDIO_MAX_RETRIES", 1, int),
            max_audio_upload_bytes=number("STABLE_AUDIO_MAX_UPLOAD_BYTES", _MAX_SAMPLE_BYTES, int),
            max_memory_mb=number("STABLE_AUDIO_MEMORY", 8192, int),
            max_cpu=number("STABLE_AUDIO_CPU", 4.0, float),
            gpu=gpu, gpu_memory=gpu_memory,
            idle_timeout=number("STABLE_AUDIO_IDLE_TIMEOUT", 60, int),
            hf_secret_name=str(env.get("STABLE_AUDIO_HF_SECRET", "huggingface-token")).strip(),
        )

    def validate(self) -> None:
        if self.provider != "modal":
            raise StableAudioError("only STABLE_AUDIO_PROVIDER=modal is currently implemented")
        if self.model not in SUPPORTED_MODELS:
            raise StableAudioError(f"unsupported model {self.model!r}; this deployment supports small-music only")
        if self.gpu:
            raise StableAudioError("this deployment currently supports CPU-only Small Music; STABLE_AUDIO_GPU must be empty")
        if self.enabled and not self.modal_url:
            raise StableAudioError("STABLE_AUDIO_MODAL_URL is required when Stable Audio is enabled")
        if self.enabled and not self.api_key:
            raise StableAudioError("STABLE_AUDIO_API_KEY is required when Stable Audio is enabled")
        if self.modal_url and not self.modal_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise StableAudioError("STABLE_AUDIO_MODAL_URL must be HTTPS (or local loopback)")
        if "modal.com/apps/" in self.modal_url:
            raise StableAudioError(_MODAL_INVOCATION_HINT)
        if (self.timeout <= 0 or self.max_duration <= 0 or self.default_duration <= 0
                or self.default_duration > self.max_duration):
            raise StableAudioError("timeout and duration limits must be positive and default duration must fit the maximum")
        if not 0 <= self.max_retries <= 3:
            raise StableAudioError("STABLE_AUDIO_MAX_RETRIES must be between 0 and 3")
        if not 1024 <= self.max_audio_upload_bytes <= 256 * 1024 * 1024:
            raise StableAudioError("STABLE_AUDIO_MAX_UPLOAD_BYTES must be 1KiB..256MiB")
        if self.gpu_memory:
            logger.warning("STABLE_AUDIO_GPU_MEMORY is advisory; this deployment has no GPU")
        if self.max_memory_mb < 1024 or self.max_cpu <= 0 or self.idle_timeout < 2:
            raise StableAudioError("invalid Modal CPU, memory, or idle timeout configuration")


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
    elements: list[str] = field(default_factory=list)
    mode: str = "text-to-audio"
    audio_path: str | None = None
    sample_id: str | None = None
    strength: float | None = None
    inpaint_starts: list[float] | None = None
    inpaint_ends: list[float] | None = None
    lyrics: str | None = None

    def stable_prompt(self) -> str:
        """Preserve TIMBOR musical context while composing a model prompt.

        The prompt is structured so that Stable Audio 3 receives a concise yet
        descriptive brief: original user prompt + genre/era + BPM + key + mood,
        with elements appended if present.  Lyrics are prepended to the prompt
        when provided.  Empty or whitespace-only prompts raise a validation error.
        """
        core = self.prompt.strip()
        if not core:
            raise StableAudioError("prompt must not be empty")
        parts: list[str] = []
        if self.lyrics:
            parts.append(self.lyrics.strip())
        parts.append(core)
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

    def validate(self, config: StableAudioConfig) -> None:
        model = self.model or config.model
        if model not in SUPPORTED_MODELS:
            raise StableAudioError(f"unsupported model {model!r}; this deployment supports small-music")
        if not math.isfinite(self.duration) or not 0 < self.duration <= config.max_duration:
            raise StableAudioError(f"duration must be > 0 and <= {config.max_duration:g} seconds")
        if not self.stable_prompt():
            raise StableAudioError("prompt must not be empty")
        if self.mode not in SUPPORTED_MODES:
            raise StableAudioError(f"unsupported mode {self.mode!r}")
        if self.audio_path and self.sample_id:
            raise StableAudioError("choose either --audio-input or --audio-sample-id, not both")
        if self.mode == "text-to-audio" and (self.audio_path or self.sample_id):
            raise StableAudioError("conditioning audio requires audio-to-audio or inpaint mode")
        if self.mode == "audio-to-audio" and not (self.audio_path or self.sample_id):
            raise StableAudioError("audio-to-audio requires input audio or a sample-library sample_id")
        if self.mode == "inpaint":
            if not (self.audio_path or self.sample_id):
                raise StableAudioError("inpainting/continuation requires input audio")
            if not self.inpaint_starts or not self.inpaint_ends or len(self.inpaint_starts) != len(self.inpaint_ends):
                raise StableAudioError("inpainting requires equally sized inpaint_starts and inpaint_ends")
            if any(not math.isfinite(a) or not math.isfinite(b) or a < 0 or b <= a
                   or b > self.duration for a, b in zip(self.inpaint_starts, self.inpaint_ends)):
                raise StableAudioError("inpaint ranges must have finite 0 <= start < end <= duration")
        elif self.inpaint_starts or self.inpaint_ends:
            raise StableAudioError("inpaint ranges require --stable-audio-mode inpaint")
        if self.strength is not None and not 0 <= self.strength <= 1:
            raise StableAudioError("strength must be in [0, 1]")
        if self.strength is not None and self.mode != "audio-to-audio":
            raise StableAudioError("audio strength is only valid in audio-to-audio mode")
        if (isinstance(self.seed, bool) or not isinstance(self.seed, int)
                or self.seed < -1 or self.seed >= 2**32):
            raise StableAudioError("seed must be -1 or a 32-bit unsigned integer")


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
        raise StableAudioError("generated output is not a RIFF/WAVE file")
    declared_size = struct.unpack_from("<I", payload, 4)[0] + 8
    if declared_size > len(payload) or declared_size < 44:
        raise StableAudioError("generated WAV is truncated or has an invalid RIFF length")
    offset, fmt, audio = 12, None, None
    limit = min(len(payload), declared_size)
    while offset + 8 <= limit:
        chunk_id, chunk_size = struct.unpack_from("<4sI", payload, offset)
        start, end = offset + 8, offset + 8 + chunk_size
        if end > limit:
            raise StableAudioError("generated WAV contains a truncated chunk")
        chunk = payload[start:end]
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise StableAudioError("generated WAV fmt chunk is too short")
            tag, channels, rate, _byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", chunk)
            fmt = (tag, channels, rate, block_align, bits)
        elif chunk_id == b"data":
            audio = chunk
        offset = end + (chunk_size & 1)
    if fmt is None or audio is None:
        raise StableAudioError("generated WAV is missing fmt or data chunk")
    tag, channels, rate, block_align, bits = fmt
    if tag not in (1, 3) or channels not in (1, 2) or rate < 8000 or block_align <= 0:
        raise StableAudioError(f"unsupported WAV encoding (tag={tag}, channels={channels}, rate={rate})")
    if tag == 1 and bits not in (8, 16, 24, 32) or tag == 3 and bits not in (32, 64):
        raise StableAudioError(f"unsupported WAV bit depth {bits}")
    if len(audio) % block_align:
        raise StableAudioError("generated WAV data is not frame aligned")
    frames = len(audio) // block_align
    if frames <= 0:
        raise StableAudioError("generated WAV contains no audio frames")
    duration = frames / rate
    if expected_duration is not None and duration < expected_duration * min_duration_ratio:
        raise StableAudioError(f"generated audio is truncated ({duration:.2f}s, expected {expected_duration:.2f}s)")
    if tag == 3:
        import numpy as np
        dtype = "<f4" if bits == 32 else "<f8"
        samples = np.frombuffer(audio, dtype=dtype).astype(np.float64)
        if not np.isfinite(samples).all():
            raise StableAudioError("generated WAV contains NaN or Inf samples")
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    elif bits == 8:
        import numpy as np
        samples = np.frombuffer(audio, dtype=np.uint8).astype(np.float64)
        samples = (samples - 128.0) / 128.0
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    elif bits == 16:
        import numpy as np
        samples = np.frombuffer(audio, dtype="<i2").astype(np.float64) / 32768.0
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    elif bits == 32:
        import numpy as np
        samples = np.frombuffer(audio, dtype="<i4").astype(np.float64) / float(1 << 31)
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    else:
        import numpy as np
        raw = np.frombuffer(audio, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = raw[:, 0] | raw[:, 1] << 8 | raw[:, 2] << 16
        values = np.where(values & 0x800000, values - (1 << 24), values)
        samples = values.astype(np.float64) / float(1 << 23)
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipped = float(np.mean(np.abs(samples) >= 0.999))
    if not math.isfinite(rms) or not math.isfinite(peak) or rms < 1e-5 or peak < 1e-4:
        raise StableAudioError(f"generated WAV is silent or near-silent (rms={rms:.3g}, peak={peak:.3g})")
    import numpy as np
    frame_samples = samples[:samples.size // channels * channels].reshape(-1, channels)
    frame_rms = np.sqrt(np.mean(frame_samples.astype(np.float64) ** 2, axis=1))
    if frame_rms.size and float(np.mean(frame_rms < 1e-4)) >= 0.99:
        raise StableAudioError("generated WAV is effectively silent")
    return AudioDiagnostics(duration, rate, channels, bits, tag, rms, peak, clipped,
                            float(np.mean(frame_rms < 1e-4)) if frame_rms.size else 1.0,
                            int(samples.size))


def _read_sample(path: str, max_bytes: int) -> tuple[str, bytes, float]:
    if not path:
        raise StableAudioError("audio path is required")
    full = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.isfile(full):
        raise StableAudioError(f"input audio file does not exist: {path}")
    size = os.path.getsize(full)
    if size > max_bytes:
        raise StableAudioError(f"input audio exceeds STABLE_AUDIO_MAX_UPLOAD_BYTES ({size} > {max_bytes})")
    with open(full, "rb") as source:
        # Read only the bytes the file actually holds: allocating the full
        # upload limit up front fails on memory-constrained hosts even for a
        # tiny conditioning clip. One extra byte still detects a file that grew
        # past the limit between getsize() and read().
        payload = source.read(size + 1)
    if len(payload) > max_bytes:
        raise StableAudioError("input audio exceeded the configured upload limit")
    info = inspect_wav(payload)
    return os.path.basename(full), payload, info.duration


class StableAudioProvider:
    """Synchronous HTTP provider for the Modal Stable Audio service."""

    def __init__(self, config: StableAudioConfig | None = None,
                 opener=None, sample_resolver=None):
        self.config = config or StableAudioConfig.from_env()
        self.config.validate()
        self._open = opener or urllib.request.urlopen
        self._sample_resolver = sample_resolver

    def _resolve_input(self, request: GenerationRequest) -> tuple[str, bytes, float] | None:
        path = request.audio_path
        if request.sample_id:
            if not self._sample_resolver:
                raise StableAudioError("sample_id requires a configured TIMBOR sample-library resolver")
            try:
                resolved = self._sample_resolver(request.sample_id)
            except (KeyError, ValueError, FileNotFoundError) as exc:
                raise StableAudioError("sample ID is not valid or no longer available in TIMBOR's sample library") from exc
            path = resolved[0] if isinstance(resolved, tuple) else resolved
        return _read_sample(path, self.config.max_audio_upload_bytes) if path else None

    def _build_body(self, request: GenerationRequest) -> tuple[bytes, str, dict]:
        request.validate(self.config)
        job_id = str(uuid.uuid4())
        fields: dict[str, Any] = {
            "job_id": job_id,
            "prompt": request.stable_prompt(),
            "negative_prompt": request.negative_prompt,
            "duration": float(request.duration),
            "model": request.model or self.config.model,
            "seed": int(request.seed),
            "mode": request.mode,
        }
        if request.mode == "audio-to-audio":
            fields["strength"] = request.strength if request.strength is not None else 0.9
        if request.mode == "inpaint":
            fields["inpaint_starts"] = request.inpaint_starts
            fields["inpaint_ends"] = request.inpaint_ends
        source = self._resolve_input(request)
        if source is None:
            return json.dumps(fields, separators=(",", ":")).encode(), "application/json", {"job_id": job_id}
        filename, audio, source_duration = source
        if request.mode == "inpaint" and source_duration > request.duration + 1e-3:
            raise StableAudioError("inpaint output duration must not truncate the source audio")
        if request.mode == "inpaint" and any(end > request.duration + 1e-3 for end in request.inpaint_ends or []):
            raise StableAudioError("inpaint masks must end within the requested output duration")
        boundary = "timbor-" + uuid.uuid4().hex
        parts = []
        def field_part(name, value):
            if value is None:
                return
            parts.extend((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
                          str(value).encode(), b"\r\n"))
        field_part("job_id", job_id)
        field_part("prompt", fields["prompt"])
        field_part("negative_prompt", fields["negative_prompt"])
        field_part("duration", fields["duration"])
        field_part("model", fields["model"])
        field_part("seed", fields["seed"])
        field_part("mode", fields["mode"])
        if request.mode == "audio-to-audio":
            field_part("strength", fields["strength"])
        if request.mode == "inpaint":
            field_part("inpaint_starts", json.dumps(fields["inpaint_starts"]))
            field_part("inpaint_ends", json.dumps(fields["inpaint_ends"]))
        parts.extend((f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"input.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode(),
                      audio, b"\r\n", f"--{boundary}--\r\n".encode()))
        return b"".join(parts), f"multipart/form-data; boundary={boundary}", {"job_id": job_id}

    def _decode_response(self, body: bytes, content_type: str,
                         response_headers) -> tuple[bytes, dict]:
        if content_type.lower().startswith(("audio/wav", "audio/x-wav")):
            encoded_metadata = _header_value(response_headers, "X-Audio-Metadata")
            if not encoded_metadata:
                return body, {}
            import base64
            try:
                return body, json.loads(base64.b64decode(encoded_metadata, validate=True))
            except (ValueError, TypeError, json.JSONDecodeError):
                raise StableAudioError("Modal returned malformed audio metadata")
        try:
            doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise StableAudioError("Modal returned neither a WAV nor valid JSON") from exc
        if not doc.get("success"):
            error = doc.get("error") or doc.get("detail") or "Modal generation failed"
            raise StableAudioError(str(error))
        # The audio is transported out-of-band (base64 or a URL); never embed the
        # payload or a transient URL in the metadata callers persist next to the WAV.
        metadata = {key: value for key, value in doc.items()
                    if key not in {"audio_base64", "audio_url"}}
        if "audio_base64" in doc:
            import base64
            try:
                return base64.b64decode(doc["audio_base64"], validate=True), metadata
            except (ValueError, TypeError) as exc:
                raise StableAudioError("Modal returned invalid base64 audio") from exc
        audio_url = doc.get("audio_url")
        if not audio_url:
            raise StableAudioError("Modal success response did not include audio")
        parsed = urllib.parse.urlparse(urllib.parse.urljoin(self.config.modal_url + "/", audio_url))
        base = urllib.parse.urlparse(self.config.modal_url)
        if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
            raise StableAudioError("Modal audio_url must point to the configured service")
        req = urllib.request.Request(parsed.geturl(), headers={"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {})
        with self._open(req, timeout=self.config.timeout) as response:
            return response.read(), metadata

    def generate(self, request: GenerationRequest) -> tuple[bytes, dict]:
        if not self.config.enabled:
            raise StableAudioError("Stable Audio is disabled; set STABLE_AUDIO_ENABLED=true")
        if not self.config.modal_url:
            raise StableAudioError("set STABLE_AUDIO_MODAL_URL to your deployed Modal endpoint")
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
                    raise StableAudioError(f"unexpected sample rate {diagnostics.sample_rate}; expected {self.config.sample_rate}")
                if diagnostics.channels != 2:
                    raise StableAudioError(f"unexpected channel count {diagnostics.channels}; expected stereo")
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
                message = f"Modal HTTP {exc.code}: {body}"
                if exc.code == 405:
                    message = f"{message} ({_MODAL_INVOCATION_HINT})"
                elif "crash-loop" in body.lower() or "gated" in body.lower():
                    message = f"{message} ({_MODAL_CRASH_LOOP_HINT})"
                if exc.code not in (429, 502, 503, 504) or attempt >= self.config.max_retries:
                    raise StableAudioError(message) from exc
                last_error = StableAudioError(message)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # A connection timeout can occur after inference has started;
                # retrying it could pay for a duplicate generation.
                if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                    detail = (f"Modal did not respond within "
                              f"{self.config.timeout:g}s and was not retried to "
                              f"avoid duplicate paid inference; "
                              f"{_MODAL_CRASH_LOOP_HINT}")
                else:
                    detail = (f"Modal could not be reached ({exc}); "
                              f"{_MODAL_CRASH_LOOP_HINT}")
                raise StableAudioError(detail) from exc
            except StableAudioError:
                # Once the service has returned a response, any service error or
                # local validation failure is terminal; retrying could duplicate
                # a paid inference without improving the audio's validity.
                raise
            delay = min(2 ** attempt, 8)
            logger.warning(json.dumps({"event": "retry_started", "request_id": job_id,
                                       "attempt": attempt + 2, "delay_seconds": delay,
                                       "error": str(last_error)}))
            time.sleep(delay)
        raise last_error or StableAudioError("generation failed")


def resolve_registered_sample(sample_id: str, project_root: str | None = None,
                               db_path: str | None = None) -> tuple[str, Any]:
    """Resolve a sample through the existing Studio registry and opaque ID."""
    from studio.sample_library import SampleLibraryService
    root = os.path.abspath(project_root or os.path.dirname(os.path.dirname(__file__)))
    service = SampleLibraryService(root, db_path=db_path)
    try:
        return service.resolve_id(sample_id)
    finally:
        service.close()


def get_provider(config: StableAudioConfig | None = None,
                 sample_resolver=None) -> StableAudioProvider:
    cfg = config or StableAudioConfig.from_env()
    resolver = sample_resolver
    if resolver is None:
        resolver = lambda sample_id: resolve_registered_sample(sample_id)
    return StableAudioProvider(cfg, sample_resolver=resolver)
