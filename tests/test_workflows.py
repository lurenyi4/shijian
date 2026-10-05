import zipfile

import pytest
from PIL import Image

from storage import Library


def library_with_text(tmp_path):
    library = Library(tmp_path / 'library', seed=False)
    source = tmp_path / 'sample.png'
    Image.new('RGB', (40, 40), 'white').save(source)
    doc, _ = library.import_file(source, source.name)
    with library.connect() as db:
        db.execute('UPDATE documents SET pages=2 WHERE id=?', (doc['id'],))
    doc = library.edit(doc['id'], {'text': '第一行\n第二行\n第三行'})
    return library, doc


def test_page_anchors_require_current_text_and_order(tmp_path):
    library, doc = library_with_text(tmp_path)
    version = library.page_notes(doc['id'])['text_version']
    library.save_page_note(doc['id'], 1, 1, True, version)
    library.save_page_note(doc['id'], 2, 3, False, version)
    with pytest.raises(ValueError):
        library.save_page_note(doc['id'], 2, 1, False, version)
    library.edit(doc['id'], {'text': '更改后的正文'})
    assert library.page_notes(doc['id'])['pages'][0]['stale']
    with pytest.raises(ValueError):
        library.save_page_note(doc['id'], 1, 1, True, version)


def test_restoring_same_text_does_not_restore_page_confirmation(tmp_path):
    library, doc = library_with_text(tmp_path)
    notes = library.page_notes(doc['id'])
    library.save_page_note(doc['id'], 1, 1, True, notes['text_version'])
    library.edit(doc['id'], {'text': '中间版本'})
    restored = library.edit(doc['id'], {'text': doc['text']})
    assert library.page_notes(doc['id'])['pages'][0]['stale']
    assert restored['text_revision'] > doc['text_revision']


def test_page_updates_change_sync_token(tmp_path):
    library, doc = library_with_text(tmp_path)
    token = library.change_token()
    notes = library.page_notes(doc['id'])
    library.save_page_note(doc['id'], 1, 1, True, notes['text_version'])
    assert library.change_token() != token


def test_collection_merge_updates_trash_and_draft(tmp_path):
    library, doc = library_with_text(tmp_path)
    library.edit(doc['id'], {'collection': '旧诗集', 'trashed': True})
    current = library.get(doc['id'])
    library.save_draft(doc['id'], {'collection': '旧诗集', 'base_updated_at': current['updated_at']})
    library.rename_collection('旧诗集', '新诗集')
    assert library.get(doc['id'])['collection'] == '新诗集'
    assert library.draft(doc['id'])['collection'] == '新诗集'
    with pytest.raises(ValueError):
        library.remove_empty_collection('新诗集')


def test_backup_manifest_and_verified_restore(tmp_path):
    from archive_restore import inspect_backup, restore_backup
    library, doc = library_with_text(tmp_path)
    backup = library.backup()
    info = inspect_backup(backup)
    assert info['verified'] and info['files'] >= 3
    destination = tmp_path / 'restored'
    restore_backup(backup, destination)
    restored = Library(destination, seed=False)
    assert restored.get(doc['id'])['text'] == doc['text']
    with pytest.raises(ValueError):
        restore_backup(backup, destination)
    bad = tmp_path / 'corrupt.zip'
    with zipfile.ZipFile(backup) as src, zipfile.ZipFile(bad, 'w') as out:
        for name in src.namelist():
            raw = src.read(name)
            if name.startswith('originals/'):
                raw = b'broken'
            out.writestr(name, raw)
    with pytest.raises(ValueError):
        inspect_backup(bad)


def test_preference_patch_preserves_other_settings(tmp_path):
    library, _ = library_with_text(tmp_path)
    library.set_setting('preferences', {'large_text': True})
    library.preferences({'vertical': True, 'font_size': 28})
    assert library.setting('preferences')['large_text'] is True
