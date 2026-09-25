"""Explicit runtime roots; installed code locations never determine private data."""
from __future__ import annotations
import os
from pathlib import Path
from .domain import Problem


def roots():
    code = Path(os.environ.get('VOCABATRON_CODE_ROOT', str(Path.cwd()))).absolute()
    data = Path(os.environ.get('VOCABATRON_DATA_ROOT', str(code / '.private'))).absolute()
    runtime = Path(os.environ.get('VOCABATRON_RUNTIME_ROOT', str(data / 'runtime'))).absolute()
    legacy = Path(os.environ.get('VOCABATRON_LEGACY_ROOT', str(code / '.private'))).absolute()
    for path in (code, data, runtime, legacy):
        if path.resolve() != path:
            raise Problem('UNSAFE_PATH', 'Runtime roots must not contain symbolic links')
    return code, data, runtime, legacy


def private_path(path):
    path = Path(path).absolute()
    code, data, runtime, legacy = roots()
    allowed = (data, runtime, legacy, code / '.cache')
    if path.resolve() != path or not any(path.is_relative_to(root) for root in allowed):
        raise Problem('UNSAFE_PATH', 'Path is outside configured private roots')
    return path


def worker_environment():
    code, data, runtime, legacy = roots()
    result=dict(zip(('VOCABATRON_CODE_ROOT', 'VOCABATRON_DATA_ROOT',
                     'VOCABATRON_RUNTIME_ROOT', 'VOCABATRON_LEGACY_ROOT'),
                    map(str, (code, data, runtime, legacy))))
    if os.environ.get('VOCABATRON_OCR_ROOT'):
        result['VOCABATRON_OCR_ROOT']=str(private_path(os.environ['VOCABATRON_OCR_ROOT']))
    return result
