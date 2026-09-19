"""Versioned records; raw source strings are never normalized in place."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from .limits import Limits


class Problem(Exception):
    def __init__(self, code: str, message: str, *, details=None):
        self.code, self.message, self.details = code, message, details
        super().__init__(f"{code}: {message}")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def digest(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


class Fragment(Record):
    page: int
    bbox: tuple[float, float, float, float]
    raw: str
    char_indices: tuple[int, ...] = ()


class Candidate(Record):
    candidate_id: str
    word_id: str
    relation: Literal["SYNONYM", "ANTONYM"]
    text: str
    source: Fragment
    segment: int


class Word(Record):
    word_id: str
    ordinal: str
    raw: str
    letters: str
    part_of_speech: str
    pronunciation: tuple[Fragment, ...] = ()
    forms: tuple[Fragment, ...] = ()
    definition: tuple[Fragment, ...] = ()
    examples: tuple[Fragment, ...] = ()
    fields: tuple[Fragment, ...] = ()
    candidates: tuple[Candidate, ...]


class Lesson(Record):
    schema_version: Literal[1, 2] = 1
    lesson: int = Field(gt=0, le=9999)
    source_sha256: str
    title: tuple[Fragment, ...]
    words: tuple[Word, ...]
    coverage_sha256: str

    @property
    def version(self):
        return digest(self) if self.schema_version == 1 else self.content_version

    @property
    def content_version(self):
        data=self.model_dump(mode="json")
        data.pop("coverage_sha256");data.pop("schema_version")
        return digest(data)


class Choice(Record):
    word_id: str
    candidate_id: str


class Selection(Record):
    choices: tuple[Choice, ...]


class FrozenClues(Record):
    schema_version: Literal[1, 2] = 1
    lesson_version: str
    source_sha256: str
    choices: tuple[Choice, ...]
    model: str
    model_digest: str
    ollama_version: str
    prompt_version: str
    parameters: dict
    selection_run_id: str | None = None
    evidence_sha256: str | None = None

    @property
    def version(self):
        data=self.model_dump(mode="json")
        if self.schema_version == 1:
            data.pop("selection_run_id");data.pop("evidence_sha256")
        return digest(data)


class Placement(Record):
    word_id: str
    row: int
    col: int
    direction: Literal["across", "down"]


class Layout(Record):
    schema_version: Literal[1] = 1
    lesson_version: str
    placements: tuple[Placement, ...]
    size: int = 20


class SolverOptions(Record):
    workers: int = Field(default=4, ge=1, le=8)
    seed: int = Field(default=37,ge=0,le=2147483647)
    seconds_per_layout: float = Field(default=240, gt=0, le=900)
    optimization_seconds: float = Field(default=2, ge=0, le=30)


class PrivateConfig(Record):
    schema_version: Literal[1] = 1
    lesson: int = Field(gt=0, le=9999)
    source: str
    template: str
    prefixes: tuple[str, str]
    expected_words: int | None = None
    solver: SolverOptions = SolverOptions()
    resources: Limits = Limits()
    total_seconds: float = Field(default=1800, gt=0, le=7200)
    stage_seconds: float = Field(default=600, gt=0, le=1800)


def check_input(lesson: Lesson, size: int = 20):
    if not lesson.words:
        raise Problem("EMPTY_INPUT", "课表没有主词条")
    seen, ids = set(), set()
    for w in lesson.words:
        if w.word_id in ids or w.letters in seen:
            raise Problem("DUPLICATE_WORD", "存在重复主词或永久 ID")
        ids.add(w.word_id)
        seen.add(w.letters)
        if not re.fullmatch(r"[A-Za-z]+", w.raw) or w.letters != w.raw.upper():
            raise Problem("ANSWER_CHARACTERS_REQUIRE_DECISION", "答案包含需另行决定的字符；未删除或替换")
        if len(w.letters) == 1:
            raise Problem("SINGLE_LETTER_REQUIRES_DECISION", "单字主词规则尚未定义")
        if len(w.letters) > size:
            raise Problem("WORD_TOO_LONG", "主词长度超过固定网格边长")
        if not w.candidates:
            raise Problem("NO_CANDIDATE", "主词缺少原文线索候选")
        cids = set()
        for c in w.candidates:
            if c.word_id != w.word_id or c.candidate_id in cids or not c.text.strip():
                raise Problem("INVALID_CANDIDATE", "候选来源或标识不合法")
            cids.add(c.candidate_id)


def selected_candidates(lesson: Lesson, frozen: FrozenClues):
    if frozen.lesson_version != lesson.version or frozen.source_sha256 != lesson.source_sha256:
        raise Problem("STALE_SELECTION", "冻结线索不属于当前课表版本")
    return check_choices(lesson, frozen.choices)


def check_choices(lesson: Lesson, choices):
    words = {w.word_id: w for w in lesson.words}
    result = {}
    for choice in choices:
        if choice.word_id not in words or choice.word_id in result:
            raise Problem("MODEL_WORD_COVERAGE", "模型返回额外或重复的词条 ID")
        candidates = {c.candidate_id: c for c in words[choice.word_id].candidates}
        if choice.candidate_id not in candidates:
            raise Problem("MODEL_CANDIDATE_MISMATCH", "候选不属于对应词条")
        result[choice.word_id] = candidates[choice.candidate_id]
    if set(result) != set(words):
        raise Problem("MODEL_WORD_COVERAGE", "模型未恰好覆盖全部词条")
    return result
