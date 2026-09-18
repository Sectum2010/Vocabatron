"""Application operations shared by CLI and a future local background worker."""
from __future__ import annotations

import importlib.metadata
from pathlib import Path
import resource
import time
import uuid

from . import __version__
from .clues import Ollama, select
from .domain import FrozenClues, Layout, Lesson, PrivateConfig, Problem, Selection, digest, selected_candidates
from .ingest import ingest, check_transcription
from .pdf import calibrate, clue_capacity, export_pdf, verify_pdf
from .solver import solve_pair
from .storage import PrivateStore, identifier, sha256
from .validation import validate_layout, validate_pair, public_structure_summary


def configuration(store):
    config=PrivateConfig.model_validate(store.read("config.json"))
    for prefix in config.prefixes:identifier(prefix)
    if config.prefixes[0]==config.prefixes[1]:raise Problem("DUPLICATE_PREFIX","两份结果的文件前缀须不同")
    store.path(config.source);store.path(config.template)
    return config


def import_lesson(store):
    config=configuration(store)
    with store.exclusive():
        lesson,report=ingest(store.path(config.source),config.lesson,store)
        store.json(f"ingest/versions/{lesson.version}/lesson.json",lesson)
        store.json(f"ingest/versions/{lesson.version}/coverage.json",report)
        for name in ("transcript.md","raw-pages.json","poppler.txt","input-security.json"):
            store.write(f"ingest/versions/{lesson.version}/{name}",store.path(f"ingest/{name}").read_bytes())
    return {"status":report["status"],"words":len(lesson.words),"lesson":lesson.lesson,"version":lesson.version}


def load_inputs(store):
    config=configuration(store)
    lesson=Lesson.model_validate(store.read("ingest/lesson.json"))
    if sha256(store.path(config.source))!=lesson.source_sha256 or config.lesson!=lesson.lesson:
        raise Problem("SOURCE_CHANGED","原件或课号变化，须重新导入并选择线索")
    frozen=FrozenClues.model_validate(store.read("clues/frozen.json"))
    selected_candidates(lesson,frozen)
    return config,lesson,frozen


def select_clues(store,*,new_version=False,adapter=None):
    config=configuration(store);lesson=Lesson.model_validate(store.read("ingest/lesson.json"))
    if sha256(store.path(config.source))!=lesson.source_sha256:raise Problem("SOURCE_CHANGED","原件已变化")
    with store.exclusive():
        if store.path("clues/frozen.json").exists() and not new_version:
            frozen=FrozenClues.model_validate(store.read("clues/frozen.json"));selected_candidates(lesson,frozen)
            return {"status":"FROZEN_REUSED","count":len(frozen.choices),"version":frozen.version}
        frozen=select(lesson,adapter or Ollama(),store)
        return {"status":"FROZEN","count":len(frozen.choices),"version":frozen.version}


def dependencies():
    return {p:importlib.metadata.version(p) for p in ("ortools","pdfplumber","pypdf","reportlab","pydantic","pypdfium2","Pillow")}


def _export_set(store,task_id,config,lesson,frozen,layouts,profile,template,metrics):
    started=time.perf_counter();pair=validate_pair(lesson,*layouts)
    structural=[public_structure_summary(validate_layout(lesson,l)) for l in layouts]
    metrics["independent_validation_seconds"]=time.perf_counter()-started
    stage=store.stage(task_id)
    store.json(stage/"lesson.json",lesson);store.json(stage/"clues.json",frozen)
    store.json(stage/"config.json",config);store.json(stage/"template-profile.json",profile)
    store.write(stage/"template.pdf",Path(template).read_bytes())
    reports=[];export_seconds=[];pdf_seconds=[]
    for index,(prefix,layout) in enumerate(zip(config.prefixes,layouts),1):
        store.json(stage/f"layout-{index}.json",layout)
        filename=f"{prefix}_Lesson_{lesson.lesson}_Crossword.pdf"
        output=store.path(stage/filename)
        started=time.perf_counter();export_pdf(template,output,lesson,layout,frozen,profile)
        export_seconds.append(time.perf_counter()-started)
        started=time.perf_counter()
        report=verify_pdf(template,output,lesson,layout,frozen,profile,expected_name=filename,
                          preview_dir=store.path(stage/f"previews/version-{index}"))
        pdf_seconds.append(time.perf_counter()-started)
        store.json(stage/f"pdf-verification-{index}.json",report)
        reports.append(report)
    metrics.update({"pdf_export_seconds":export_seconds,"final_pdf_validation_seconds":pdf_seconds,
                    "peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    if "_started_monotonic" in metrics:
        metrics["total_seconds"]=time.perf_counter()-metrics.pop("_started_monotonic")
    store.json(stage/"metrics.json",metrics)
    manifest={"schema_version":1,"status":"VERIFIED","task_id":task_id,"lesson_version":lesson.version,
              "source_sha256":lesson.source_sha256,"frozen_version":frozen.version,"template_sha256":profile["template_sha256"],
              "generator_version":__version__,"dependencies":dependencies(),"structures":structural,"pair":pair,
              "manual_review":"NOT_PERFORMED","pdfs":[{"name":r["filename"],"sha256":r["sha256"],"pages":2} for r in reports]}
    manifest["generator_source_sha256"]=digest([(p.name,sha256(p)) for p in sorted(Path(__file__).parent.glob("*.py"))])
    manifest["snapshot_hashes"]={name:sha256(store.path(stage/name)) for name in (
        "lesson.json","clues.json","config.json","template-profile.json","template.pdf","layout-1.json","layout-2.json","metrics.json","pdf-verification-1.json","pdf-verification-2.json")}
    destination=store.publish(stage,task_id,manifest)
    return {"status":"VERIFIED","task_id":task_id,"directory":str(destination.relative_to(store.root)),"words":len(lesson.words),"pdf_count":2}


def generate(store,task_id=None,options=None,progress=None):
    config,lesson,frozen=load_inputs(store)
    task_id=identifier(task_id or f"lesson-{lesson.lesson}-{uuid.uuid4().hex[:12]}")
    task=Path("tasks")/task_id
    with store.exclusive():
        if store.path(Path("results")/task_id).exists():
            return verify_set(store,task_id)
        template=store.path(config.template);profile=calibrate(template)
        identity=digest([lesson.version,frozen.version,profile["template_sha256"],config.prefixes])
        if store.path(task/"identity.json").exists() and store.read(task/"identity.json")["identity"]!=identity:
            raise Problem("TASK_INPUT_CHANGED","任务 ID 已绑定其他输入或配置")
        store.json(task/"identity.json",{"identity":identity})
        # A new explicit invocation is a retry; do not inherit a previous cancel request.
        store.path(task/"cancel.request").unlink(missing_ok=True)
        store.json(task/"lesson.json",lesson);store.json(task/"clues.json",frozen);store.json(task/"profile.json",profile)
        started=time.perf_counter();options=options or config.solver
        def emit(stage,value):
            store.json(task/"state.json",{"stage":stage,"heartbeat":time.time(),"owner":"local-exclusive-process","data":value})
            if stage in {"FIRST_VERIFIED","SECOND_VERIFIED"}:
                store.json(task/("layout-1.json" if stage=="FIRST_VERIFIED" else "layout-2.json"),value)
            if progress:progress(stage)
        def cancel():return store.path(task/"cancel.request").exists()
        try:
            costs,capacities=clue_capacity(lesson,frozen,profile)
            saved=Layout.model_validate(store.read(task/"layout-1.json")) if store.path(task/"layout-1.json").exists() else None
            layouts,solver_metrics,pair=solve_pair(lesson,options,clue_costs=costs,capacities=capacities,
                                                   progress=emit,cancel=cancel,saved_first=saved)
            emit("EXPORTING",{})
            result=_export_set(store,task_id,config,lesson,frozen,layouts,profile,template,
                               {"solver":solver_metrics,"parameters":options.model_dump(),"_started_monotonic":started})
            emit("COMPLETE",result)
            return result
        except Problem as e:
            emit(e.code,{"message":e.message,"details":e.details})
            raise
        except KeyboardInterrupt:
            emit("CANCELLED",{})
            raise Problem("CANCELLED","进程已中断；已验证布局保留，可用同一任务 ID 继续") from None


def _read_result(store,task_id):
    root=Path("results")/identifier(task_id);manifest=store.read(root/"manifest.json")
    if manifest.get("status")!="VERIFIED" or len(manifest.get("pdfs",[]))!=2:raise Problem("INCOMPLETE_SET","结果清单不完整")
    for name,expected in manifest["snapshot_hashes"].items():
        if Path(name).name!=name:raise Problem("UNSAFE_PATH","快照名称不合法")
        if sha256(store.path(root/name))!=expected:raise Problem("SNAPSHOT_CHANGED","结果快照哈希不符")
    lesson=Lesson.model_validate(store.read(root/"lesson.json"));frozen=FrozenClues.model_validate(store.read(root/"clues.json"))
    layouts=tuple(Layout.model_validate(store.read(root/f"layout-{i}.json")) for i in (1,2))
    return root,manifest,lesson,frozen,layouts


def verify_set(store,task_id):
    root,manifest,lesson,frozen,layouts=_read_result(store,task_id)
    profile=store.read(root/"template-profile.json");template=store.path(root/"template.pdf")
    validate_pair(lesson,*layouts);selected_candidates(lesson,frozen)
    config=PrivateConfig.model_validate(store.read(root/"config.json"))
    names=[f"{prefix}_Lesson_{lesson.lesson}_Crossword.pdf" for prefix in config.prefixes]
    if [x["name"] for x in manifest["pdfs"]]!=names:raise Problem("PDF_FILENAME","清单文件名与配置不符")
    for layout,item in zip(layouts,manifest["pdfs"]):
        output=store.path(root/item["name"])
        if sha256(output)!=item["sha256"]:raise Problem("PDF_CHANGED","PDF 哈希与完成清单不符")
        verify_pdf(template,output,lesson,layout,frozen,profile,expected_name=item["name"])
    return {"status":"VERIFIED","task_id":task_id,"words":len(lesson.words),"pdf_count":2}


def task_status(store,task_id):
    task_id=identifier(task_id)
    # The durable publication manifest is authoritative after a crash or rebuild.
    if store.path(Path("results")/task_id).exists():
        root,manifest,lesson,frozen,layouts=_read_result(store,task_id)
        for item in manifest["pdfs"]:
            if Path(item["name"]).name!=item["name"]:raise Problem("UNSAFE_PATH","结果文件名越界")
            path=store.path(root/item["name"])
            if not path.is_file() or sha256(path)!=item["sha256"]:
                raise Problem("INCOMPLETE_SET","完成清单与实际 PDF 不符")
        return {"stage":"COMPLETE","task_id":task_id,"pdf_count":2}
    state=store.read(Path("tasks")/task_id/"state.json")
    return {"stage":state["stage"],"heartbeat":state["heartbeat"]}


def rebuild(store,task_id,new_task_id):
    with store.exclusive():
        root,manifest,lesson,frozen,layouts=_read_result(store,task_id)
        config=PrivateConfig.model_validate(store.read(root/"config.json"))
        return _export_set(store,identifier(new_task_id),config,lesson,frozen,layouts,
            store.read(root/"template-profile.json"),store.path(root/"template.pdf"),
            {"operation":"REBUILD","model_call":"NOT_RUN","solver":"NOT_RUN",
             "origin_task_id":task_id,"origin_manifest_sha256":sha256(store.path(root/"manifest.json")),
             "original_generation_metrics":store.read(root/"metrics.json"),"_started_monotonic":time.perf_counter()})


def private_acceptance(store,task_id):
    config,lesson,frozen=load_inputs(store)
    root,manifest,saved_lesson,saved_frozen,layouts=_read_result(store,task_id)
    if saved_lesson.version!=lesson.version or saved_frozen.version!=frozen.version or manifest["template_sha256"]!=sha256(store.path(config.template)):
        raise Problem("ACCEPTANCE_INPUT_MISMATCH","结果集不属于当前私人验收原件、课表或冻结版本")
    if config.expected_words is None:raise Problem("EXPECTED_COUNT_REQUIRED","私人验收需显式配置预期词数")
    if len(lesson.words)!=config.expected_words:raise Problem("ACCEPTANCE_WORD_COUNT","原件主词数与私人验收预期不符")
    check_transcription(store.path(config.source),lesson,store)
    result=verify_set(store,task_id)
    response_dir=store.path(f"model/{lesson.version}")
    if not response_dir.exists() or not list(response_dir.glob("batch-*-attempt-*.json")):
        raise Problem("REAL_MODEL_EVIDENCE_MISSING","缺少真实模型响应证据")
    metrics=store.read("model/metrics.json")
    if not metrics.get("calls") or any(not c.get("eval_count") for c in metrics["calls"]):
        raise Problem("REAL_MODEL_EVIDENCE_MISSING","缺少模型推理计数")
    if metrics["inspection"]["digest"]!=frozen.model_digest or metrics["inspection"]["version"]!=frozen.ollama_version:
        raise Problem("REAL_MODEL_EVIDENCE_MISMATCH","模型版本证据与冻结记录不符")
    latest={}
    for path in response_dir.glob("batch-*-attempt-*.json"):
        parts=path.stem.split("-");batch,attempt=int(parts[1]),int(parts[3])
        if batch not in latest or attempt>latest[batch][0]:latest[batch]=(attempt,path)
    choices=[]
    for _,path in (latest[k] for k in sorted(latest)):
        import json
        response=json.loads(path.read_text())
        if response.get("model")!=frozen.model or not response.get("done"):
            raise Problem("REAL_MODEL_EVIDENCE_MISMATCH","模型响应未完整结束或模型名称不符")
        choices.extend(Selection.model_validate_json(response["message"]["content"]).choices)
    if tuple(choices)!=frozen.choices:
        raise Problem("REAL_MODEL_EVIDENCE_MISMATCH","冻结选择与保留的真实模型响应不符")
    result.update({"transcription":"VERIFIED","real_model":"EVIDENCED","manual_review":"NOT_PERFORMED"})
    store.json("acceptance/report.json",result)
    return result


def benchmark_model(store,sample_words=1):
    """Explicit, bounded protocol timing. Never changes the application's frozen clues."""
    config,lesson,frozen=load_inputs(store)
    if not 1<=sample_words<=len(lesson.words):raise Problem("BENCHMARK_SIZE","样本词条数不合法")
    subset=lesson.model_copy(update={"words":lesson.words[:sample_words]})
    root=Path("benchmarks")/("model-"+uuid.uuid4().hex[:12])
    measurements=[]
    with store.exclusive():
        original_hash=sha256(store.path("clues/frozen.json"))
        for label in ("initial","warm"):
            scoped=PrivateStore(store.path(root/label))
            select(subset,Ollama(),scoped)
            metrics=scoped.read("model/metrics.json")
            measurements.append({"label":label,"sample_words":sample_words,
                "already_loaded":metrics["inspection"]["already_loaded"],"calls":metrics["calls"]})
        if sha256(store.path("clues/frozen.json"))!=original_hash:
            raise Problem("FROZEN_SELECTION_CHANGED","性能采样意外改变正式冻结版本")
        report={"status":"MEASURED","scope":"small identical protocol sample, not full-lesson latency",
                "measurements":measurements,"frozen_version_unchanged":frozen.version}
        store.json("benchmarks/model-timing.json",report)
    return report
