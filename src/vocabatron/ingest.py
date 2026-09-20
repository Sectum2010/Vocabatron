"""Text-layer table import, source character ownership and Poppler cross-check."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import subprocess

import pdfplumber

from .domain import Candidate, Fragment, Lesson, Problem, Word, check_input, digest
from .storage import PrivateStore, sha256


def whitespace(text):
    """Only presentation whitespace; punctuation and source text stay untouched."""
    return " ".join(text.split())


def split_candidates(fragment: Fragment, word_id: str):
    match = re.match(r"^(SYNONYMS?|ANTONYMS?)\s*:\s*(.*)$", fragment.raw,
                     re.IGNORECASE | re.DOTALL)
    if not match:
        raise Problem("CANDIDATE_BOUNDARY_REQUIRES_DECISION", "候选标签或边界无法确定")
    relation = "SYNONYM" if match[1].upper().startswith("SYN") else "ANTONYM"
    # A comma is a source delimiter. Spaces never split a multi-word phrase.
    chunks = match[2].split(",")
    if any(not x.strip() for x in chunks):
        raise Problem("CANDIDATE_BOUNDARY_REQUIRES_DECISION", "候选含空片段；未猜测修复")
    return tuple(Candidate(candidate_id=f"{word_id}-{relation.lower()}-{i+1}",
                           word_id=word_id, relation=relation, text=whitespace(raw),
                           source=fragment, segment=i)
                 for i, raw in enumerate(chunks))


def _fragment(page, bbox, text):
    indices = tuple(i for i, ch in enumerate(page.chars)
                    if bbox[0] <= (ch["x0"]+ch["x1"])/2 < bbox[2]
                    and bbox[1] <= (ch["top"]+ch["bottom"])/2 < bbox[3])
    return Fragment(page=page.page_number, bbox=tuple(bbox), raw=text, char_indices=indices)



def word_from_fields(fields, wid, ordinal, page_number):
    header = [(f, re.fullmatch(r"(.+?)\s*\(([^()]+)\)\s*(\[.*)?", whitespace(f.raw)))
              for f in fields]
    header = [(f, m) for f, m in header if m]
    if len(header) != 1:
        raise Problem("HEADER_REQUIRES_DECISION", "主词与词性无法唯一识别", details={"page":page_number,"ordinal":ordinal})
    header_field, match = header[0]
    candidate_fields = [f for f in fields if re.match(r"^(?:SYN|ANT)ONYM", f.raw, re.I)]
    candidates = tuple(c for f in candidate_fields for c in split_candidates(f, wid))
    forms = tuple(f for f in fields if re.match(r"^FORMS\s*:", f.raw, re.I))
    pronunciation_fields = tuple(f for f in fields if f != header_field and
                                 (f.raw.strip().startswith("[") or f.raw.strip().endswith("]")))
    tail = match[3]
    # Some source tables divide the opening bracket into the main-word cell.
    # Keep the complete original cell in fields and retain this exact substring.
    pronunciation = ((header_field.model_copy(update={"raw":tail,"char_indices":()}),)
                     if tail else ()) + pronunciation_fields
    remaining = [f for f in fields if f.raw.strip() and f != header_field
                 and f not in candidate_fields and f not in forms and f not in pronunciation_fields
                 and f.raw.strip() != ordinal]
    if len(remaining) != 2:
        raise Problem("FIELDS_REQUIRE_DECISION", "定义与例句单元格无法唯一识别", details={"page":page_number,"ordinal":ordinal,"count":len(remaining)})
    word = Word(word_id=wid, ordinal=ordinal, raw=match[1], letters=match[1].upper(),
                part_of_speech=match[2], pronunciation=pronunciation, forms=forms,
                definition=(remaining[0],), examples=(remaining[1],), fields=tuple(fields), candidates=candidates)
    return word

def extract_local(source: Path, expected_lesson: int, *, limits=None):
    from .pdf import _inspect_pdf
    from .transcription import ordered_evidence, poppler_pages
    _,security=_inspect_pdf(source)
    source_hash = sha256(source)
    try:
        import tempfile
        # Bounded file output inherits RLIMIT_FSIZE; never unbounded capture_output.
        with tempfile.NamedTemporaryFile(suffix=".xml",dir=source.parent) as file:
            subprocess.run(["/usr/bin/pdftotext", "-bbox-layout", str(source), file.name],
                           check=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,timeout=60)
            if Path(file.name).stat().st_size>(limits or {}).get("ipc_bytes",32*1024*1024):
                raise Problem("RESOURCE_LIMIT","第二提取器输出超限")
            poppler=Path(file.name).read_text(encoding="utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise Problem("SECOND_EXTRACTOR_UNAVAILABLE", "需要本机 Poppler 文本提取进行交叉核对") from None
    words, titles, page_reports, raw_pages = [], [], [], []
    with pdfplumber.open(source) as pdf:
        second_pages = poppler_pages(poppler)
        for page in pdf.pages:
            if limits and len(page.chars)>limits["characters"]:
                raise Problem("RESOURCE_LIMIT", "PDF 字符数量超限")
            if not page.chars:
                raise Problem("NO_TEXT_LAYER", "原件缺少可读取的文本层；未使用 OCR")
            tables = page.find_tables()
            if len(tables) != 1:
                raise Problem("TABLE_STRUCTURE_REQUIRES_DECISION", "页面表格结构无法唯一识别")
            table = tables[0]
            extracted = table.extract()
            starts = [i for i, row in enumerate(extracted) if row[0] and row[0].strip().isdigit()]
            if not starts:
                raise Problem("TABLE_STRUCTURE_REQUIRES_DECISION", "未识别原始序号")
            owned = Counter()
            redundant_cells = []
            record_fragments = []
            for si, start in enumerate(starts):
                end = starts[si+1] if si+1 < len(starts) else len(extracted)
                fields = []
                for ri in range(start, end):
                    for ci, raw in enumerate(extracted[ri]):
                        if raw is not None:
                            bbox = table.rows[ri].cells[ci]
                            frag = _fragment(page, bbox, raw)
                            if frag.char_indices and all(owned[i] for i in frag.char_indices):
                                # pdfplumber can infer a nested cell inside a spanning cell.
                                # These are the same source character objects, not repeated text.
                                duplicate_evidence=ordered_evidence(frag,page.chars,second_pages[page.page_number-1])
                                if not duplicate_evidence["table_order_match"] or not duplicate_evidence["poppler_order_match"]:
                                    raise Problem("TRANSCRIPTION_REQUIRES_DECISION","合并单元格的重复推断文本不一致")
                                redundant_cells.append({"row":ri,"column":ci,"bbox":bbox,
                                                        "char_indices":frag.char_indices})
                                continue
                            fields.append(frag)
                            owned.update(frag.char_indices)
                ordinal = extracted[start][0].strip()
                wid = f"w-{digest([source_hash,page.page_number,ordinal])[:16]}"
                word = word_from_fields(fields, wid, ordinal, page.page_number)
                words.append(word)
                record_fragments.extend(fields)
            outside = [i for i,ch in enumerate(page.chars) if not owned[i]]
            nonspace_outside = [i for i in outside if page.chars[i]["text"].strip()]
            # Header/footer text is preserved too, never silently discarded.
            for upper, lower in ((0, table.bbox[1]), (table.bbox[3], page.height)):
                chars = [i for i in outside if upper <= (page.chars[i]["top"]+page.chars[i]["bottom"])/2 < lower]
                if chars:
                    bbox = (0, upper, page.width, lower)
                    fragment = _fragment(page, bbox, page.crop(bbox).extract_text() or "")
                    titles.append(fragment)
                    owned.update(fragment.char_indices)
            missing = [i for i,ch in enumerate(page.chars) if ch["text"].strip() and owned[i] != 1]
            original = Counter(c for ch in page.chars for c in ch["text"] if not c.isspace())
            other = Counter(c for w in second_pages[page.page_number-1] for c in w["text"] if not c.isspace())
            evidence=[ordered_evidence(f,page.chars,second_pages[page.page_number-1])
                      for f in record_fragments+[t for t in titles if t.page==page.page_number]]
            cell_errors=[e["bbox"] for e in evidence if not e["table_order_match"] or not e["poppler_order_match"]]
            report = {"page":page.page_number,"characters":len(page.chars),"nonspace":sum(original.values()),
                      "unique_character_ownership":not missing,"cell_character_checks":len(record_fragments),
                      "ordered_fields":evidence,"cell_errors":cell_errors,"poppler_character_match":original == other,
                      "table_bbox":table.bbox,"record_count":len(starts),
                      "redundant_inferred_cells":redundant_cells,
                      "unassigned_or_duplicate_indices":missing}
            page_reports.append(report)
            raw_pages.append({"page":page.page_number,"chars":page.chars,"table_cells":table.cells,
                              "table_extract":extracted,"reading_order":page.extract_words()})
    ordinals = [int(w.ordinal) for w in words]
    title_numbers = {int(n) for f in titles for n in re.findall(r"\bLesson\s*(?:#\s*)?(\d+)\b", f.raw, re.I)}
    report = {"schema_version":1,"source_sha256":source_hash,"pages":page_reports,
              "original_ordinals":ordinals,"ordinal_sequence_valid":ordinals == list(range(1,len(words)+1)),
              "title_lesson_numbers":sorted(title_numbers),"word_count":len(words),
              "validator_version":"ordered-spatial-v2",
              "method":"ordered per-field spatial characters + table strings + Poppler bbox-layout",
              "manual_review":"NOT_PERFORMED"}
    passed = all(p["unique_character_ownership"] and p["poppler_character_match"] and not p["cell_errors"] for p in page_reports)
    report["status"] = "VERIFIED" if passed and report["ordinal_sequence_valid"] else "REQUIRES_REVIEW"
    if report["status"] != "VERIFIED":
        raise Problem("TRANSCRIPTION_REQUIRES_DECISION", "字符覆盖或独立提取不一致；详见私人覆盖报告")
    if title_numbers != {expected_lesson}:
        raise Problem("LESSON_REQUIRES_DECISION", "标题课号与显式课号不一致或无法唯一读取")
    lesson = Lesson(schema_version=2,lesson=expected_lesson,source_sha256=source_hash,title=tuple(titles),words=tuple(words),coverage_sha256=digest(report))
    check_input(lesson)
    md = ["# 原始文本转录", "", f"原件 SHA-256：{source_hash}", "", "仅整理表格与排版空白；下列单元格保留原字符串。", ""]
    for f in titles:
        md.extend([f"## 页 {f.page}：表格外文本",f.raw,""])
    for w in words:
        md.extend([f"## 原序号 {w.ordinal}", f"稳定 ID：{w.word_id}", ""])
        for f in w.fields:
            md.extend([f"页 {f.page}；位置 {f.bbox}；字符索引数 {len(f.char_indices)}", "```text",f.raw,"```",""])
    return {"lesson":lesson.model_dump(mode="json"),"coverage":report,"raw_pages":raw_pages,
            "poppler":poppler,"transcript":"\n".join(md)+"\n","security":security}


def extract(source, expected_lesson):
    from .supervisor import run_job
    return run_job("extract",{"source":str(source),"lesson":expected_lesson})


def write_bundle(store, root, bundle):
    root=Path(root)
    for key,name in (("lesson","lesson.json"),("coverage","coverage.json"),("raw_pages","raw-pages.json"),("security","input-security.json")):
        store.json(root/name,bundle[key])
    store.write(root/"poppler.txt",bundle["poppler"].encode())
    store.write(root/"transcript.md",bundle["transcript"].encode())


def ingest(source: Path, expected_lesson: int, store: PrivateStore):
    """Explicit source import: complete immutable version, then one atomic pointer.

    Application imports additionally snapshot their configuration under the same
    store lock through services.import_lesson. This lower-level entry has the same
    publication boundary and never writes the legacy ingest/ files.
    """
    import uuid
    from .execution import checkpoint
    with store.exclusive():
        bundle=extract(source,expected_lesson)
        version=Path("imports")/uuid.uuid4().hex
        write_bundle(store,version,bundle)
        with store.commit_lock():
            checkpoint(force_cancel=True)
            store.json("current.json",{"schema_version":2,"lesson_path":str(version/"lesson.json"),"frozen_path":None})
        return Lesson.model_validate(bundle["lesson"]),bundle["coverage"]


def check_transcription(source: Path, lesson: Lesson, store: PrivateStore):
    from .transcription import same_content
    from .execution import checkpoint
    import uuid
    root=Path("checks")/uuid.uuid4().hex
    try:
        bundle=extract(source,lesson.lesson)
        checkpoint()
        write_bundle(store,root,bundle)
        same_content(lesson,Lesson.model_validate(bundle["lesson"]))
        return bundle["coverage"]
    except Problem as exc:
        store.json(root/"failure.json",{"status":exc.code})
        raise
