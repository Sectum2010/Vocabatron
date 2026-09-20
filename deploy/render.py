"""Render reviewable units into the configured private data root; no system writes."""
import argparse
import grp
import os
from pathlib import Path
import pwd
import re
from vocabatron.app.config import load_config
from vocabatron.storage import private_mkdir

parser=argparse.ArgumentParser();parser.add_argument('config',type=Path);parser.add_argument('--block-device',required=True)
args=parser.parse_args();config=load_config(args.config)
if not re.fullmatch(r'/dev/[A-Za-z0-9_/-]+',args.block_device):parser.error('An explicit local block device is required')
values={'CODE_ROOT':str(config.code_root),'CONFIG':str(args.config.absolute()),'PYTHON':str(config.code_root/'.venv/bin/python'),
    'DATA_ROOT':str(config.data_root),'RUNTIME_ROOT':str(config.runtime_root),'OUTPUTS_ROOT':str(config.outputs_root),
    'USER':pwd.getpwuid(os.getuid()).pw_name,'GROUP':grp.getgrgid(os.getgid()).gr_name,'UID':str(os.getuid()),'BLOCK_DEVICE':args.block_device}
if any(any(c in v for c in '\n\r"%') or any(c.isspace() for c in v) for v in values.values()):
    parser.error('This renderer requires paths without whitespace, quotes or systemd specifiers')
destination=config.data_root/'deployment';private_mkdir(destination)
for source in Path(__file__).parent.glob('*.in'):
    content=source.read_text()
    for key,value in values.items():content=content.replace('@'+key+'@',value)
    target=destination/source.name.removesuffix('.in');target.write_text(content);target.chmod(0o600)
print(destination)
