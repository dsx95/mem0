import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from local_runtime.materials import (
    Parser,
    chunk_records,
    discover,
    file_hash,
    image_tiles,
    import_file,
    split_text,
)


def test_chunks_preserve_content_and_page_provenance(tmp_path):
    source = tmp_path / "说明.txt"
    text = "这是资料中的完整句子。\n" * 350
    source.write_text(text)
    chunks = split_text(text)
    covered = set()
    for chunk in chunks:
        assert len(chunk["text"]) <= 1400
        covered.update(range(chunk["char_start"], chunk["char_end"]))
    assert covered == set(range(len(text.strip())))
    units = [{"text": text, "metadata": {"page_start": 3, "page_end": 3}}]
    records = list(chunk_records(source, tmp_path, file_hash(source), units, "vision"))
    assert len(records) == len(chunks)
    assert all(meta["page_start"] == meta["page_end"] == 3 for _, meta in records)
    assert len({meta["ingest_key"] for _, meta in records}) == len(records)


def test_discovery_ignores_hidden_and_rejects_escape(tmp_path):
    root = tmp_path / "materials"
    root.mkdir()
    (root / "good.txt").write_text("hello")
    (root / ".hidden.txt").write_text("private")
    (tmp_path / "outside.txt").write_text("outside")
    assert [p.name for p in discover(root)] == ["good.txt"]
    with pytest.raises(ValueError):
        discover(root, ["../outside.txt"])


def test_long_image_tiles_cover_bottom_and_right():
    image = Image.new("RGB", (1950, 5100), "white")
    tiles = list(image_tiles(image))
    assert max(box[2] for _, box in tiles) == image.width
    assert max(box[3] for _, box in tiles) == image.height
    for x, y in [(0, 0), (1949, 5099), (1720, 1720), (80, 4500)]:
        assert any(box[0] <= x < box[2] and box[1] <= y < box[3] for _, box in tiles)


class Store:
    def __init__(self):
        self.records = {}

    def add(self, text, *, user_id, infer, metadata):
        assert infer is False
        key = str(len(self.records) + 1)
        self.records[key] = {"memory": text, "metadata": metadata, "user_id": user_id}
        return {"results": [{"id": key, "event": "ADD"}]}

    def get(self, key):
        return self.records[key]


def test_resume_uses_persisted_keys_even_without_report(tmp_path):
    path = tmp_path / "document.txt"
    path.write_text("完整的原始资料" * 400)
    units = [{"text": path.read_text(), "metadata": {"page_start": 1, "page_end": 1}}]
    store = Store()
    first = import_file(store, path, tmp_path, file_hash(path), units, "vision", "kb", {})
    restored_keys = {item["metadata"]["ingest_key"]: key for key, item in store.records.items()}
    second = import_file(store, path, tmp_path, file_hash(path), units, "vision", "kb", restored_keys)
    assert first["added"] > 1
    assert second["added"] == 0
    assert second["skipped"] == first["chunks"] == len(store.records)


def test_new_source_version_gets_distinct_identity(tmp_path):
    path = tmp_path / "document.txt"
    units = [{"text": "同一个片段", "metadata": {}}]
    old = list(chunk_records(path, tmp_path, "version1", units, "vision"))[0][1]
    new = list(chunk_records(path, tmp_path, "version2", units, "vision"))[0][1]
    assert old["doc_id"] == new["doc_id"]
    assert old["ingest_key"] != new["ingest_key"]


def test_pdf_page_numbers_are_physical_pages(tmp_path):
    import pymupdf

    path = tmp_path / "pages.pdf"
    with pymupdf.open() as doc:
        for number in (1, 2):
            page = doc.new_page()
            page.insert_textbox((50, 50, 550, 500), f"Page {number}. " + "Readable source text. " * 12)
        doc.save(path)
    parser = Parser(SimpleNamespace(), tmp_path / "cache", "unused")
    units = parser.pdf_units(path)
    assert [unit["metadata"]["page_start"] for unit in units] == [1, 2]
    assert units[0]["text"].startswith("Page 1.")
    assert units[1]["text"].startswith("Page 2.")


def test_gif_timestamps_use_real_frame_durations(tmp_path):
    path = tmp_path / "animation.gif"
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue", "green")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[100, 300, 200], loop=0)
    settings = SimpleNamespace(llm=SimpleNamespace(base_url="http://unused"))
    parser = Parser(settings, tmp_path / "cache", "unused")
    parser.visual_units = lambda image, meta: [{"text": "frame", "metadata": meta}]
    units = parser.parse(path, file_hash(path))
    assert [u["metadata"]["start_seconds"] for u in units] == [0, 0.1, 0.4]
    assert [u["metadata"]["end_seconds"] for u in units] == [0.1, 0.4, 0.6]


def test_shell_reuses_one_memory_and_releases_it_after_eof(monkeypatch, tmp_path, capsys):
    from local_runtime import materials

    selected = tmp_path / "source.txt"
    selected.write_text("资料")
    settings = SimpleNamespace(data_dir=tmp_path, collection="test")
    memory = object()
    opened, closed, searches = [], [], []
    monkeypatch.setattr(materials, "load_settings", lambda _: settings)
    monkeypatch.setattr(materials, "create_memory", lambda _: opened.append(memory) or memory)
    monkeypatch.setattr(materials, "close_memory", closed.append)
    monkeypatch.setattr(materials.sys, "stdin", io.StringIO("问题一\n\n问题二\n"))

    def search(actual_memory, query, root, user_id, top_k, source_files, hybrid):
        searches.append((actual_memory, query, root, user_id, top_k, source_files, hybrid))
        return {"results": [{"memory": query}]}

    monkeypatch.setattr(materials, "search_materials", search)
    assert (
        materials.main(
            ["shell", "--root", str(tmp_path), "--user-id", "private-user", "--file", "source.txt", "--timings"]
        )
        == 0
    )
    assert opened == closed == [memory]
    assert [item[1] for item in searches] == ["问题一", "问题二"]
    assert all(
        item[0] is memory and item[2:] == (tmp_path, "private-user", 5, ["source.txt"], False) for item in searches
    )
    assert '"search_ms"' in capsys.readouterr().out


@pytest.mark.parametrize("status", [429, 401])
def test_shell_redacts_errors_and_closes_on_auth_failure_or_exit(monkeypatch, tmp_path, capsys, status):
    from local_runtime import materials

    class ProviderError(Exception):
        status_code = status

    memory, closed, queries = object(), [], []
    monkeypatch.setattr(materials, "load_settings", lambda _: SimpleNamespace(data_dir=tmp_path, collection="test"))
    monkeypatch.setattr(materials, "create_memory", lambda _: memory)
    monkeypatch.setattr(materials, "close_memory", closed.append)
    monkeypatch.setattr(materials.sys, "stdin", io.StringIO("first\nsecond\n/exit\nignored\n"))

    def search(*args):
        queries.append(args[1])
        if len(queries) == 1:
            raise ProviderError("secret-provider-body")
        return {"results": []}

    monkeypatch.setattr(materials, "search_materials", search)
    code = materials.main(["shell", "--root", str(tmp_path)])
    assert closed == [memory]
    assert queries == (["first"] if status == 401 else ["first", "second"])
    assert code == (1 if status == 401 else 0)
    output = capsys.readouterr().out
    assert "secret-provider-body" not in output
    assert json.loads(output.splitlines()[0])["error"] == f"ProviderError (HTTP {status})"
