"""Application operations shared by CLI and a future local background worker."""
from __future__ import annotations

import importlib.metadata
import math
from pathlib import Path
import resource
import time
import uuid

from . import __version__
from .clues import Ollama, select
from .domain import FrozenClues, Layout, Lesson, PrivateConfig, Problem, Selection, digest, selected_candidates
from .ingest import extract, write_bundle, check_transcription
from .pdf import calibrate, clue_capacity, export_pdf, verify_pdf, plan
from .execution import Execution, checkpoint, CURRENT, owner_identity, cancel_attempt
from .requests import request_identity
from .manifest import SNAPSHOTS, validate_manifest
from .evidence import verify_evidence
from .solver import solve_pair
from .storage import PrivateStore, identifier, sha256
from .validation import validate_layout, validate_pair, public_structure_summary


def configuration(store):
    config=PrivateConfig.model_validate(store.read("config.json"))
    for prefix in config.prefixes:identifier(prefix)
    if config.prefixes[0]==config.prefixes[1]:raise Problem("DUPLICATE_PREFIX","两份结果的文件前缀须不同")
    store.path(config.source);store.path(config.template)
    return config


def current_lesson(store):
    if store.path("current.json").exists():
        pointer=store.read("current.json")
        if pointer.get("schema_version")!=2:raise Problem("INPUT_INVALID","当前版本指针格式错误")
        return Lesson.model_validate(store.read(pointer["lesson_path"])),pointer
    return Lesson.model_validate(store.read("ingest/lesson.json")),None


def import_lesson(store, *, task_id=None, progress=None):
    with store.exclusive():
        config=configuration(store)
        task_id=identifier(task_id or "import-"+uuid.uuid4().hex[:16])
        with Execution(store,task_id,digest(config),progress=progress,pdf_limits=config.resources,total_seconds=config.total_seconds,stage_seconds=config.stage_seconds) as context:
            context.emit("IMPORTING")
            bundle=extract(store.path(config.source),config.lesson)
            lesson=Lesson.model_validate(bundle["lesson"])
            if config.expected_words is not None and len(lesson.words)!=config.expected_words:raise Problem("INPUT_INVALID","词数与显式配置不符")
            version=Path("imports")/context.attempt_id
            write_bundle(store,version,bundle)
            context.emit("PUBLISHING_IMPORT")
            with store.commit_lock():
                checkpoint(force_cancel=True)
                store.json("current.json",{"schema_version":2,"lesson_path":str(version/"lesson.json"),"frozen_path":None})
                context.publication_committed=True
            result={"status":"VERIFIED","words":len(lesson.words),"lesson":lesson.lesson,"version":lesson.version}
            context.finish("COMPLETE",result)
            return result


def load_inputs(store):
    with store.exclusive():
        config=configuration(store);lesson,pointer=current_lesson(store)
        if sha256(store.path(config.source))!=lesson.source_sha256 or config.lesson!=lesson.lesson:
            raise Problem("SOURCE_CHANGED","原件或课号变化，须重新导入并选择线索")
        if pointer:
            path=pointer.get("frozen_path")
            if not path:raise Problem("SELECTION_REQUIRED","当前课表尚未冻结线索")
        elif store.path("clues/current.json").exists():path="clues/versions/"+identifier(store.read("clues/current.json")["version"])+".json"
        else:path="clues/frozen.json"
        frozen=FrozenClues.model_validate(store.read(path));selected_candidates(lesson,frozen)
        return config,lesson,frozen


def select_clues(store,*,new_version=False,adapter=None,task_id=None,progress=None):
    with store.exclusive():
        config=configuration(store);lesson,pointer=current_lesson(store)
        if sha256(store.path(config.source))!=lesson.source_sha256:raise Problem("SOURCE_CHANGED","原件已变化")
        if not new_version:
            try:
                _,_,frozen=load_inputs(store)
                verify_evidence(store,lesson,frozen)
                return {"status":"FROZEN_REUSED","count":len(frozen.choices),"version":frozen.version}
            except (FileNotFoundError,Problem) as exc:
                if isinstance(exc,Problem) and exc.code not in {"SELECTION_REQUIRED","STALE_SELECTION"}:raise
        task_id=identifier(task_id or "select-"+uuid.uuid4().hex[:16])
        with Execution(store,task_id,lesson.content_version,progress=progress,pdf_limits=config.resources,total_seconds=config.total_seconds,stage_seconds=config.stage_seconds) as context:
            context.emit("SELECTING")
            frozen=select(lesson,adapter or Ollama(),store,publish=False)
            verify_evidence(store,lesson,frozen)
            context.emit("PUBLISHING_SELECTION")
            pointer=pointer or {"schema_version":2,"lesson_path":"ingest/lesson.json"}
            with store.commit_lock():
                checkpoint(force_cancel=True)
                store.json("current.json",{**pointer,"frozen_path":f"clues/versions/{frozen.version}.json"})
                context.publication_committed=True
            result={"status":"FROZEN","count":len(frozen.choices),"version":frozen.version,"selection_run_id":frozen.selection_run_id}
            context.finish("COMPLETE",result)
            return result


def check_lesson(store, *, task_id=None, progress=None):
    with store.exclusive():
        config=configuration(store);lesson,_=current_lesson(store)
        with Execution(store,identifier(task_id or "check-"+uuid.uuid4().hex[:16]),lesson.content_version,progress=progress,pdf_limits=config.resources,total_seconds=config.total_seconds,stage_seconds=config.stage_seconds) as context:
            context.emit("CHECKING_TRANSCRIPT")
            report=check_transcription(store.path(config.source),lesson,store)
            result={"status":report["status"],"words":len(lesson.words)}
            context.finish("COMPLETE",result);return result


def dependencies():
    return {p:importlib.metadata.version(p) for p in ("ortools","pdfplumber","pypdf","reportlab","pydantic","pypdfium2","Pillow","numpy")}


def _export_set(store,task_id,config,lesson,frozen,layouts,profile,template,metrics):
    started=time.perf_counter();pair=validate_pair(lesson,*layouts)
    structural=[public_structure_summary(validate_layout(lesson,l)) for l in layouts]
    metrics["independent_validation_seconds"]=time.perf_counter()-started
    checkpoint()
    stage=store.stage(task_id)
    store.json(stage/"lesson.json",lesson);store.json(stage/"clues.json",frozen)
    store.json(stage/"config.json",config);store.json(stage/"template-profile.json",profile)
    store.write(stage/"template.pdf",Path(template).read_bytes())
    template=store.path(stage/"template.pdf")
    if sha256(template)!=profile["template_sha256"]:raise Problem("TASK_INPUT_CHANGED","模板在保存快照前发生变化")
    reports=[];export_seconds=[];pdf_seconds=[]
    for index,(prefix,layout) in enumerate(zip(config.prefixes,layouts),1):
        checkpoint()
        store.json(stage/f"layout-{index}.json",layout)
        filename=f"{prefix}_Lesson_{lesson.lesson}_Crossword.pdf"
        output=store.path(stage/filename)
        context=CURRENT.get()
        if context:context.emit(f"EXPORTING_PDF_{index}")
        started=time.perf_counter();export_pdf(template,output,lesson,layout,frozen,profile)
        export_seconds.append(time.perf_counter()-started)
        started=time.perf_counter()
        if context:context.emit(f"VALIDATING_PDF_{index}")
        report=verify_pdf(template,output,lesson,layout,frozen,profile,expected_name=filename,
                          preview_dir=store.path(stage/f"previews/version-{index}"))
        pdf_seconds.append(time.perf_counter()-started)
        store.json(stage/f"pdf-verification-{index}.json",report)
        reports.append(report)
    metrics.update({"pdf_export_seconds":export_seconds,"final_pdf_validation_seconds":pdf_seconds,
                    "peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    if "_started_monotonic" in metrics:
        metrics["total_seconds"]=time.perf_counter()-metrics.pop("_started_monotonic")
    context=CURRENT.get()
    metrics["process_memory"]={"application_peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "scope":"Linux main application process lifetime maximum, not aggregate or per-task delta",
        "subprocesses":context.subprocesses if context else [],"solver_threads":config.solver.workers,
        "application_observed_threads":len(list(Path("/proc/self/task").iterdir()))}
    store.json(stage/"metrics.json",metrics)
    manifest={"schema_version":2,"status":"VERIFIED","task_id":task_id,"lesson_version":lesson.version,
              "source_sha256":lesson.source_sha256,"frozen_version":frozen.version,"template_sha256":profile["template_sha256"],
              "generator_version":__version__,"dependencies":dependencies(),"structures":structural,"pair":pair,
              "manual_review":"NOT_PERFORMED","pdfs":[{"name":r["filename"],"sha256":r["sha256"],"pages":2} for r in reports]}
    manifest["generator_source_sha256"]=digest([(p.name,sha256(p)) for p in sorted(Path(__file__).parent.glob("*.py"))])
    manifest["snapshot_hashes"]={name:sha256(store.path(stage/name)) for name in (
        "lesson.json","clues.json","config.json","template-profile.json","template.pdf","layout-1.json","layout-2.json","metrics.json","pdf-verification-1.json","pdf-verification-2.json")}
    manifest["request_identity"]=request_identity(config,lesson,frozen,profile["template_sha256"])
    manifest["request_sha256"]=digest(manifest["request_identity"])
    validate_manifest(manifest,task_id)
    checkpoint()
    if context:context.emit("PUBLISHING")
    try:destination=store.publish(stage,task_id,manifest)
    except OSError:raise Problem("PUBLISH_FAILED","完整结果尚未发布；可重试已验证布局") from None
    return {"status":"VERIFIED","task_id":task_id,"directory":str(destination.relative_to(store.root)),"words":len(lesson.words),"pdf_count":2}


def generate(store,task_id=None,options=None,progress=None):
    with store.exclusive():
        if task_id is not None:task_id=identifier(task_id)
        bound=task_id is not None and (store.path(Path("tasks")/task_id/"identity.json").exists() or store.path(Path("results")/task_id).exists())
        try:config,lesson,frozen=load_inputs(store)
        except (Problem,FileNotFoundError,ValueError) as exc:
            if bound:raise Problem("TASK_INPUT_CHANGED","当前输入无法匹配任务原有请求；须显式处理输入变化") from None
            raise
        if options is not None and options.seed!=config.solver.seed:
            config=config.model_copy(update={"solver":config.solver.model_copy(update={"seed":options.seed})})
        task_id=identifier(task_id or f"lesson-{lesson.lesson}-{uuid.uuid4().hex[:12]}")
        task=Path("tasks")/task_id
        template=store.path(config.template)
        identity=request_identity(config,lesson,frozen,sha256(template));identity_hash=digest(identity)
        if store.path(Path("results")/task_id).exists():
            root,manifest,old_lesson,old_frozen,layouts=_read_result(store,task_id)
            saved=manifest.get("request_identity") or request_identity(PrivateConfig.model_validate(store.read(root/"config.json")),old_lesson,old_frozen,manifest["template_sha256"])
            if identity!=saved:raise Problem("TASK_INPUT_CHANGED","已完成任务绑定了其他输入或交付配置")
            return verify_set(store,task_id)
        if store.path(task/"identity.json").exists() and store.read(task/"identity.json").get("identity")!=identity_hash:
            raise Problem("TASK_INPUT_CHANGED","任务 ID 已绑定其他输入或配置")
        store.immutable_json(task/"identity.json",{"schema_version":2,"identity":identity_hash,"request":identity})
        with Execution(store,task_id,identity_hash,progress=progress,pdf_limits=config.resources,total_seconds=config.total_seconds,stage_seconds=config.stage_seconds) as context:
            context.emit("CALIBRATING")
            profile=calibrate(template)
            if profile["template_sha256"]!=identity["template_sha256"]:raise Problem("TASK_INPUT_CHANGED","校准期间模板发生变化")
            verify_evidence(store,lesson,frozen)
            started=time.perf_counter();options=options or config.solver
            def emit(stage,value):
                context.emit(stage,value)
                if stage in {"FIRST_VERIFIED","SECOND_VERIFIED"}:
                    store.json(task/("layout-1.json" if stage=="FIRST_VERIFIED" else "layout-2.json"),value)
            def typography(layout):plan(lesson,layout,frozen,profile)
            costs,capacities=clue_capacity(lesson,frozen,profile)
            saved=[]
            for index in (1,2):
                path=task/f"layout-{index}.json"
                if not store.path(path).exists():break
                try:
                    layout=Layout.model_validate(store.read(path));validate_layout(lesson,layout);typography(layout)
                    if saved:validate_pair(lesson,saved[0],layout)
                    saved.append(layout)
                except (Problem,ValueError):
                    context.emit("CHECKPOINT_REJECTED",{"index":index});break
            if len(saved)==2:
                layouts=tuple(saved);solver_metrics={"status":"RESUMED_VERIFIED_PAIR","solver":"NOT_RUN"}
            else:
                layouts,solver_metrics,pair=solve_pair(lesson,options,clue_costs=costs,capacities=capacities,
                    progress=emit,cancel=context.cancelled,saved_first=saved[0] if saved else None,typography=typography)
            context.emit("OPTIMIZING")
            from .optimization import optimize_pair
            layouts,optimization=optimize_pair(lesson,layouts,options,typography,cancel=context.cancelled)
            solver_metrics["optimization"]=optimization
            for index,layout in enumerate(layouts,1):store.json(task/f"layout-{index}.json",layout)
            context.emit("EXPORTING")
            result=_export_set(store,task_id,config,lesson,frozen,layouts,profile,template,
                {"solver":solver_metrics,"parameters":options.model_dump(),"_started_monotonic":started})
            try:context.finish("COMPLETE",result)
            except OSError:pass
            return result


def _read_result(store,task_id):
    root=Path("results")/identifier(task_id);manifest=store.read(root/"manifest.json")
    validate_manifest(manifest,task_id)
    for name,expected in manifest["snapshot_hashes"].items():
        if Path(name).name!=name:raise Problem("UNSAFE_PATH","快照名称不合法")
        if sha256(store.path(root/name))!=expected:raise Problem("SNAPSHOT_CHANGED","结果快照哈希不符")
    lesson=Lesson.model_validate(store.read(root/"lesson.json"));frozen=FrozenClues.model_validate(store.read(root/"clues.json"))
    layouts=tuple(Layout.model_validate(store.read(root/f"layout-{i}.json")) for i in (1,2))
    config=PrivateConfig.model_validate(store.read(root/"config.json"));profile=store.read(root/"template-profile.json")
    selected_candidates(lesson,frozen)
    if (manifest["lesson_version"]!=lesson.version or manifest["source_sha256"]!=lesson.source_sha256 or
        manifest["frozen_version"]!=frozen.version or manifest["template_sha256"]!=sha256(store.path(root/"template.pdf")) or
        profile["template_sha256"]!=manifest["template_sha256"] or config.lesson!=lesson.lesson):
        raise Problem("SNAPSHOT_CHANGED","清单和快照身份不一致")
    names=[f"{prefix}_Lesson_{lesson.lesson}_Crossword.pdf" for prefix in config.prefixes]
    if [x["name"] for x in manifest["pdfs"]]!=names:raise Problem("PDF_FILENAME","清单文件名与保存配置不符")
    if manifest.get("request_identity"):
        expected=request_identity(config,lesson,frozen,manifest["template_sha256"])
        # Historical delivery rules belong to that result, not current defaults.
        for key in ("output_rules","font_sha256"):expected[key]=manifest["request_identity"].get(key)
        if manifest["request_identity"]!=expected:
            raise Problem("SNAPSHOT_CHANGED","保存的请求身份与快照不符")
    return root,manifest,lesson,frozen,layouts


def verify_set(store,task_id):
    root,manifest,lesson,frozen,layouts=_read_result(store,task_id)
    profile=store.read(root/"template-profile.json");template=store.path(root/"template.pdf")
    validate_pair(lesson,*layouts);selected_candidates(lesson,frozen)
    evidence=verify_evidence(store,lesson,frozen)
    config=PrivateConfig.model_validate(store.read(root/"config.json"))
    names=[f"{prefix}_Lesson_{lesson.lesson}_Crossword.pdf" for prefix in config.prefixes]
    if [x["name"] for x in manifest["pdfs"]]!=names:raise Problem("PDF_FILENAME","清单文件名与配置不符")
    for layout,item in zip(layouts,manifest["pdfs"]):
        output=store.path(root/item["name"])
        if sha256(output)!=item["sha256"]:raise Problem("PDF_CHANGED","PDF 哈希与完成清单不符")
        verify_pdf(template,output,lesson,layout,frozen,profile,expected_name=item["name"])
    return {"status":"VERIFIED","task_id":task_id,"words":len(lesson.words),"pdf_count":2,"model_evidence":evidence["status"]}


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
    if not state.get("terminal") and state.get("stage")!="COMPLETE":
        owner=state.get("owner",{})
        actual=owner_identity(owner.get("pid")) if isinstance(owner,dict) and owner.get("pid") else None
        heartbeat=state.get("heartbeat")
        stale=(not isinstance(heartbeat,(int,float)) or not math.isfinite(heartbeat) or
               time.time()-heartbeat>15 or heartbeat>time.time()+5)
        active_path=Path("tasks")/task_id/"active.json"
        active=store.read(active_path) if store.path(active_path).exists() else {}
        changed_attempt=active.get("attempt_id")!=state.get("attempt_id")
        if stale or changed_attempt or not actual or any(actual.get(k)!=owner.get(k) for k in ("pid","start_ticks","boot_id")):
            return {"stage":"INTERRUPTED","task_id":task_id,"retryable":True,"attempt_id":state.get("attempt_id")}
    return {"stage":state["stage"],"heartbeat":state["heartbeat"],"attempt_id":state.get("attempt_id")}


def rebuild(store,task_id,new_task_id,*,progress=None):
    with store.exclusive():
        root,manifest,lesson,frozen,layouts=_read_result(store,task_id)
        verify_evidence(store,lesson,frozen)
        config=PrivateConfig.model_validate(store.read(root/"config.json"));new_task_id=identifier(new_task_id)
        identity=request_identity(config,lesson,frozen,manifest["template_sha256"])
        if store.path(Path("results")/new_task_id).exists():
            _,existing,_,_,_=_read_result(store,new_task_id)
            if existing.get("request_identity")!=identity:raise Problem("TASK_INPUT_CHANGED","重建任务绑定了其他请求")
            return verify_set(store,new_task_id)
        path=Path("tasks")/new_task_id/"identity.json"
        value={"schema_version":2,"identity":digest(identity),"request":identity}
        if store.path(path).exists() and store.read(path)!=value:raise Problem("TASK_INPUT_CHANGED","重建任务绑定了其他请求")
        store.immutable_json(path,value)
        with Execution(store,new_task_id,digest(identity),progress=progress,pdf_limits=config.resources,
                       total_seconds=config.total_seconds,stage_seconds=config.stage_seconds) as context:
            context.emit("REBUILDING")
            result=_export_set(store,new_task_id,config,lesson,frozen,layouts,
                store.read(root/"template-profile.json"),store.path(root/"template.pdf"),
                {"operation":"REBUILD","model_call":"NOT_RUN","solver":"NOT_RUN",
                 "origin_task_id":task_id,"origin_manifest_sha256":sha256(store.path(root/"manifest.json")),
                 "original_generation_metrics":store.read(root/"metrics.json"),"_started_monotonic":time.perf_counter()})
            try:context.finish("COMPLETE",result)
            except OSError:pass
            return result


def private_acceptance(store,task_id):
    config,lesson,frozen=load_inputs(store)
    root,manifest,saved_lesson,saved_frozen,layouts=_read_result(store,task_id)
    if saved_lesson.version!=lesson.version or saved_frozen.version!=frozen.version or manifest["template_sha256"]!=sha256(store.path(config.template)):
        raise Problem("ACCEPTANCE_INPUT_MISMATCH","结果集不属于当前私人验收原件、课表或冻结版本")
    if config.expected_words is None:raise Problem("EXPECTED_COUNT_REQUIRED","私人验收需显式配置预期词数")
    if len(lesson.words)!=config.expected_words:raise Problem("ACCEPTANCE_WORD_COUNT","原件主词数与私人验收预期不符")
    check_transcription(store.path(config.source),lesson,store)
    result=verify_set(store,task_id)
    evidence=verify_evidence(store,lesson,frozen)
    result.update({"transcription":"VERIFIED","model_evidence":evidence,"manual_review":"NOT_PERFORMED"})
    store.json("checks/acceptance-"+uuid.uuid4().hex+".json",result)
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
            selected=select(subset,Ollama(),scoped)
            metrics=scoped.read(f"model/runs/{selected.selection_run_id}/evidence.json")
            measurements.append({"label":label,"sample_words":sample_words,
                "already_loaded":metrics["inspection"]["already_loaded"],"calls":metrics["calls"]})
        if sha256(store.path("clues/frozen.json"))!=original_hash:
            raise Problem("FROZEN_SELECTION_CHANGED","性能采样意外改变正式冻结版本")
        report={"status":"MEASURED","scope":"small identical protocol sample, not full-lesson latency",
                "measurements":measurements,"frozen_version_unchanged":frozen.version}
        store.json("benchmarks/model-timing.json",report)
    return report


def prepare_request(store, task_id):
    """Bind an ID before a future worker executes it. No arbitrary paths in the API."""
    with store.exclusive():
        task_id=identifier(task_id);config,lesson,frozen=load_inputs(store)
        identity=request_identity(config,lesson,frozen,sha256(store.path(config.template)))
        path=Path("tasks")/task_id/"identity.json"
        value={"schema_version":2,"identity":digest(identity),"request":identity}
        if store.path(path).exists() and store.read(path)!=value:raise Problem("TASK_INPUT_CHANGED","任务 ID 已绑定其他输入")
        store.immutable_json(path,value)
        return {"task_id":task_id,"request_sha256":digest(identity),"status":"PREPARED"}


def result_files(store, task_id):
    """Validated descriptors for a future preview/download adapter, never user paths."""
    verify_set(store,task_id)
    root,manifest,lesson,frozen,layouts=_read_result(store,task_id)
    return {"task_id":task_id,"status":"COMPLETE","files":[
        {"index":index,"name":entry["name"],"sha256":entry["sha256"],"pages":2,
         "bytes":store.path(root/entry["name"]).stat().st_size}
        for index,entry in enumerate(manifest["pdfs"],1)]}
