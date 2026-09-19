import json

import pytest

from vocabatron.clues import MODEL, Ollama, budget, select
from vocabatron.domain import Choice, Fragment, Problem, check_choices, selected_candidates
from vocabatron.ingest import split_candidates
from vocabatron.storage import PrivateStore
from .helpers import frozen_for, lesson_of
from .helpers import synthetic_handout
from vocabatron.ingest import ingest


def test_multiline_phrase_and_punctuation_preserved():
    f=Fragment(page=2,bbox=(10,20,90,50),raw="SYNONYM: made\n up phrase, oddly-spelled!, don't alter")
    cs=split_candidates(f,"word-a")
    assert [c.text for c in cs]==["made up phrase","oddly-spelled!","don't alter"]
    assert all(c.source.raw==f.raw for c in cs)
    single=split_candidates(f.model_copy(update={"raw":"ANTONYM: no separator phrase"}),"word-a")
    assert len(single)==1 and single[0].relation=="ANTONYM"
    with pytest.raises(Problem,match="CANDIDATE_BOUNDARY"):split_candidates(f.model_copy(update={"raw":"SYNONYM: first,,third"}),"word-a")


@pytest.mark.parametrize("mode",["omit","duplicate","wrong_candidate","extra","invalid_json","truncated","extra_text"])
def test_bounded_model_failure_modes(tmp_path,mode):
    lesson=lesson_of(["AB","AC"]);store=PrivateStore(tmp_path/"private");calls=[]
    def transport(endpoint,payload):
        if endpoint=="version":return {"version":"test"}
        if endpoint=="tags":return {"models":[{"name":MODEL,"digest":"synthetic"}]}
        if endpoint=="ps":return {"models":[]}
        if endpoint=="show":return {"model_info":{"test.context_length":32768}}
        calls.append(payload)
        choices=[{"word_id":w.word_id,"candidate_id":w.candidates[0].candidate_id} for w in lesson.words]
        if mode=="omit":choices.pop()
        if mode=="duplicate":choices.append(choices[0])
        if mode=="wrong_candidate":choices[0]["candidate_id"]=choices[1]["candidate_id"]
        if mode=="extra":choices.append({"word_id":"extra","candidate_id":"extra"})
        if mode=="extra_text":choices[0]["text"]="model invented text"
        return {"model":MODEL,"done":True,"done_reason":"length" if mode=="truncated" else "stop","message":{"content":"{" if mode=="invalid_json" else json.dumps({"choices":choices})}}
    with pytest.raises(Problem,match="MODEL_SELECTION_INVALID"):select(lesson,Ollama(transport),store,retries=1)
    assert len(calls)==2
    assert all(c["model"]==MODEL and isinstance(c["format"],dict) and "tools" not in c for c in calls)
    assert not store.path("clues/frozen.json").exists()


def test_success_and_cache_isolation(tmp_path):
    l=lesson_of(["AB","AC"]);f=frozen_for(l)
    selected_candidates(l,f)
    for changed in (l.model_copy(update={"lesson":4}),l.model_copy(update={"source_sha256":"other"})):
        with pytest.raises(Problem,match="STALE_SELECTION"):selected_candidates(changed,f)
    # Global clue uniqueness is a preference only.
    ws=tuple(w.model_copy(update={"candidates":(w.candidates[0].model_copy(update={"text":"same original clue"}),)}) for w in l.words)
    same=l.model_copy(update={"words":ws});assert len(selected_candidates(same,frozen_for(same)))==2


def test_token_budget_scales_with_complete_records():
    small=lesson_of(["AB","AC"]);large=lesson_of(["A"+chr(65+i)+"B" for i in range(25)])
    _,a=budget(small.words);payload,b=budget(large.words)
    assert len(payload)==25 and b["num_predict"]>a["num_predict"]


def test_no_model_substitution(tmp_path):
    def transport(endpoint,payload):
        if endpoint=="version":return {"version":"test"}
        return {"models":[{"name":"different","digest":"synthetic"}]}
    with pytest.raises(Problem,match="MODEL_MISSING"):Ollama(transport).inspect()
    with pytest.raises(Problem,match="MODEL_REJECTED"):Ollama(transport).request("chat",{"model":"different"})


def test_full_text_layer_import_with_independent_extractor(tmp_path):
    source=tmp_path/"synthetic.pdf";synthetic_handout(source);store=PrivateStore(tmp_path/"private")
    lesson,report=ingest(source,3,store)
    assert report["status"]=="VERIFIED" and len(lesson.words)==2
    assert all(w.forms and w.pronunciation and w.definition and w.examples for w in lesson.words)
    assert lesson.words[0].candidates[0].text=="made up phrase"
    assert store.path(__import__("pathlib").Path(store.read("current.json")["lesson_path"]).with_name("transcript.md")).read_text().count("synthetic example")==2
    with pytest.raises(Problem,match="LESSON_REQUIRES_DECISION"):ingest(source,4,store)


def test_second_extractor_disagreement_is_not_called_verified(tmp_path,monkeypatch):
    import subprocess
    source=tmp_path/"synthetic.pdf";synthetic_handout(source);store=PrivateStore(tmp_path/"private")
    real=subprocess.run
    def altered(*args,**kwargs):
        r=real(*args,**kwargs)
        from pathlib import Path
        target=Path(args[0][-1]);target.write_bytes(target.read_bytes().replace(b"invented",b"omitted",1))
        return r
    monkeypatch.setattr(subprocess,"run",altered)
    from vocabatron.ingest import extract_local
    with pytest.raises(Problem,match="TRANSCRIPTION_REQUIRES_DECISION"):extract_local(source,3)


def test_explicit_benchmark_preserves_formal_frozen_version(tmp_path,monkeypatch):
    from vocabatron import services
    from vocabatron.storage import sha256
    store=PrivateStore(tmp_path/"private");source=store.path("sources/synthetic.pdf")
    source.parent.mkdir();source.write_bytes(b"synthetic source bytes")
    lesson=lesson_of(["AB","AC"]).model_copy(update={"source_sha256":sha256(source)})
    frozen=frozen_for(lesson)
    store.json("config.json",{"lesson":3,"source":"sources/synthetic.pdf","template":"sources/template.pdf","prefixes":["VersionA","VersionB"]})
    store.json("ingest/lesson.json",lesson);store.json("clues/frozen.json",frozen)
    before=sha256(store.path("clues/frozen.json"));state={"loaded":False}
    def transport(endpoint,payload):
        if endpoint=="version":return {"version":"synthetic"}
        if endpoint=="tags":return {"models":[{"name":MODEL,"digest":"synthetic"}]}
        if endpoint=="ps":return {"models":[{"name":MODEL}] if state["loaded"] else []}
        if endpoint=="show":return {"model_info":{"synthetic.context_length":32768}}
        state["loaded"]=True
        words=json.loads(payload["messages"][1]["content"])["untrusted_source_data"]
        choices=[{"word_id":w["word_id"],"candidate_id":w["candidates"][0]["candidate_id"]} for w in words]
        return {"model":MODEL,"done":True,"done_reason":"stop","eval_count":12,"message":{"content":json.dumps({"choices":choices})}}
    monkeypatch.setattr(services,"Ollama",lambda:Ollama(transport))
    result=services.benchmark_model(store,1)
    assert result["measurements"][1]["already_loaded"] is True
    assert sha256(store.path("clues/frozen.json"))==before
