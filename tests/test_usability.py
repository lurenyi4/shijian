from concurrent.futures import ThreadPoolExecutor
from threading import Event
import zipfile

from app import create_app
from fastapi.testclient import TestClient
from storage import Library
import io
from PIL import Image


def image_bytes():
    buffer = io.BytesIO()
    Image.new('RGB', (40, 40), 'white').save(buffer, format='PNG')
    return buffer.getvalue()


def test_recovery_draft_and_preferences_survive_restart(tmp_path):
    root = tmp_path / 'library'
    with TestClient(create_app(root, seed=False, run_worker=False)) as client:
        client.headers['X-Shijian-Session'] = client.get('/api/state').json()['session']
        doc = client.post('/api/import', files={'file': ('test.png', image_bytes())}).json()['document']
        url = '/api/documents/' + doc['id']
        draft = {'title': '', 'text': '未完成的繁體\n第二行', 'base_updated_at': doc['updated_at']}
        response = client.put(url + '/draft', json=draft)
        assert response.status_code == 200
        version = response.json()['draft_version']
        assert client.get(url).json()['text'] == ''
        assert client.get(url + '/revisions').json() == []
        assert client.put('/api/preferences', json={'large_text': True}).status_code == 200
    with TestClient(create_app(root, seed=False, run_worker=False)) as client:
        state = client.get('/api/state').json()
        client.headers['X-Shijian-Session'] = state['session']
        assert state['preferences']['large_text'] is True
        assert client.get(url + '/draft').json()['text'] == draft['text']
        assert client.patch(url, json={'title': '已完成', 'text': draft['text'],
                                      'expected_updated_at': doc['updated_at'],
                                      'draft_version': version}).status_code == 200
        assert client.get(url + '/draft').json() is None
        response = client.patch(url, json={'text': '過期的覆蓋', 'expected_updated_at': doc['updated_at']})
        assert response.status_code == 400
        assert client.get(url).json()['text'] == draft['text']


def test_interleaved_windows_cannot_overwrite_or_clear_another_draft(tmp_path):
    library = Library(tmp_path / 'library', seed=False)
    source=tmp_path/'sample.png';source.write_bytes(image_bytes())
    doc,_=library.import_file(source,'sample.png')
    initial={'title':'草稿','text':'窗口 A','base_updated_at':doc['updated_at']}
    a=library.save_draft(doc['id'],initial)
    b=library.save_draft(doc['id'],{**a,'text':'窗口 B'})
    import pytest
    with pytest.raises(ValueError):library.save_draft(doc['id'],a)
    library.edit(doc['id'],{'text':'A 正式保存'},doc['updated_at'],a['draft_version'])
    assert library.draft(doc['id'])['text']=='窗口 B'
    current=library.get(doc['id'])
    library.edit(doc['id'],{'text':'B 确认保存'},current['updated_at'],b['draft_version'])
    assert library.draft(doc['id']) is None
    with pytest.raises(ValueError):library.save_draft(doc['id'],b)
    with pytest.raises(ValueError):library.save_draft(doc['id'],initial)


def test_archive_compression_does_not_block_edit_and_uses_snapshot(tmp_path, monkeypatch):
    library = Library(tmp_path / 'library', seed=False)
    source = tmp_path / 'test.png'
    source.write_bytes(image_bytes())
    doc, _ = library.import_file(source, 'test.png')
    entered, release = Event(), Event()
    original = zipfile.ZipFile.write

    def blocked(archive, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(archive, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'write', blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        export = pool.submit(library.export, [doc['id']], 'archive')
        try:
            assert entered.wait(5)
            edit = pool.submit(library.edit, doc['id'], {'text': '压缩期间保存'})
            assert edit.result(timeout=2)['text'] == '压缩期间保存'
        finally:
            release.set()
        target = export.result(timeout=5)
    with zipfile.ZipFile(target) as archive:
        import json
        record = next(n for n in archive.namelist() if n.endswith('/档案.json'))
        assert json.loads(archive.read(record))['text'] == ''


def test_failed_archive_is_not_published_or_left_in_tmp(tmp_path, monkeypatch):
    import pytest
    library=Library(tmp_path/'library',seed=False)
    source=tmp_path/'sample.png';source.write_bytes(image_bytes())
    doc,_=library.import_file(source,'sample.png')
    def fail(*args,**kwargs):raise OSError('synthetic full disk')
    monkeypatch.setattr(zipfile.ZipFile,'write',fail)
    with pytest.raises(OSError):library.export([doc['id']],'archive')
    assert not list((library.root/'exports').iterdir())
    assert not list((library.root/'tmp').iterdir())
