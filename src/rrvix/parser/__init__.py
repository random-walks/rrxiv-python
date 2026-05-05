"""Public surface of the rrvix parser package."""

from rrvix.parser.build import build_cir
from rrvix.parser.sidecar import (
    EdgeMarker,
    EnvMarker,
    MetaMarker,
    Sidecar,
    parse_sidecar_file,
    parse_sidecar_text,
)
from rrvix.parser.tex import (
    TexDocument,
    parse_tex,
    parse_tex_file,
)

__all__ = [
    "EdgeMarker",
    "EnvMarker",
    "MetaMarker",
    "Sidecar",
    "TexDocument",
    "build_cir",
    "parse_sidecar_file",
    "parse_sidecar_text",
    "parse_tex",
    "parse_tex_file",
]
