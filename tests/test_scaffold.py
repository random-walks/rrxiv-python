"""Tests for the rrxiv init scaffold."""

from __future__ import annotations

from pathlib import Path

import pytest

from rrxiv.scaffold import ScaffoldOptions, scaffold_paper


def test_creates_expected_files(tmp_path: Path) -> None:
    target = tmp_path / "my-paper"
    out = scaffold_paper(
        target,
        ScaffoldOptions(
            paper_id="my-paper-001",
            title="A test paper",
            author="A. Author",
            license="CC-BY-4.0",
            topics=("example", "test"),
        ),
    )
    assert out == target.resolve()
    assert (target / "my-paper-001.tex").is_file()
    assert (target / "my-paper-001.bib").is_file()
    assert (target / "rrxiv.cls").is_file()
    assert (target / "README.md").is_file()


def test_tex_carries_options(tmp_path: Path) -> None:
    target = tmp_path / "paper"
    scaffold_paper(
        target,
        ScaffoldOptions(
            paper_id="p-001",
            title="T",
            author="A",
            topics=("foo", "bar"),
        ),
    )
    tex = (target / "p-001.tex").read_text()
    assert "\\rrxivid{p-001}" in tex
    assert "\\title{T}" in tex
    assert "\\author{A}" in tex
    assert "\\rrxivtopics{foo,bar}" in tex


def test_basename_override(tmp_path: Path) -> None:
    target = tmp_path / "paper"
    scaffold_paper(
        target,
        ScaffoldOptions(
            paper_id="long-paper-id-123",
            title="T",
            author="A",
            basename="paper",
        ),
    )
    assert (target / "paper.tex").is_file()
    assert (target / "paper.bib").is_file()
    assert not (target / "long-paper-id-123.tex").exists()


def test_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "paper"
    target.mkdir()
    (target / "existing-file.txt").write_text("data")
    with pytest.raises(FileExistsError):
        scaffold_paper(
            target,
            ScaffoldOptions(paper_id="p", title="T", author="A"),
        )


def test_creates_into_empty_existing_dir(tmp_path: Path) -> None:
    """A pre-existing but empty directory is allowed (common when the
    user runs `mkdir my-paper && cd my-paper && rrxiv init .`)."""
    target = tmp_path / "paper"
    target.mkdir()
    scaffold_paper(target, ScaffoldOptions(paper_id="p", title="T", author="A"))
    assert (target / "p.tex").is_file()


def test_cls_self_contained(tmp_path: Path) -> None:
    """The bundled cls should have a header noting it was bundled."""
    target = tmp_path / "paper"
    scaffold_paper(target, ScaffoldOptions(paper_id="p", title="T", author="A"))
    cls = (target / "rrxiv.cls").read_text()
    assert "(bundled)" in cls
    assert "\\ProvidesClass{rrxiv}" in cls
    # Verify the v0.2 pipe-format edge macros are present
    assert "RRXIV:edge:depends_on:#1|#2" in cls
