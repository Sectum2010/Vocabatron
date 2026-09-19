"""Versioned delivery identity, independent of extendable execution budgets."""
from pathlib import Path
import reportlab
from .domain import digest
from .storage import sha256

OUTPUT_RULES={'grid':20,'pages':2,'all_words_once':True,'different_crossings':True,
              'exclude_dihedral_translations':True,'style':'vera-overlay-v1','minimum_clue_size':11,
              'sizes':[12,11.5,11],'line_spacing':1.35,'entry_gap':5,'column_margin':12}


def request_identity(config,lesson,frozen,template_sha256):
    return {'schema_version':2,'lesson_number':lesson.lesson,'source_sha256':lesson.source_sha256,
        'lesson_content_version':lesson.content_version,'frozen_version':frozen.version,
        'selection_provenance':{'schema':frozen.schema_version,'run':frozen.selection_run_id,'evidence':frozen.evidence_sha256,
                                'model_digest':frozen.model_digest},
        'template_sha256':template_sha256,'prefixes':list(config.prefixes),
        'output_rules':dict(OUTPUT_RULES),'font_sha256':sha256(Path(reportlab.__file__).parent/'fonts'/'Vera.ttf'),
        'solver_seed':config.solver.seed}
