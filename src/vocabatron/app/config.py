"""Private deployment configuration. Never supplied by an HTTP client."""
from __future__ import annotations
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from ..limits import Limits


class ResourcePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    cpu_threads: int = Field(default=2, ge=1, le=4)
    search_slots: int = Field(default=1, ge=1, le=2)
    threads_per_search: int = Field(default=2, ge=1, le=2)
    document_slots: int = Field(default=1, ge=1, le=2)
    memory_max_bytes: int = Field(default=8*1024**3, ge=2*1024**3, le=12*1024**3)
    host_memory_reserve_bytes: int = Field(default=24*1024**3, ge=16*1024**3)
    disk_reserve_bytes: int = Field(default=20*1024**3, ge=10*1024**3)
    external_cpu_start_percent: float = Field(default=15, gt=0, le=50)
    external_cpu_stop_percent: float = Field(default=25, gt=0, le=65)
    cpu_work_during_gpu_activity: bool = False
    cpu_psi_percent: float = Field(default=5, gt=0, le=10)
    cpu_pressure_min_external_percent: float = Field(default=0,ge=0,le=30)
    memory_psi_percent: float = Field(default=0.5, gt=0, le=1)
    io_psi_percent: float = Field(default=2, gt=0, le=5)
    idle_window_seconds: float = Field(default=20, ge=5, le=120)
    stale_seconds: float = Field(default=6, ge=3, le=15)
    sample_seconds: float = Field(default=1, ge=0.5, le=2)
    search_seconds: float = Field(default=90, ge=5, le=240)
    inference_timeout_seconds: float = Field(default=480, ge=30, le=600)
    gpu_idle_percent: float = Field(default=2, ge=0, le=3)
    gpu_idle_window_seconds: float = Field(default=30, ge=15, le=120)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: int = 1
    code_root: Path
    data_root: Path
    runtime_root: Path
    database: Path
    outputs_root: Path
    static_root: Path
    legacy_root: Path
    template: Path
    public_base_url: str
    listen_host: str = '127.0.0.1'
    listen_port: int = Field(default=8766, ge=1024, le=65535)
    allowed_logins: tuple[str, ...] = ()
    csrf_secret: SecretStr = Field(min_length=32)
    resources: ResourcePolicy = ResourcePolicy()
    document_limits: Limits = Limits(pages=256, characters=2_000_000)
    upload_max_files: int = Field(default=32, ge=1, le=100)
    upload_max_bytes: int = Field(default=64*1024**2, ge=1024, le=256*1024**2)
    owner_id: str = 'owner'
    require_legacy_migration: bool = False

    @field_validator('public_base_url')
    @classmethod
    def base(cls, value):
        value = value.rstrip('/') + '/'
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('A fixed private HTTPS base URL is required')
        if '..' in parsed.path or '//' in parsed.path or '%' in parsed.path:
            raise ValueError('Invalid application base path')
        return value

    @field_validator('allowed_logins')
    @classmethod
    def logins(cls, values):
        if any(not v or len(v)>254 or v.strip()!=v or any(ord(c)<33 for c in v) for v in values):
            raise ValueError('Invalid authorized login')
        return tuple(sorted(set(v.casefold() for v in values)))

    @model_validator(mode='after')
    def boundaries(self):
        for key in ('code_root','data_root','runtime_root','database','outputs_root','static_root','legacy_root','template'):
            path = getattr(self, key)
            if not path.is_absolute() or path.resolve() != path:
                raise ValueError(f'{key} must be an absolute path without symlinks')
        if self.listen_host != '127.0.0.1' or self.owner_id != 'owner':
            raise ValueError('Only the loopback, single-owner deployment is supported')
        if not self.database.is_relative_to(self.data_root) or self.outputs_root == self.data_root:
            raise ValueError('Invalid data/exports separation')
        if self.outputs_root.is_relative_to(self.data_root) or self.data_root.is_relative_to(self.outputs_root):
            raise ValueError('Exports and private data must be separate')
        if self.code_root in (self.data_root,self.runtime_root,self.outputs_root):
            raise ValueError('Code must not be a writable data root')
        return self

    @property
    def base_path(self):
        return urlsplit(self.public_base_url).path

    @property
    def origin(self):
        p = urlsplit(self.public_base_url)
        return f'{p.scheme}://{p.netloc}'

    def activate(self):
        os.environ.update({
            'VOCABATRON_CODE_ROOT': str(self.code_root), 'VOCABATRON_DATA_ROOT': str(self.data_root),
            'VOCABATRON_RUNTIME_ROOT': str(self.runtime_root), 'VOCABATRON_LEGACY_ROOT': str(self.legacy_root),
            'VOCABATRON_THREAD_BUDGET': str(self.resources.cpu_threads),
        })


def load_config(path=None):
    value = path or os.environ.get('VOCABATRON_CONFIG')
    if not value:
        raise ValueError('Set VOCABATRON_CONFIG to the private deployment configuration')
    path = Path(value).absolute()
    if path.resolve()!=path or path.stat().st_size>128*1024:
        raise ValueError('Invalid private configuration file')
    config = AppConfig.model_validate_json(path.read_bytes())
    config.activate()
    return config
