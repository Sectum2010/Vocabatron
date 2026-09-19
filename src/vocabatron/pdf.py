"""Calibrated vector-template overlays and checks on the actual exported PDF."""
from __future__ import annotations

from collections import Counter
import io
import math
from pathlib import Path

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter, Transformation
from reportlab import rl_config
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
import reportlab

from .domain import Problem, selected_candidates
from .storage import identifier, sha256
from .validation import clue_rows, validate_clue_rows, validate_layout

FONT="VocabatronVera"
MIN_CLUE_SIZE=11.0


def font():
    if FONT not in pdfmetrics.getRegisteredFontNames():
        path=Path(reportlab.__file__).parent/"fonts"/"Vera.ttf"
        if not path.is_file():raise Problem("FONT_UNAVAILABLE","缺少可合法嵌入的字体")
        pdfmetrics.registerFont(TTFont(FONT,str(path)))
    return pdfmetrics.getFont(FONT)


def check_glyphs(text):
    face=font().face
    if any(ord(c) not in face.charToGlyph for c in text):
        raise Problem("MISSING_GLYPH","当前嵌入字体不覆盖全部原文字符；未替换原文")


def _inspect_pdf(path):
    reader=PdfReader(path)
    if reader.is_encrypted:raise Problem("ENCRYPTED_PDF","原件加密，不能可靠读取")
    seen=set();findings=[]
    def walk(obj):
        if hasattr(obj,"idnum"):
            key=(obj.idnum,obj.generation)
            if key in seen:return
            seen.add(key);obj=obj.get_object()
        if isinstance(obj,dict):
            for key,value in obj.items():
                if key in {"/JavaScript","/JS","/OpenAction","/AA","/EmbeddedFiles","/RichMedia","/Launch","/OCProperties"}:
                    findings.append(str(key))
                if key not in {"/Parent","/P"}:walk(value)
        elif isinstance(obj,list):
            for value in obj:walk(value)
    walk(reader.trailer["/Root"])
    if any(page.get("/Annots") for page in reader.pages):
        raise Problem("PDF_ANNOTATIONS_REQUIRE_DECISION","原件含注释或交互对象，需确认其可见内容")
    if "/OCProperties" in findings:
        raise Problem("PDF_LAYERS_REQUIRE_DECISION","原件含可选内容层，需确认可见内容")
    return reader,{"active_or_attached_keys":sorted(set(findings)),"execution":"NEVER",
                   "policy":"new catalog, selected pages only, no attachments or document actions"}


def _calibrate(path: Path):
    reader,security=_inspect_pdf(path)
    if len(reader.pages)<2:raise Problem("TEMPLATE_PAGE_COUNT","模板至少需要两页")
    with pdfplumber.open(path) as doc:
        first,second=doc.pages[:2]
        grids=[t for t in first.find_tables() if len(t.rows)==20 and len(t.columns)==20]
        if len(grids)!=1:raise Problem("TEMPLATE_GRID","无法从原向量线唯一定位 20×20 网格")
        grid=grids[0]
        if any(cell is None for row in grid.rows for cell in row.cells):
            raise Problem("TEMPLATE_GRID","网格单元格结构不完整")
        columns=[]
        for table in second.find_tables():
            values=table.extract()
            if len(table.columns)==2 and len(table.rows)>=2 and all(word in " ".join(v or "" for v in values[0]).upper() for word in ("ACROSS","DOWN")):
                columns=[table.rows[1].cells[0],table.rows[1].cells[1]]
        if len(columns)!=2:raise Problem("TEMPLATE_COLUMNS","无法定位原 ACROSS / DOWN 内容单元格")
        words=first.extract_words()
        labels=[w for w in words if w["text"].lower()=="lesson"]
        if len(labels)!=1:raise Problem("TEMPLATE_LESSON","无法唯一定位原课号位置")
        label=labels[0]
        blanks=[w for w in words if set(w["text"])=={"_"} and w["x0"]>label["x1"] and abs(w["top"]-label["top"])<2]
        if len(blanks)!=1:raise Problem("TEMPLATE_LESSON","无法唯一定位课号空位")
        blank=blanks[0]
        pages=[]
        for i,p in enumerate(doc.pages[:2]):
            original=reader.pages[i]
            # pdfplumber display coordinates are normalized to the rotated MediaBox.
            box=list(map(float,original.mediabox))
            if box[0]!=0 or box[1]!=0:
                raise Problem("TEMPLATE_ORIGIN_REQUIRES_DECISION","当前校准器要求原模板 MediaBox 原点为零")
            pages.append({"width":p.width,"height":p.height,"mediabox":box,
                          "cropbox":list(map(float,original.cropbox)),"rotation":original.rotation})
        return {"schema_version":1,"template_sha256":sha256(path),"pages":pages,"input_pages":len(reader.pages),
                "grid":[row.cells for row in grid.rows],"columns":columns,
                "lesson_blank":[blank["x0"],blank["top"],blank["x1"],blank["bottom"]],
                "security":security,"font":"ReportLab bundled Bitstream Vera Sans"}


def wrap(text,width,size):
    check_glyphs(text)
    lines=[];current=""
    for token in text.split(" "):
        if pdfmetrics.stringWidth(token,FONT,size)>width:
            raise Problem("CLUE_TOO_WIDE","原候选包含无法在可读字号下容纳的片段")
        proposed=current+" "+token if current else token
        if current and pdfmetrics.stringWidth(proposed,FONT,size)>width:
            lines.append(current);current=token
        else:current=proposed
    if current:lines.append(current)
    return lines


def clue_text(number,relation,text):
    return f"{number}. ({'syn.' if relation=='SYNONYM' else 'ant.'}) {text}"


def clue_capacity(lesson,frozen,profile):
    """Necessary lower bounds only; exact numbering/line breaking is checked after solving.

    Every clue needs at least one line. The longest token gives a safe directional
    impossibility test. Never use a pessimistic maximum-number estimate as a proof.
    """
    choices=selected_candidates(lesson,frozen);costs={}
    capacities=[math.floor((box[3]-box[1]-24)*100) for box in profile["columns"]]
    for word in lesson.words:
        c=choices[word.word_id];values=[]
        for cap,box in zip(capacities,profile["columns"]):
            try:wrap(clue_text(1,c.relation,c.text),box[2]-box[0]-24,MIN_CLUE_SIZE)
            except Problem as exc:
                if exc.code!="CLUE_TOO_WIDE":raise
                values.append(cap+1)
            else:values.append(math.floor((MIN_CLUE_SIZE*1.35+5)*100))
        costs[word.word_id]=values
    return costs,capacities


def plan(lesson,layout,frozen,profile):
    font()
    checked=validate_layout(lesson,layout);rows=clue_rows(lesson,layout,frozen)
    validate_clue_rows(lesson,layout,frozen,rows)
    items=[]
    def text_item(page,text,x,top,size,kind,**extra):
        check_glyphs(text)
        ascent,descent=pdfmetrics.getAscentDescent(FONT,size)
        width=pdfmetrics.stringWidth(text,FONT,size)
        item={"page":page,"text":text,"x":x,"top":top,"size":size,"kind":kind,
              "bbox":[x,top,x+width,top+ascent-descent],**extra}
        items.append(item);return item
    for (r,c),cell in sorted(checked["cells"].items()):
        x0,y0,x1,y1=profile["grid"][r][c];w=x1-x0;h=y1-y0
        size=min(13,h*.45,w*.46)
        width=pdfmetrics.stringWidth(cell["letter"],FONT,size)
        text_item(0,cell["letter"],x0+(w-width)/2,y0+(h-size)/2+h*.045,size,"letter",row=r,col=c)
    for (r,c),number in checked["starts"].items():
        x0,y0,x1,y1=profile["grid"][r][c]
        text_item(0,str(number),x0+1.6,y0+1.3,min(6.2,(y1-y0)*.24),"number",row=r,col=c)
    x0,y0,x1,y1=profile["lesson_blank"]
    size=min(11,y1-y0)
    width=pdfmetrics.stringWidth(str(lesson.lesson),FONT,size)
    if width>x1-x0-2:raise Problem("LESSON_OVERFLOW","课号超出原模板空位")
    text_item(0,str(lesson.lesson),(x0+x1-width)/2,y0-1,size,"lesson")
    for direction,box in zip(("across","down"),profile["columns"]):
        entries=[r for r in rows if r["direction"]==direction]
        for size in (12.0,11.5,MIN_CLUE_SIZE):
            try:
                wrapped=[wrap(clue_text(r["number"],r["relation"],r["text"]),box[2]-box[0]-24,size) for r in entries]
            except Problem as exc:
                if exc.code!="CLUE_TOO_WIDE":raise
                continue
            height=sum(len(lines)*size*1.35+5 for lines in wrapped)
            if height<=box[3]-box[1]-24:break
        else:raise Problem("CLUE_OVERFLOW","两栏不能在可读字号下容纳全部冻结线索")
        top=box[1]+12
        for row,lines in zip(entries,wrapped):
            for line in lines:
                text_item(1,line,box[0]+12,top,size,"clue",word_id=row["word_id"],direction=direction)
                top+=size*1.35
            top+=5
    # Reject overlap among additions, including start numbers and letters.
    for i,a in enumerate(items):
        ax,ay,bx,by=a["bbox"]
        if ax<0 or ay<0 or bx>profile["pages"][a["page"]]["width"] or by>profile["pages"][a["page"]]["height"]:
            raise Problem("TEXT_OUT_OF_BOUNDS","绘制文字超出原页面")
        for b in items[i+1:]:
            if a["page"]!=b["page"]:continue
            cx,cy,dx,dy=b["bbox"]
            if min(bx,dx)>max(ax,cx)+.01 and min(by,dy)>max(ay,cy)+.01:
                raise Problem("TEXT_OVERLAP","字母、题号或线索的实际字形度量发生重叠")
    return items,rows


def _export_pdf(template,output,lesson,layout,frozen,profile):
    if sha256(template)!=profile["template_sha256"]:raise Problem("TEMPLATE_CHANGED","模板指纹与坐标配置不符")
    reader,_=_inspect_pdf(template)
    items,rows=plan(lesson,layout,frozen,profile)
    writer=PdfWriter()
    for index,pmeta in enumerate(profile["pages"]):
        buffer=io.BytesIO();c=canvas.Canvas(buffer,pagesize=(pmeta["width"],pmeta["height"]),invariant=1,pageCompression=1)
        c.setTitle("Crossword");c.setAuthor("");c.setCreator("Vocabatron")
        for item in items:
            if item["page"]!=index:continue
            ascent,_=pdfmetrics.getAscentDescent(FONT,item["size"])
            c.setFont(FONT,item["size"])
            c.drawString(item["x"],pmeta["height"]-item["top"]-ascent,item["text"])
        c.showPage();c.save();buffer.seek(0)
        base=writer.add_page(reader.pages[index])
        for key in ("/Metadata","/PieceInfo","/AA","/StructParents"):
            if key in base:del base[key]
        x0,y0,x1,y1=pmeta["mediabox"];rotation=pmeta["rotation"]%360
        matrices={0:(1,0,0,1,x0,y0),90:(0,1,-1,0,x1,y0),180:(-1,0,0,-1,x1,y1),270:(0,-1,1,0,x0,y1)}
        if rotation not in matrices:raise Problem("PDF_ROTATION","页面旋转角不合法")
        base.merge_transformed_page(PdfReader(buffer).pages[0],Transformation(matrices[rotation]),expand=False)
    writer.add_metadata({"/Title":"Crossword","/Author":"","/Creator":"Vocabatron","/Producer":"Vocabatron"})
    output=Path(output)
    if output.exists() or output.resolve()==Path(template).resolve():raise Problem("OUTPUT_EXISTS","不覆盖现有 PDF 或原件")
    output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with output.open("xb") as stream:writer.write(stream)
    output.chmod(0o600)
    return items,rows



def inspect_pdf(path):
    from .supervisor import run_job
    return run_job("inspect",{"source":str(path)})


def calibrate(path: Path):
    from .supervisor import run_job
    return run_job("calibrate",{"template":str(path)})


def export_pdf(template,output,lesson,layout,frozen,profile):
    from .supervisor import run_job
    result=run_job("export",{"template":str(template),"output":str(output),
        "lesson":lesson.model_dump(mode="json"),"layout":layout.model_dump(mode="json"),
        "frozen":frozen.model_dump(mode="json"),"profile":profile})
    return result["items"],result["rows"]


def verify_pdf(template,output,lesson,layout,frozen,profile,*,expected_name,preview_dir=None):
    from .supervisor import run_job
    return run_job("verify",{"template":str(template),"output":str(output),
        "lesson":lesson.model_dump(mode="json"),"layout":layout.model_dump(mode="json"),
        "frozen":frozen.model_dump(mode="json"),"profile":profile,"expected_name":expected_name,
        "preview_dir":str(preview_dir) if preview_dir else None})
