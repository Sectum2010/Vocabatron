"""Strict complete-set schema; required snapshot hashes cannot be elided."""
from typing import Literal
from pydantic import Field,model_validator
from .domain import Record,Problem

SNAPSHOTS=('lesson.json','clues.json','config.json','template-profile.json','template.pdf',
           'layout-1.json','layout-2.json','metrics.json','pdf-verification-1.json','pdf-verification-2.json')


class PdfEntry(Record):
    name:str
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    pages:Literal[2]


class Manifest(Record):
    schema_version:Literal[1,2]
    status:Literal['VERIFIED']
    task_id:str
    lesson_version:str
    source_sha256:str
    frozen_version:str
    template_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    generator_version:str
    dependencies:dict[str,str]
    structures:list[dict]
    pair:dict
    manual_review:str
    pdfs:tuple[PdfEntry,PdfEntry]
    generator_source_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    snapshot_hashes:dict[str,str]
    request_identity:dict|None=None
    request_sha256:str|None=None

    @model_validator(mode='after')
    def complete(self):
        import re
        from pathlib import Path
        from .storage import identifier
        from .domain import digest
        identifier(self.task_id)
        if set(self.snapshot_hashes)!=set(SNAPSHOTS):raise ValueError('required snapshots incomplete')
        if any(not re.fullmatch('[a-f0-9]{64}',v) for v in self.snapshot_hashes.values()):raise ValueError('invalid snapshot hash')
        if len({p.name for p in self.pdfs})!=2 or any(Path(p.name).name!=p.name or not p.name.endswith('.pdf') for p in self.pdfs):raise ValueError('invalid pdf names')
        if len(self.structures)!=2:raise ValueError('missing structures')
        if self.schema_version==2 and (not self.request_identity or digest(self.request_identity)!=self.request_sha256):raise ValueError('missing request identity')
        return self


def validate_manifest(value,task_id):
    from pydantic import ValidationError
    try:manifest=Manifest.model_validate(value)
    except (ValidationError,Problem):raise Problem('INCOMPLETE_SET','结果清单缺失必需字段或格式不合法') from None
    if manifest.task_id!=task_id:raise Problem('INCOMPLETE_SET','清单任务 ID 与目录不符')
    return manifest
