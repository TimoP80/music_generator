"""Offline tests for the optional YuE2 and ACE-Step Modal clients."""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor import ace_step_engine, yue_engine


def make_wav(seconds=10.0, rate=48000):
    count = int(seconds * rate)
    t = np.arange(count, dtype=np.float64) / rate
    mono = (0.1 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
    stereo = np.repeat(mono[:, None], 2, axis=1)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(stereo.tobytes())
    return out.getvalue()


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return self.payload


def test_provider_environment_isolated_and_defaults():
    yue = yue_engine.EngineConfig.from_env({})
    ace = ace_step_engine.EngineConfig.from_env({})
    assert not yue.enabled and yue.modal_url == "" and yue.api_key == ""
    assert yue.default_duration == 180 and yue.timeout == 1800
    assert not ace.enabled and ace.modal_url == "" and ace.api_key == ""
    assert ace.default_duration == 30 and ace.max_duration == 600
    assert yue.sample_rate == ace.sample_rate == 48000


def test_provider_environment_parsing_and_endpoint_validation():
    yue = yue_engine.EngineConfig.from_env({
        "YUE2_ENABLED": "true", "YUE2_MODAL_URL": "https://yue.modal.run/generate/",
        "YUE2_API_KEY": "local-test", "YUE2_DEFAULT_DURATION": "240",
        "YUE2_TIMEOUT": "1200", "YUE2_GPU": "L40S"})
    assert yue.modal_url == "https://yue.modal.run"
    assert yue.default_duration == 240 and yue.timeout == 1200
    yue.validate()
    ace = ace_step_engine.EngineConfig.from_env({
        "ACESTEP_ENABLED": "yes", "ACESTEP_MODAL_URL": "https://ace.modal.run/",
        "ACESTEP_API_KEY": "local-test", "ACESTEP_MAX_DURATION": "480"})
    assert ace.modal_url == "https://ace.modal.run" and ace.max_duration == 480
    ace.validate()
    for config_type, error_type, key in (
        (yue_engine.EngineConfig, yue_engine.YueEngineError, "YUE2_MODAL_URL"),
        (ace_step_engine.EngineConfig, ace_step_engine.AceStepEngineError, "ACESTEP_MODAL_URL"),
    ):
        config = config_type(enabled=True, api_key="secret")
        try:
            config.validate()
        except error_type as exc:
            assert key in str(exc)
        else:
            raise AssertionError("enabled provider without endpoint was accepted")


def test_request_payloads_keep_provider_fields_separate():
    yue_config = yue_engine.EngineConfig()
    yue_request = yue_engine.GenerationRequest(
        "anthem", 180, seed=12, genre="gabber", era="1994", bpm=190,
        key="F minor", mood="euphoric", lyrics="[Chorus] Go!",
    )
    body, content_type, _ = yue_engine.YueEngineProvider(yue_config)._build_body(yue_request)
    payload = json.loads(body)
    assert content_type == "application/json"
    assert payload["lyrics"] == "[Chorus] Go!"
    assert "1994 gabber music" in payload["prompt"]
    assert payload["seed"] == 12 and payload["duration"] == 180

    ace_config = ace_step_engine.EngineConfig()
    ace_request = ace_step_engine.GenerationRequest(
        "machine rave", 30, seed=7, bpm=170, key="D minor", mood="dark",
        lyrics="[Verse] Night falls",
    )
    body, content_type, _ = ace_step_engine.AceStepEngineProvider(ace_config)._build_body(ace_request)
    payload = json.loads(body)
    assert content_type == "application/json"
    assert payload["lyrics"] == "[Verse] Night falls"
    assert payload["bpm"] == 170 and payload["key"] == "D minor"
    assert payload["seed"] == 7 and payload["duration"] == 30


def test_models_reject_unsupported_duration_prompt_and_audio_modes():
    yue_config = yue_engine.EngineConfig(max_duration=120)
    ace_config = ace_step_engine.EngineConfig(max_duration=120)
    for request, config, error_type in (
        (yue_engine.GenerationRequest("x" * 600, 30), yue_config, yue_engine.YueEngineError),
        (yue_engine.GenerationRequest("music", 121), yue_config, yue_engine.YueEngineError),
        (yue_engine.GenerationRequest("music", 30, mode="audio-to-audio"), yue_config, yue_engine.YueEngineError),
        (ace_step_engine.GenerationRequest("music", 9), ace_config, ace_step_engine.AceStepEngineError),
        (ace_step_engine.GenerationRequest("x" * 600, 30), ace_config, ace_step_engine.AceStepEngineError),
        (ace_step_engine.GenerationRequest("music", 30, mode="inpaint"), ace_config, ace_step_engine.AceStepEngineError),
    ):
        try:
            request.validate(config)
        except error_type:
            continue
        raise AssertionError(f"invalid request was accepted: {request}")


def test_fake_http_generation_validates_and_decodes_headers():
    audio = make_wav()
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        metadata = {"job_id": "remote-job", "seed": 31, "generation_seconds": 4.5}
        headers = {
            "Content-Type": "audio/wav",
            "X-Audio-Metadata": base64.b64encode(json.dumps(metadata).encode()).decode(),
        }
        return FakeResponse(audio, headers)

    config = yue_engine.EngineConfig(
        enabled=True, modal_url="https://yue.modal.run", api_key="local-test")
    provider = yue_engine.YueEngineProvider(config, opener=opener)
    result_audio, metadata = provider.generate(
        yue_engine.GenerationRequest("industrial rave", 180, seed=31, lyrics="[Instrumental]"))
    assert result_audio == audio and metadata["job_id"] == "remote-job"
    assert metadata["generation_seconds"] == 4.5 and metadata["sample_rate"] == 48000
    request, timeout = calls[0]
    assert request.full_url == "https://yue.modal.run/generate"
    assert request.get_header("Authorization") == "Bearer local-test"
    assert timeout == 1800


def test_ace_step_fake_http_generation_and_duration_validation():
    audio = make_wav(seconds=10)

    def opener(request, timeout):
        return FakeResponse(audio, {"Content-Type": "audio/wav"})

    config = ace_step_engine.EngineConfig(
        enabled=True, modal_url="https://ace.modal.run", api_key="local-test")
    provider = ace_step_engine.AceStepEngineProvider(config, opener=opener)
    result_audio, metadata = provider.generate(
        ace_step_engine.GenerationRequest("warehouse rave", 10, seed=3))
    assert result_audio == audio
    assert metadata["duration"] == 10 and metadata["sample_rate"] == 48000


def test_generated_wav_silence_rejected():
    silent = make_wav(seconds=0.1)
    # Replace data with silence and retain a structurally valid RIFF/WAVE.
    with wave.open(io.BytesIO(silent), "rb") as wav:
        params = wav.getparams()
        frames = wav.getnframes()
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams(params)
        wav.writeframes(b"\0\0" * frames * 2)
    for inspect, error_type in (
        (yue_engine.inspect_wav, yue_engine.YueEngineError),
        (ace_step_engine.inspect_wav, ace_step_engine.AceStepEngineError),
    ):
        try:
            inspect(out.getvalue())
        except error_type:
            continue
        raise AssertionError("silent WAV was accepted")


def main() -> int:
    tests = [test_provider_environment_isolated_and_defaults,
             test_provider_environment_parsing_and_endpoint_validation,
             test_request_payloads_keep_provider_fields_separate,
             test_models_reject_unsupported_duration_prompt_and_audio_modes,
             test_fake_http_generation_validates_and_decodes_headers,
             test_ace_step_fake_http_generation_and_duration_validation,
             test_generated_wav_silence_rejected]
    for test in tests:
        test()
        print(f"{test.__name__}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
