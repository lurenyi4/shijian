import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import create_app
from engine import read_result
from storage import Library


@pytest.fixture
def client(tmp_path):
    application = create_app(tmp_path/'library', seed=False, run_worker=False)
    with TestClient(application) as c:
        c.headers['X-Shijian-Session'] = c.get('/api/state').json()['session']
        yield c


def image_bytes(color='white'):
    buf=io.BytesIO()
    Image.new('RGB',(100,140),color).save(buf,format='PNG')
    return buf.getvalue()


def add(client, filename='爷爷的诗稿.png', content=None):
    response=client.post('/api/import',files={'file':(filename,content or image_bytes(),'image/png')})
    assert response.status_code==200, response.text
    return response.json()['document']


def test_import_preserves_original_dedup_and_restore(client):
    content=image_bytes()
    doc=add(client,content=content)
    assert doc['status']=='new'
    assert client.get('/api/documents/'+doc['id']+'/original').content==content
    assert client.get('/api/documents/'+doc['id']+'/preview').headers['content-type']=='image/jpeg'
    assert client.get('/api/documents/'+doc['id']+'/image').content==content
    client.patch('/api/documents/'+doc['id'],json={'trashed':True})
    response=client.post('/api/import',files={'file':('renamed.png',content)})
    assert response.json()['duplicate']
    assert response.json()['document']['id']==doc['id']
    assert not response.json()['document']['trashed']
    assert len(client.get('/api/state').json()['documents'])==1


def test_edit_history_preserves_traditional_characters_and_linebreaks(client):
    doc=add(client)
    url='/api/documents/'+doc['id']
    original='床前明月光，\n疑是地上霜。\n\n異體字：閒、裏、祇。'
    edited=client.patch(url,json={'title':'靜夜思','text':original,'author':'爺爺','reviewed':True})
    assert edited.status_code==200,edited.text
    assert edited.json()['reviewed']==1
    assert edited.json()['text']==original
    modified=client.patch(url,json={'text':original+'\n手稿後記'})
    assert modified.json()['reviewed']==0
    history=client.get(url+'/revisions').json()
    assert len(history)==2
    assert history[0]['text']==original
    assert history[0]['title']=='靜夜思'
    # Metadata-only actions should not generate a phantom revision.
    client.patch(url,json={'favorite':True,'collection':'家藏'})
    assert len(client.get(url+'/revisions').json())==2


def test_empty_review_and_invalid_files_rejected(client):
    doc=add(client)
    assert client.patch('/api/documents/'+doc['id'],json={'reviewed':True}).status_code==400
    assert client.post('/api/import',files={'file':('not-image.png',b'not an image')}).status_code==400
    assert client.post('/api/import',files={'file':('evil.html',b'<script>x</script>')}).status_code==400
    assert len(client.get('/api/state').json()['documents'])==1


def test_backup_restores_database_originals_and_history(client,tmp_path):
    doc=add(client)
    client.patch('/api/documents/'+doc['id'],json={'text':'原稿\n校對文字','collection':'爷爷的诗','reviewed':True})
    backup=client.post('/api/backup').json()
    payload=client.get(backup['url']).content
    dest=tmp_path/'restored'
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        assert 'library.sqlite3' in z.namelist()
        assert '恢复说明.txt' in z.namelist()
        z.extractall(dest)
    restored=Library(dest,seed=False)
    record=restored.get(doc['id'])
    assert record['text']=='原稿\n校對文字'
    assert record['collection']=='爷爷的诗'
    assert (dest/record['source']).read_bytes()==image_bytes()
    with restored.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0]==1


def test_archive_export_has_text_original_and_versions(client):
    doc=add(client)
    text='手稿文字\n保留换行'
    client.patch('/api/documents/'+doc['id'],json={'text':text,'title':'春/夜'})
    result=client.post('/api/export',json={'ids':[doc['id']],'format':'archive'})
    assert result.status_code==200
    with zipfile.ZipFile(io.BytesIO(client.get(result.json()['url']).content)) as z:
        assert len(z.namelist())==4
        name=next(n for n in z.namelist() if n.endswith('.txt'))
        assert text in z.read(name).decode('utf-8-sig')
        assert any(n.endswith('/校对历史.json') for n in z.namelist())
        assert any(n.endswith('/原稿.png') for n in z.namelist())


def test_cross_site_and_unauthorized_writes_blocked(client):
    assert client.post('/api/collections',json={'name':'evil'},headers={'X-Shijian-Session':''}).status_code==403
    assert client.post('/api/collections',json={'name':'evil'},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/state',headers={'Host':'evil.example'}).status_code==403
    assert client.patch('/api/documents/missing',json={'title':'x'}).status_code==404


def test_queue_cancellation_requeue_uses_distinct_attempts(client):
    engine=client.app.state.engine
    engine.info=lambda:{'available':True}
    doc=add(client)
    doc_id=doc['id']
    assert engine.enqueue([doc_id,doc_id])==[doc_id]
    first=engine.pending.get_nowait()
    assert engine.enqueue([doc_id])==[]
    engine.cancel([doc_id])
    assert engine.library.get(doc_id)['status']=='interrupted'
    assert engine.enqueue([doc_id])==[doc_id]
    second=engine.pending.get_nowait()
    assert first[2]!=second[2]
    assert engine.jobs[doc_id]==second[2]
    assert engine.jobs[doc_id]!=first[2]


def test_startup_marks_interrupted_work(tmp_path):
    library=Library(tmp_path,seed=True)
    library.update('demo-1',status='running')
    reopened=Library(tmp_path,seed=False)
    assert reopened.get('demo-1')['status']=='interrupted'


def test_pdf_multipage_preview(client):
    buf=io.BytesIO()
    first=Image.new('RGB',(400,600),'white')
    second=Image.new('RGB',(400,600),'#ffeeee')
    first.save(buf,format='PDF',save_all=True,append_images=[second])
    doc=add(client,filename='两页诗稿.pdf',content=buf.getvalue())
    assert doc['pages']==2
    assert client.get('/api/documents/'+doc['id']+'/preview?page=2').status_code==200
    assert client.get('/api/documents/'+doc['id']+'/image?page=1').status_code==200
    assert client.get('/api/documents/'+doc['id']+'/preview?page=3').status_code==400


def test_result_zip_only_reads_text_does_not_extract(tmp_path):
    path=tmp_path/'result.zip'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('../../escape.txt','untrusted')
        z.writestr('a/full.md','# 古詩\n繁體與換行')
    assert read_result(path)=='# 古詩\n繁體與換行'
    assert not (tmp_path.parent/'escape.txt').exists()


def test_manual_text_not_overwritten_without_explicit_reparse(client):
    doc=add(client)
    client.patch('/api/documents/'+doc['id'],json={'text':'珍贵的人工校对'})
    engine=client.app.state.engine
    engine.info=lambda:{'available':True}
    assert engine.enqueue([doc['id']])==[]
    assert engine.enqueue([doc['id']],force=True)==[doc['id']]


def test_active_edit_rejected(client):
    doc=add(client)
    client.app.state.library.update(doc['id'],status='running')
    response=client.patch('/api/documents/'+doc['id'],json={'text':'不能覆盖正在识别的任务'})
    assert response.status_code==400
