import wave
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest

from local_runtime.materials import MaterialError, Parser, chunk_records, file_hash
from local_runtime.videos import VideoOptions, decode_video, parse_video, request_cached, signature


@pytest.fixture
def sample_video(tmp_path):
    path = tmp_path / "sample.mp4"
    with av.open(str(path), "w") as container:
        video = container.add_stream("mpeg4", rate=10)
        video.width, video.height, video.pix_fmt = 64, 48, "yuv420p"
        audio = container.add_stream("aac", rate=16000)
        audio.layout = "mono"
        for i in range(25):
            frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), i * 9, dtype=np.uint8), format="rgb24")
            frame.pts, frame.time_base = i, Fraction(1, 10)
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        samples = (np.sin(np.arange(40000) * 2 * np.pi * 440 / 16000) * 10000).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 16000, 0, Fraction(1, 16000)
        for packet in audio.encode(frame):
            container.mux(packet)
        for packet in audio.encode():
            container.mux(packet)
    return path


def test_real_video_decoding_preserves_pts_and_audio(sample_video, tmp_path, monkeypatch):
    options = VideoOptions(segment_seconds=2, sample_fps=2)
    record = decode_video(sample_video, file_hash(sample_video), tmp_path / "cache", options)
    assert record["duration_seconds"] == pytest.approx(2.5)
    assert record["decoded_frames"] == 25
    assert record["sampled_frames"] == 5
    assert [f["seconds"] for s in record["segments"] for f in s["frames"]] == [0, 0.5, 1, 1.5, 2]
    assert record["segments"][-1]["end_seconds"] == 2.5
    assert len(record["audio"]) == 1 and not record["audio"][0]["silent"]
    with wave.open(record["audio"][0]["path"], "rb") as audio:
        assert audio.getframerate() == 16000 and audio.getnchannels() == 1
        assert audio.getnframes() == 40000
    monkeypatch.setattr(av, "open", lambda *a, **k: pytest.fail("complete cache should avoid re-decoding"))
    assert decode_video(sample_video, file_hash(sample_video), tmp_path / "cache", options) == record


def test_corrupt_video_fails_before_model_calls(tmp_path):
    path = tmp_path / "bad.mp4"
    path.write_bytes(b"incomplete-mp4")
    with pytest.raises(MaterialError, match="upload is complete"):
        decode_video(path, file_hash(path), tmp_path / "cache", VideoOptions())


def test_local_errors_remain_readable_when_materials_is_a_cli():
    import runpy

    from local_runtime import materials

    namespace = runpy.run_path(materials.__file__, run_name="local_runtime.cli_error_test")
    assert namespace["error_summary"](MaterialError("Video is incomplete")) == "Video is incomplete"


def test_music_only_asr_is_not_indexed_and_later_chunk_identity_is_stable(tmp_path):
    path = tmp_path / "demo.mp4"
    units = [
        {"text": "视频音轨转写（00:00.000–00:10.000）：\n♪♪♪", "metadata": {"extraction_method": "video_asr"}},
        {"text": "机器人举起杯子", "metadata": {"extraction_method": "video_sampled_frames"}},
    ]
    records = list(chunk_records(path, tmp_path, "sha", units, "vl"))
    assert len(records) == 1
    assert records[0][1]["unit_index"] == 2


def test_video_api_preserves_order_and_resumes_cached_responses(sample_video, tmp_path):
    settings = SimpleNamespace(llm=SimpleNamespace(base_url="http://local-test"))
    parser = Parser(settings, tmp_path / "cache", "test-vl", VideoOptions(segment_seconds=2, sample_fps=2))
    calls = []
    parser.ensure_client = lambda: None

    def request(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="观察记录"))], usage=None
        )

    parser.request_vision = request
    units = parse_video(parser, sample_video, file_hash(sample_video))
    assert len(units) == 3
    assert parser.video_calls == 2 and parser.audio_calls == 1
    audio_call = next(c for c in calls if c["model"] == "qwen3-asr-flash")
    assert audio_call["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert audio_call["extra_body"] == {"asr_options": {"enable_itn": False}}
    assert sorted(u["metadata"]["start_seconds"] for u in units) == [0, 0, 2]
    assert all(u["metadata"]["video_config_id"] for u in units)
    for call in calls:
        if call["model"] == "test-vl":
            content = call["messages"][1]["content"]
            assert all(
                content[i]["type"] == "text" and content[i + 1]["type"] == "image_url"
                for i in range(0, len(content), 2)
            )
    assert parse_video(parser, sample_video, file_hash(sample_video)) == units
    assert len(calls) == 3 and parser.video_cache_hits == 3


def test_empty_audio_is_valid_but_truncated_output_is_not_cached(tmp_path):
    settings = SimpleNamespace(llm=SimpleNamespace(base_url="http://local-test"))
    parser = Parser(settings, tmp_path, "test-vl")
    parser.ensure_client = lambda: None
    reply = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=""))
    parser.request_vision = lambda **kw: SimpleNamespace(choices=[reply], usage=None)
    assert request_cached(parser, "asr", "test-asr", [], "empty-audio", allow_empty=True) == ""
    reply.finish_reason = "length"
    with pytest.raises(MaterialError, match="incomplete"):
        request_cached(parser, "vision", "test-vl", [], "bad-reply")
    assert len(list((tmp_path / "video-responses").glob("*.json"))) == 1


def test_video_sampling_configuration_changes_identity(tmp_path):
    path = tmp_path / "demo.mp4"
    one = {"text": "视频观察", "metadata": {"video_config_id": "one"}}
    two = {"text": "视频观察", "metadata": {"video_config_id": "two"}}
    first = list(chunk_records(path, tmp_path, "sha", [one], "vl"))[0][1]
    second = list(chunk_records(path, tmp_path, "sha", [two], "vl"))[0][1]
    assert first["ingest_key"] != second["ingest_key"]
    assert signature(VideoOptions(workers=1)) == signature(VideoOptions(workers=4))
    assert signature(VideoOptions(sample_fps=1)) != signature(VideoOptions(sample_fps=2))
    with pytest.raises(ValueError):
        VideoOptions(sample_fps=float("nan")).validate()
