import json
from vocabatron.private_fingerprints import Fingerprints,source_fingerprints
from vocabatron.domain import Fragment
from .test_web_app import library,add_lesson
from .helpers import lesson_of


def test_matching_overlapping_terms_and_escaped_whitespace():
    matcher=Fingerprints(['invented private sentence','private sentence ending','ending marker'])
    assert matcher.matches('prefix invented\\nprivate sentence suffix')
    assert matcher.matches('a private sentence ending here')
    assert not matcher.matches('An unrelated public paragraph')


def test_archived_library_source_is_in_private_fingerprint_coverage(library):
    text='A completely invented confidential classroom sentence used only to verify this synthetic privacy boundary.'
    lesson=lesson_of(['AB','AC']);word=lesson.words[0].model_copy(update={'definition':(Fragment(page=1,bbox=(0,0,100,20),raw=text),)})
    lid,_=add_lesson(library,lesson.model_copy(update={'words':(word,*lesson.words[1:])}))
    with library.db.transaction() as c:c.execute('UPDATE lessons SET archived=1 WHERE id=?',(lid,))
    config=library.config.code_root/'.private/web/config.json';config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'database':str(library.config.database),'data_root':str(library.config.data_root)}))
    matcher,coverage=source_fingerprints(library.config.code_root,[])
    assert coverage['library_lessons']==1 and coverage['source_strings']>0
    assert matcher.matches('Embedded document: '+text)
    assert not matcher.matches('No private source data in this public fixture')


def test_vendor_pattern_review_never_allows_changed_or_private_content(monkeypatch):
    import hashlib
    from vocabatron import privacy
    raw=('invented encoded sequence@nonexistent.'+'example').encode()
    monkeypatch.setattr(privacy,'VENDOR_ENCODED_DATA',{hashlib.sha256(raw).hexdigest()})
    assert not privacy.inspect_text('invented-vendor.mjs',raw)
    assert any(i['rule']=='personal_email_in_content' for i in privacy.inspect_text('invented-vendor.mjs',raw+b' changed'))
    assert any(i['rule']=='private_value' for i in privacy.inspect_text('invented-vendor.mjs',raw,Fingerprints(['invented encoded sequence'])))
