# Local document recovery

The Web importer uses `positioned-document-v1` evidence and the
`vocabulary-anchors-v2` parser. Backend extraction and vocabulary interpretation
are separate. Native pdfplumber characters and Poppler positioned text are the
first path. Tables are optional geometry hints: out-of-page wrappers and
duplicate geometry cannot consume the valid table's characters.

Open entries continue across pages until the next entry or lesson heading.
Explicit candidate fields preserve their original punctuation and source
fragments. Commas, semicolons and list line breaks delimit candidates; spaces
inside phrases do not. Missing noncritical pronunciation, forms or examples do
not invalidate an otherwise complete entry. Every token has a semantic,
metadata or unresolved owner.

Recovery is bounded: positioned text without table hints, Tesseract 300 DPI,
orientation and deskew, 360 DPI threshold/alternate segmentation, then Docling
Heron layout with RapidOCR PP-OCRv4 on CPU. The PDFium renderer enforces pixel
bounds before OCR. Critical OCR strings need agreement between independent
observations; the accepted string must be one of those observations. Native
letters remain authoritative in mixed documents. Upright boxes, inverse source
polygons, confidence and alternative observations stay in private evidence.
Disagreements fail closed after this ladder. Missing local models or exhausted
document resource limits are reported as unavailable recovery, not silently
accepted vocabulary.

The second backend uses CPU-only Torch and ONNX Runtime. PaddleOCR's complete
stack and OCRmyPDF are not required; installing a second heavy framework or
Ghostscript normalization would need a demonstrated pipeline benefit. Local
language-model structure arbitration is optional and is not enabled by this
implementation. Clue preparation continues to use only the configured local
clue model.

## Explicit setup

Inspect executables, language data, Python wheels, disk space and existing
private manifests first. Do not replace a working environment unnecessarily.
Python dependencies and the CPU wheel index are pinned in `pyproject.toml` and
`uv.lock`; sync only the existing project virtual environment.

```sh
.venv/bin/python deploy/prepare-ocr.py
.venv/bin/python -m vocabatron.app.dev_runner --cpu-only --cpu-budget 4 -- .venv/bin/uv sync --frozen
.venv/bin/python -m vocabatron.app.dev_runner --cpu-only --cpu-budget 4 -- .venv/bin/python deploy/prepare-ocr.py --acquire
```

The setup script accepts only a directory under the existing project's
`.private/`. On Ubuntu Noble aarch64 it can extract signed-APT-metadata-verified
Tesseract packages locally; it does not run sudo or install system packages.
Poppler and libseccomp must already be available. Other OS/architecture bundles
require separate verification. Inspect the exact package operation before any
privileged system installation.

`deploy/ocr-models.json` pins official upstream URLs, revisions, byte sizes,
SHA256 and licenses. Prepared manifests record exact private local paths. The
layout model and three RapidOCR models occupy approximately 176 MiB; the
English and orientation Tesseract data occupy approximately 14 MiB. The
extracted Tesseract runtime is approximately 28 MiB in total. Python wheel
space is additional. No model files belong in Git, `frontend/public` or Outputs.

Production has no setup invocation. Document child processes install a
fail-closed seccomp network filter before importing parsing/OCR libraries,
probe IPv4/IPv6 denial, and inherit it in their descendants. HF/Transformers
offline flags and explicit verified model paths provide additional protection.
Model integrity or availability errors do not start a download. Working caches
are private, bounded per-job directories and are cleaned after each worker.
This network boundary and resource supervisor are not a full filesystem
sandbox against native parser vulnerabilities.

## Recovery and compatibility

Parser upgrades enqueue one new task per failed source and parser version.
The old task and its report remain immutable history. New source-attempt
records retain previous and current report references, native/OCR attempts,
queue/resource waiting and processing stage timings. Semantic aliases reuse
existing lesson IDs and frozen clues across equivalent native/OCR packaging.
Answer-set and structure-family rules, historical numbering and deduplication
are unchanged.

A complete search hint is independently checked for layout legality, both
historical equivalence classes and exact typography before acceptance. This
avoids rebuilding a solver model merely to rediscover the same complete valid
candidate. Invalid, duplicate, incomplete or overflowing hints still reach
the full remaining CP-SAT domain. This direct acceptance path makes no
exhaustion claim. Thread calibration distinguishes complete-hint latency from
bounded cold search; UNKNOWN cold trials are never counted as completed
variants or exhaustion evidence.

Activity dismissal and import-notice dismissal are separate persistent flags.
Only terminal work can be dismissed; History can restore it. Successful verified
migrations are excluded from the default feed. No dismissal deletes archive
objects, source reports, tasks or variants.

The synthetic regression suite never downloads assets. Explicitly select a
prepared private OCR root to exercise native, image-only, low resolution,
rotation, skew, mixed-content, continuation and disagreement checks:

```sh
VOCABATRON_TEST_OCR_ROOT="$PWD/.private/web/ocr" .venv/bin/python -m vocabatron.app.dev_runner --cpu-only --cpu-budget 4 -- .venv/bin/pytest
```

OCR workers must report `seccomp-network-denied-and-probed`. Missing model
tests must fail without creating an implicit cache or contacting a server.
Real private acceptance remains explicitly authorized and separate from this
synthetic suite. Final source requires a complete regression before deployment.
