import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image

import engine as engine_module
from engine import LocalEngine
from storage import Library


@pytest.fixture
def queued(tmp_path):
    library = Library(tmp_path / "library", seed=False)
    image = Image.new("RGBA", (100, 140), (0, 0, 0, 0))
    image.paste((0, 0, 0, 255), (30, 30, 70, 100))
    source = tmp_path / "sample.png"
    image.save(source)
    doc, _ = library.import_file(source, source.name)
    engine = LocalEngine(library)
    library.set_setting(
        "engine", {"executable": sys.executable, "tier": "standard", "model_source": "local"}
    )
    engine.enqueue([doc["id"]])
    return engine, doc["id"]


@pytest.mark.parametrize("action", ["cancel", "requeue", "stop"])
def test_cancel_during_image_preparation_never_resurrects_old_job(queued, monkeypatch, action):
    engine, doc_id = queued
    _, config, token = engine.pending.get_nowait()
    preparing, proceed = Event(), Event()
    normalize = engine_module.manuscript_rgb

    def paused(image):
        preparing.set()
        assert proceed.wait(5)
        return normalize(image)

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("A cancelled attempt must not launch the OCR process")

    monkeypatch.setattr(engine_module, "manuscript_rgb", paused)
    monkeypatch.setattr(engine_module.subprocess, "Popen", unexpected_spawn)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(engine.parse, doc_id, config, token)
        try:
            assert preparing.wait(5)
            if action == "stop":
                engine.stop()
            else:
                engine.cancel([doc_id])
            assert engine.library.get(doc_id)["status"] == "interrupted"
            if action == "requeue":
                assert engine.enqueue([doc_id]) == [doc_id]
                assert engine.jobs[doc_id] != token
        finally:
            proceed.set()
        future.result(timeout=5)
    assert engine.library.get(doc_id)["status"] == (
        "queued" if action == "requeue" else "interrupted"
    )
    assert engine.info()["pending"] == (1 if action == "requeue" else 0)
    assert not engine.library.active_results


def test_worker_commits_text_and_preserves_history_with_opaque_ocr_input(queued, monkeypatch):
    engine, doc_id = queued
    engine.library.update(doc_id, text="舊校對", raw_text="舊識別")
    done = Event()
    finish = engine.library.finish_recognition

    def record_finish(*args):
        result = finish(*args)
        done.set()
        return result

    def completed_process(command, **kwargs):
        with Image.open(command[2]) as image:
            assert image.mode == "RGB"
            assert image.getpixel((0, 0)) == (255, 255, 255)
            assert image.getpixel((50, 50)) == (0, 0, 0)
        with zipfile.ZipFile(command[command.index("-o") + 1], "w") as archive:
            archive.writestr("full.md", "# 新識別\n保留換行")
        return SimpleNamespace(returncode=0, poll=lambda: 0)

    monkeypatch.setattr(engine.library, "finish_recognition", record_finish)
    monkeypatch.setattr(engine_module.subprocess, "Popen", completed_process)
    engine.start()
    try:
        assert done.wait(5)
    finally:
        engine.stop()
    doc = engine.library.get(doc_id)
    assert doc["status"] == "done" and doc["reviewed"] == 0
    assert doc["text"] == doc["raw_text"] == "# 新識別\n保留換行"
    with engine.library.connect() as db:
        assert (
            db.execute("SELECT text FROM revisions WHERE document_id=?", (doc_id,)).fetchone()[
                "text"
            ]
            == "舊校對"
        )
    assert not engine.library.active_results
