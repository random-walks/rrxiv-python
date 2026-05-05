"""Public surface of the rrxiv parser package."""

from rrxiv.parser.build import build_cir
from rrxiv.parser.sidecar import (
    EdgeMarker,
    EnvMarker,
    MetaMarker,
    Sidecar,
    parse_sidecar_file,
    parse_sidecar_text,
)
from rrxiv.parser.tex import (
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
