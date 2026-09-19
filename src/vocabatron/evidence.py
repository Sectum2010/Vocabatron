"""Read only the responses explicitly adopted by one immutable selection run."""
import json
from pathlib import Path
from .domain import Problem, Selection, Lesson, digest, check_choices
from .storage import identifier,sha256
from .clues import MODEL,PROMPT


def verify_evidence(store,lesson,frozen):
    if frozen.schema_version==1:return legacy_evidence(store,lesson,frozen)
    if not frozen.selection_run_id or not frozen.evidence_sha256:raise Problem('MODEL_EVIDENCE_MISSING','缺少运行证据引用')
    root=Path('model/runs')/identifier(frozen.selection_run_id)
    try:
        if sha256(store.path(root/'evidence.json'))!=frozen.evidence_sha256:raise Problem('MODEL_EVIDENCE_CHANGED','模型证据哈希不符')
        evidence=store.read(root/'evidence.json')
        if (evidence['selection_run_id']!=frozen.selection_run_id or evidence['lesson_version']!=lesson.version or
            evidence['source_sha256']!=lesson.source_sha256 or evidence['lesson_content_version']!=lesson.content_version or
            evidence['inspection']['digest']!=frozen.model_digest or evidence['inspection']['version']!=frozen.ollama_version or
            evidence['prompt_sha256']!=frozen.prompt_version):raise Problem('MODEL_EVIDENCE_MISMATCH','模型证据跨版本或跨课表')
        for key,name in (('inspection_sha256','inspection.json'),('lesson_sha256','lesson.json')):
            if sha256(store.path(root/name))!=evidence[key]:raise Problem('MODEL_EVIDENCE_CHANGED','运行输入证据变化')
        saved=Lesson.model_validate(store.read(root/'lesson.json'))
        if saved.version!=lesson.version:raise Problem('MODEL_EVIDENCE_MISMATCH','证据课表不符')
        adopted=evidence['adopted_responses']
        if not adopted or len(set(adopted))!=len(adopted):raise Problem('MODEL_EVIDENCE_MISMATCH','采用的响应列表不完整')
        records={r['response']:r for r in evidence['attempts']}
        if len(records)!=len(evidence['attempts']):raise Problem('MODEL_EVIDENCE_MISMATCH','响应记录重复')
        choices=[]
        for name in adopted:
            record=records[name]
            for key in ('request','response'):
                filename=record[key]
                if Path(filename).name!=filename:raise Problem('UNSAFE_PATH','证据文件名不合法')
                if sha256(store.path(root/filename))!=record[key+'_sha256']:raise Problem('MODEL_EVIDENCE_CHANGED','请求或响应证据变化')
            request=store.read(root/record['request']);response=store.read(root/name)
            if request['model']!=MODEL or response.get('model')!=frozen.model or response.get('done') is not True or response.get('done_reason')=='length':
                raise Problem('MODEL_EVIDENCE_MISMATCH','实际模型名称或结束状态不符')
            if digest(request['format'])!=record['schema_sha256'] or digest(request['messages'][0]['content'])!=frozen.prompt_version:
                raise Problem('MODEL_EVIDENCE_MISMATCH','实际 schema 或提示词不符')
            subset=lesson.model_copy(update={'words':tuple(w for w in lesson.words if w.word_id in record['word_ids'])})
            parsed=Selection.model_validate_json(response['message']['content']);check_choices(subset,parsed.choices)
            choices.extend(parsed.choices)
        check_choices(lesson,choices)
        if tuple(choices)!=frozen.choices or evidence['choices']!=[c.model_dump(mode='json') for c in choices]:
            raise Problem('MODEL_EVIDENCE_MISMATCH','冻结选择与采用响应不符')
        return {'status':'BOUND_TO_IMMUTABLE_RUN','selection_run_id':frozen.selection_run_id,'adopted_responses':adopted,'calls':evidence['calls']}
    except (KeyError,FileNotFoundError,ValueError) as exc:
        raise Problem('MODEL_EVIDENCE_MISSING','模型证据缺失或格式错误') from None


def legacy_evidence(store,lesson,frozen):
    """No latest/max-attempt guessing. Exactly one complete response must match."""
    folder=store.path(Path('model')/lesson.version);matches=[]
    for path in sorted(folder.glob('batch-*-attempt-*.json')) if folder.is_dir() else []:
        try:
            response=json.loads(path.read_text())
            if response.get('model')!=frozen.model or response.get('done') is not True or response.get('done_reason')=='length':continue
            choices=Selection.model_validate_json(response['message']['content']).choices
            check_choices(lesson,choices)
            if choices==frozen.choices:matches.append({'path':str(path.relative_to(store.root)),'sha256':sha256(path)})
        except (KeyError,ValueError,Problem):continue
    return {'status':'LEGACY_UNIQUE_RESPONSE_MATCH' if len(matches)==1 else 'LEGACY_AMBIGUOUS_OR_MISSING',
            'full_provenance':'NOT_AVAILABLE','matches':matches,
            'structural_validity':'INDEPENDENT_OF_MODEL_EVIDENCE'}
