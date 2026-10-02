"""Deterministic unit tests for TIMBOR's Stable Audio 3 Modal client.

Run with ``python tests/test_stable_audio.py``. No cloud calls are made.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import pathlib
import struct
import sys
import tempfile
import urllib.error
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import timbor.stable_audio as stable_audio
from timbor.stable_audio import (
    AudioDiagnostics, GenerationRequest, StableAudioConfig, StableAudioError,
    StableAudioProvider, inspect_wav,
)

FAILED = []


def make_wav(seconds=1.0, rate=44100, channels=2, sample_width=2, silent=False):
    count = int(seconds * rate)
    t = np.arange(count, dtype=np.float64) / rate
    mono = np.zeros(count) if silent else 0.12 * np.sin(2 * np.pi * 440 * t)
    samples = np.repeat(mono[:, None], channels, axis=1).reshape(-1)
    if sample_width == 2:
        data = (samples * 32767).astype("<i2").tobytes()
    elif sample_width == 4:
        data = (samples * (2**31 - 1)).astype("<i4").tobytes()
    else:
        raise ValueError(sample_width)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(rate)
        wav.writeframes(data)
    return out.getvalue()


class FakeResponse:
    def __init__(self, body, headers=None, status=200):
        self.body = body
        self.headers = headers or {}
        self.status = status
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, *args): return self.body


def config(**overrides):
    values = {"enabled": True, "modal_url": "https://example.modal.run",
              "api_key": "unit-test-key", "max_retries": 1}
    values.update(overrides)
    return StableAudioConfig(**values)


def test_env_configuration():
    cfg = StableAudioConfig.from_env({"STABLE_AUDIO_ENABLED": "true",
        "STABLE_AUDIO_PROVIDER": "modal", "STABLE_AUDIO_MODEL": "small-music",
        "STABLE_AUDIO_MODAL_URL": "https://example.modal.run/",
        "STABLE_AUDIO_API_KEY": "not-reported", "STABLE_AUDIO_TIMEOUT": "42"})
    assert cfg.enabled and cfg.modal_url == "https://example.modal.run"
    assert cfg.model == "small-music" and cfg.timeout == 42
    try:
        StableAudioConfig.from_env({"STABLE_AUDIO_TIMEOUT": "invalid"})
    except StableAudioError:
        pass
    else:
        raise AssertionError("invalid timeout was accepted")
    for values in ({"provider": "local"}, {"model": "medium"}, {"model": "unknown"},
                   {"modal_url": "http://example.com"}):
        try:
            config(**values).validate()
        except StableAudioError:
            continue
        raise AssertionError(f"invalid configuration accepted: {values}")


def test_modal_dashboard_url_is_rejected():
    """Pasting the Modal dashboard page yields a 405; the config must refuse it."""
    for url in ("https://modal.com/apps/timop80/main/deployed/timbor-stable-audio-3",
                "https://modal.com/apps/example/main/deployed/service"):
        try:
            config(modal_url=url).validate()
        except StableAudioError as exc:
            assert "*.modal.run" in str(exc), str(exc)
        else:
            raise AssertionError(f"Modal dashboard URL was accepted: {url}")


def test_modal_url_is_normalized_to_invocation_base():
    """A pasted /generate route or trailing slash must not double the path."""
    cfg = StableAudioConfig.from_env({
        "STABLE_AUDIO_ENABLED": "true",
        "STABLE_AUDIO_MODAL_URL": "https://example--app-class-web.modal.run/generate/",
        "STABLE_AUDIO_API_KEY": "not-reported"})
    assert cfg.modal_url == "https://example--app-class-web.modal.run", cfg.modal_url


def test_http_405_error_points_at_invocation_endpoint():
    """A 405 (method not allowed) must explain the correct Modal URL shape."""
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 405, "Method Not Allowed", {},
                                     io.BytesIO(b"method not allowed"))
    provider = StableAudioProvider(config(max_retries=0), opener=opener)
    try:
        provider.generate(GenerationRequest("rave", 1, seed=1))
    except StableAudioError as exc:
        assert "405" in str(exc) and "*.modal.run" in str(exc), str(exc)
    else:
        raise AssertionError("405 HTTPError was not surfaced")


def test_structured_prompt_and_validation():
    request = GenerationRequest(prompt="hardcore rave", duration=30, seed=12,
        genre="gabber", era="1994", bpm=190, key="F minor", mood="dark euphoric",
        elements=["distorted kick", "hoover"])
    prompt = request.stable_prompt()
    assert "gabber" in prompt and "1994" in prompt and "190 BPM" in prompt
    assert "F minor" in prompt and "distorted kick" in prompt
    request.validate(config())
    for invalid in (GenerationRequest("", 10), GenerationRequest("sound", 121),
                    GenerationRequest("sound", 2, seed=-2)):
        try:
            invalid.validate(config())
        except StableAudioError:
            continue
        raise AssertionError("invalid request was accepted")
    try:
        GenerationRequest("sound", 10, mode="audio-to-audio").validate(config())
    except StableAudioError:
        pass
    else:
        raise AssertionError("audio-to-audio without input was accepted")


def test_audio_validation():
    valid = make_wav(seconds=2.0)
    report = inspect_wav(valid, expected_duration=2.0)
    assert isinstance(report, AudioDiagnostics)
    assert report.sample_rate == 44100 and report.channels == 2
    assert report.bit_depth == 16 and report.rms > 0.05
    # Conditioning uploads may be any supported WAV sample rate; only generated
    # responses are constrained to 44.1 kHz by StableAudioProvider.generate().
    assert inspect_wav(make_wav(2, rate=48000)).sample_rate == 48000
    for bad, duration in ((b"not wave", None), (valid[:-50], None),
                          (make_wav(2, silent=True), None), (valid, 3.0)):
        try:
            inspect_wav(bad, expected_duration=duration)
        except StableAudioError:
            continue
        raise AssertionError("invalid WAV was accepted")


def test_request_body_prompt_seed_and_auth():
    provider = StableAudioProvider(config(), opener=lambda *a, **k: None)
    request = GenerationRequest("dark jungle", 4, seed=321, genre="jungle", bpm=170)
    body, content_type, _ = provider._build_body(request)
    assert content_type == "application/json"
    doc = json.loads(body)
    assert doc["seed"] == 321 and doc["model"] == "small-music"
    assert "170 BPM" in doc["prompt"]
    path = provider.config.modal_url + "/generate"
    sent = {}
    def opener(req, timeout):
        sent["url"], sent["authorization"], sent["timeout"] = req.full_url, req.get_header("Authorization"), timeout
        return FakeResponse(make_wav(4), {"Content-Type": "audio/wav"})
    client = StableAudioProvider(config(), opener=opener)
    audio, metadata = client.generate(request)
    assert audio.startswith(b"RIFF") and metadata["seed"] == 321
    assert sent == {"url": path, "authorization": "Bearer unit-test-key", "timeout": 600.0}
    assert metadata["diagnostics"]["sample_rate"] == 44100

    def wrong_rate(req, timeout):
        return FakeResponse(make_wav(4, rate=48000), {"Content-Type": "audio/wav"})
    try:
        StableAudioProvider(config(max_retries=0), opener=wrong_rate).generate(request)
    except StableAudioError as exc:
        assert "sample rate" in str(exc)
    else:
        raise AssertionError("non-44.1 kHz generated output was accepted")


def test_structured_service_metadata_and_request_parsing():
    service_metadata = {"job_id": "real-job", "generation_seconds": 0.5, "seed": 17}
    headers = {"Content-Type": "audio/wav", "X-Audio-Metadata":
        __import__("base64").b64encode(json.dumps(service_metadata).encode()).decode()}
    provider = StableAudioProvider(config(), opener=lambda *a, **k: FakeResponse(make_wav(1), headers))
    audio, result = provider.generate(GenerationRequest("rave", 1, seed=17))
    assert audio.startswith(b"RIFF") and result["job_id"] == "real-job"
    assert result["generation_seconds"] == 0.5


def test_malformed_and_server_failures():
    provider = StableAudioProvider(config(max_retries=0), opener=lambda *a, **k: FakeResponse(b"{}", {"Content-Type": "text/plain"}))
    try:
        provider.generate(GenerationRequest("rave", 1))
    except StableAudioError:
        pass
    else:
        raise AssertionError("malformed response accepted")

    def server_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"no"))
    provider = StableAudioProvider(config(max_retries=2), opener=server_error)
    try:
        provider.generate(GenerationRequest("rave", 1))
    except StableAudioError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("server failure ignored")


def test_retry_and_avoid_duplicate_timeout():
    attempts = []
    def busy_once(req, timeout):
        attempts.append(req)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"busy"))
        return FakeResponse(make_wav(1), {"Content-Type": "audio/wav"})
    client = StableAudioProvider(config(max_retries=1), opener=busy_once)
    _, result = client.generate(GenerationRequest("rave", 1))
    assert len(attempts) == 2 and result["duration"] == 1

    timeout_attempts = []
    def timeout(req, timeout):
        timeout_attempts.append(req)
        raise TimeoutError("request may already have started")
    client = StableAudioProvider(config(max_retries=2), opener=timeout)
    try:
        client.generate(GenerationRequest("rave", 1))
    except StableAudioError:
        pass
    else:
        raise AssertionError("timeout should fail")
    assert len(timeout_attempts) == 1, "timeout must not risk duplicate paid inference"


def test_sample_library_upload_is_multipart():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "conditioning.wav")
        with open(path, "wb") as f:
            f.write(make_wav(1))
        resolved = []
        provider = StableAudioProvider(config(), sample_resolver=lambda sid: (resolved.append(sid) or path, {}))
        request = GenerationRequest("breakbeat", 1, seed=7, mode="audio-to-audio",
                                    sample_id="registered-sample", strength=0.5)
        body, content_type, _ = provider._build_body(request)
        assert content_type.startswith("multipart/form-data;")
        assert b"conditioning" not in body and b"audio\"; filename=\"input.wav\"" in body
        assert b"0.5" in body and b"\x00" in body
        assert resolved == ["registered-sample"]


def test_modal_entrypoints_match_documented_protocol():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "modal", "stable_audio.py")
    source = open(path, encoding="utf-8").read()
    assert "@http_app.get(\"/diagnostic\")" in source
    assert "@http_app.get(\"/health\")" in source
    assert "@http_app.post(\"/generate\")" in source
    assert 'async def health(request: Request)' in source
    assert "@modal.enter()" in source
    assert "StableAudioModel.from_pretrained(MODEL" in source
    assert 'OFFICIAL_COMMIT = "3a82c807b69cf4b7c5c05270011a5d5e47abac18"' in source
    assert "stable-audio-3/archive/{OFFICIAL_COMMIT}.zip" in source


def test_seed_and_duration_request_limits():
    cfg = config(max_duration=8)
    for duration in (0, 9, float("inf"), float("nan")):
        try:
            GenerationRequest("sound", duration).validate(cfg)
        except StableAudioError:
            continue
        raise AssertionError(f"invalid duration accepted: {duration}")
    for seed in (-2, 2**32):
        try:
            GenerationRequest("sound", 1, seed=seed).validate(config())
        except StableAudioError:
            continue
        raise AssertionError(f"invalid seed accepted: {seed}")


def test_bool_seed_is_rejected():
    for seed in (True, False):
        try:
            GenerationRequest("sound", 1, seed=seed).validate(config())
        except StableAudioError:
            continue
        raise AssertionError(f"boolean seed accepted: {seed}")


def test_env_file_values_may_be_quoted():
    """A quoted .env value must not retain its quote characters."""
    names = ("STABLE_AUDIO_ENABLED", "STABLE_AUDIO_MODAL_URL", "STABLE_AUDIO_API_KEY")
    with tempfile.TemporaryDirectory() as tmp:
        pathlib.Path(tmp, ".env").write_text(
            "STABLE_AUDIO_ENABLED=true\n"
            'STABLE_AUDIO_MODAL_URL="https://example.modal.run/"\n'
            "STABLE_AUDIO_API_KEY='quoted-key'\n", encoding="utf-8")
        original_root, original_env = stable_audio.PROJECT_ROOT, dict(os.environ)
        for name in names:
            os.environ.pop(name, None)
        stable_audio.PROJECT_ROOT = pathlib.Path(tmp)
        try:
            cfg = StableAudioConfig.from_env()
        finally:
            stable_audio.PROJECT_ROOT = original_root
            os.environ.clear()
            os.environ.update(original_env)
    assert cfg.enabled is True
    assert cfg.modal_url == "https://example.modal.run", cfg.modal_url
    assert cfg.api_key == "quoted-key", cfg.api_key


def test_project_env_overrides_inherited_variable():
    """A stale inherited endpoint must not shadow a corrected project .env."""
    names = ("STABLE_AUDIO_ENABLED", "STABLE_AUDIO_MODAL_URL", "STABLE_AUDIO_API_KEY")
    with tempfile.TemporaryDirectory() as tmp:
        pathlib.Path(tmp, ".env").write_text(
            "STABLE_AUDIO_ENABLED=true\n"
            "STABLE_AUDIO_MODAL_URL=https://fresh--app-class-web.modal.run\n"
            "STABLE_AUDIO_API_KEY=fresh-key\n", encoding="utf-8")
        original_root, original_env = stable_audio.PROJECT_ROOT, dict(os.environ)
        stable_audio.PROJECT_ROOT = pathlib.Path(tmp)
        os.environ["STABLE_AUDIO_MODAL_URL"] = "https://modal.com/apps/stale/main/deployed/x"
        try:
            cfg = StableAudioConfig.from_env()
        finally:
            stable_audio.PROJECT_ROOT = original_root
            os.environ.clear()
            os.environ.update(original_env)
    assert cfg.modal_url == "https://fresh--app-class-web.modal.run", cfg.modal_url
    assert cfg.api_key == "fresh-key", cfg.api_key


def test_service_metadata_header_is_case_insensitive():
    """HTTP header names are case-insensitive; casing must not drop metadata."""
    service_metadata = {"job_id": "real-job", "generation_seconds": 0.5, "seed": 17}
    encoded = base64.b64encode(json.dumps(service_metadata).encode()).decode()
    for header_name in ("X-Audio-Metadata", "x-audio-metadata", "X-AUDIO-METADATA"):
        headers = {"Content-Type": "audio/wav", header_name: encoded}
        provider = StableAudioProvider(
            config(), opener=lambda *a, **k: FakeResponse(make_wav(1), headers))
        _, result = provider.generate(GenerationRequest("rave", 1, seed=17))
        assert result["job_id"] == "real-job", header_name
        assert result["generation_seconds"] == 0.5, header_name
        assert result["seed"] == 17, header_name


def test_wav_without_service_metadata_exposes_no_transport_headers():
    headers = {"Content-Type": "audio/wav", "Content-Length": "352844",
               "Server": "modal"}
    provider = StableAudioProvider(
        config(), opener=lambda *a, **k: FakeResponse(make_wav(1), headers))
    _, result = provider.generate(GenerationRequest("rave", 1, seed=9))
    assert result["metadata"] == {}, result["metadata"]
    assert result["seed"] == 9


def test_exported_metadata_excludes_audio_payload():
    """The JSON transport must not leak base64 audio (or a transient URL) into
    the metadata that callers persist alongside the WAV."""
    wav = make_wav(1)
    doc = {"success": True, "job_id": "b64job", "seed": 5,
           "audio_base64": base64.b64encode(wav).decode()}
    provider = StableAudioProvider(
        config(), opener=lambda *a, **k: FakeResponse(
            json.dumps(doc).encode(), {"Content-Type": "application/json"}))
    audio, result = provider.generate(GenerationRequest("rave", 1, seed=5))
    assert audio.startswith(b"RIFF") and result["job_id"] == "b64job"
    assert "audio_base64" not in result["metadata"], result["metadata"]
    assert "audio_url" not in result["metadata"]
    # the exported document stays small even for long takes
    assert len(json.dumps(result)) < 4096


def test_timeout_reports_crash_loop_and_gated_model_cause():
    """A bare timeout must name the crash-looping container and the gated model
    rather than surfacing an unexplained transport failure."""
    def timeout(req, timeout):
        raise TimeoutError("The read operation timed out")
    client = StableAudioProvider(config(max_retries=2), opener=timeout)
    try:
        client.generate(GenerationRequest("rave", 1))
    except StableAudioError as exc:
        text = str(exc)
        assert "did not respond" in text and "600" in text, text
        assert "crash-looping" in text and "modal app logs" in text, text
        assert "stable-audio-3-small-music" in text, text
        assert "huggingface-token" in text, text
    else:
        raise AssertionError("timeout was not surfaced")


def test_crash_loop_http_error_points_at_gated_model():
    """A crash-looping service (5xx with a gated-repo body) must explain why."""
    body = (b"Function stable_audio.StableAudioService.* is crash-looping: "
            b"GatedRepoError: 403 Client Error")
    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error",
                                     {}, io.BytesIO(body))
    provider = StableAudioProvider(config(max_retries=0), opener=opener)
    try:
        provider.generate(GenerationRequest("rave", 1))
    except StableAudioError as exc:
        text = str(exc)
        assert "500" in text and "gated model" in text, text
        assert "stable-audio-3-small-music" in text, text
    else:
        raise AssertionError("crash-loop response was not surfaced")


def test_modal_service_reports_unloaded_model_instead_of_crashing():
    """A failed checkpoint load must be served over HTTP, not crash-loop."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "modal", "stable_audio.py")
    source = open(path, encoding="utf-8").read()
    assert "self.load_error = _describe_load_failure(exc)" in source
    assert "class ModelUnavailable" in source
    assert "status_code=503" in source
    assert '"model_load_error"' in source


def test_modal_routes_bind_the_request_object_not_a_query_parameter():
    """Every route must receive a ``Request``, never demand a ``request`` query.

    ``from __future__ import annotations`` stores annotations as strings and
    FastAPI resolves them against the handler's module globals. The fastapi
    import sits inside ``web()``, so the module has to publish ``Request`` or
    each handler would treat ``request: Request`` as a required query parameter
    and answer HTTP 422 instead of generating audio.
    """
    try:
        import modal
    except ImportError:
        return  # modal is optional; without it the deployment cannot be built
    try:
        import fastapi  # noqa: F401
    except ImportError:
        return
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "modal", "stable_audio.py")
    spec = importlib.util.spec_from_file_location("timbor_modal_stable_audio", path)
    module = importlib.util.module_from_spec(spec)
    original = modal.is_local
    modal.is_local = lambda: False  # never read or create the developer's .env
    try:
        spec.loader.exec_module(module)
    finally:
        modal.is_local = original
    get_user_cls = getattr(module.StableAudioService, "_get_user_cls", None)
    if get_user_cls is None:
        return  # a Modal release changed the wrapper; skip rather than misreport
    app = object.__new__(get_user_cls()).web()
    routes = {route.path: route for route in app.routes
              if getattr(route, "dependant", None)}
    for endpoint in ("/health", "/diagnostic", "/generate"):
        dependant = routes[endpoint].dependant
        assert dependant.request_param_name == "request", (
            f"{endpoint} does not bind the Request object; FastAPI sees the "
            f"query parameters {[param.name for param in dependant.query_params]}")


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        try:
            test()
            print(f"  {test.__name__}: OK")
        except Exception as exc:
            FAILED.append(test.__name__)
            print(f"  {test.__name__}: FAIL — {type(exc).__name__}: {exc}")
    if FAILED:
        print(f"FAILED: {FAILED}")
        return 1
    print(f"All {len(tests)} Stable Audio client tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
