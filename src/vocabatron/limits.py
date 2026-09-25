"""Finite resource settings for document workers."""
from pydantic import BaseModel,ConfigDict,Field

class Limits(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True,allow_inf_nan=False)
    input_bytes: int=Field(default=64*1024*1024,ge=1024,le=256*1024*1024)
    pages: int=Field(default=32,ge=1,le=256)
    page_points: float=Field(default=1440,ge=72,le=4000)
    render_pixels: int=Field(default=30_000_000,ge=1024,le=100_000_000)
    characters: int=Field(default=500_000,ge=1,le=2_000_000)
    objects: int=Field(default=200_000,ge=10,le=1_000_000)
    ipc_bytes: int=Field(default=32*1024*1024,ge=1024,le=128*1024*1024)
    stream_bytes: int=Field(default=1024*1024,ge=1024,le=16*1024*1024)
    cpu_seconds: int=Field(default=120,ge=1,le=600)
    wall_seconds: float=Field(default=180,gt=0,le=900)
    address_bytes: int=Field(default=4*1024**3,ge=64*1024**2,le=16*1024**3)
    rss_bytes: int=Field(default=2560*1024**2,ge=64*1024**2,le=12*1024**3)
    fds: int=Field(default=64,ge=16,le=128)
    file_bytes: int=Field(default=128*1024**2,ge=1024,le=512*1024**2)
    temporary_bytes: int=Field(default=256*1024**2,ge=1024,le=1024**3)
