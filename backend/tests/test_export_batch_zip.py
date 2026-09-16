"""Assembly of the combined product-export zip.

The subtle part is where a child export's images live. ``_register_image``
deliberately never stages them under the export directory — it streams them from
the media root at zip time so the exports volume isn't doubled — so a child's
loose files are the markdown ALONE and its images exist only inside its own zip.
Walking loose files would therefore yield an image-less bundle silently, which is
the failure this module pins.
"""
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routes.export import _append_export, _batch_zip_name


def _child_with_bundle(tmp_path, name="Guide"):
    """A child export as the engine leaves it: loose .md, plus a zip that also
    carries the images (which are NOT on disk beside the .md)."""
    d = tmp_path / "child-1"
    d.mkdir()
    (d / f"{name}.md").write_text("# Guide\n\n![shot](images/a1/shot.png)\n")
    with zipfile.ZipFile(d / f"{name}.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{name}.md", "# Guide\n\n![shot](images/a1/shot.png)\n")
        z.writestr("images/a1/shot.png", b"\x89PNG\r\n\x1a\n" + b"x" * 512)
    return d


def _child_text_only(tmp_path):
    """A markdown export with include_images off: no zip at all."""
    d = tmp_path / "child-2"
    d.mkdir()
    (d / "Admin_Guide.md").write_text("# Admin\n")
    return d


def test_images_survive_into_the_combined_zip(tmp_path):
    src = _child_with_bundle(tmp_path)
    out_path = tmp_path / "batch.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        written = _append_export(out, str(src), "User_Guide")
    assert written == 2
    with zipfile.ZipFile(out_path) as z:
        names = set(z.namelist())
        assert "User_Guide/Guide.md" in names
        assert "User_Guide/images/a1/shot.png" in names, (
            "images live only in the child's zip; walking loose files loses them"
        )
        # Content copied verbatim, not truncated or re-encoded into nonsense.
        assert z.read("User_Guide/images/a1/shot.png").startswith(b"\x89PNG")


def test_relative_image_links_still_resolve_inside_the_folder(tmp_path):
    # The markdown says images/a1/shot.png; nesting both under the same folder is
    # what keeps that link working after extraction.
    src = _child_with_bundle(tmp_path)
    out_path = tmp_path / "batch.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        _append_export(out, str(src), "User_Guide")
    with zipfile.ZipFile(out_path) as z:
        md = z.read("User_Guide/Guide.md").decode()
        ref = md.split("(")[1].split(")")[0]
        assert f"User_Guide/{ref}" in z.namelist()


def test_a_text_only_export_falls_back_to_its_loose_files(tmp_path):
    src = _child_text_only(tmp_path)
    out_path = tmp_path / "batch.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        written = _append_export(out, str(src), "Admin")
    assert written == 1
    with zipfile.ZipFile(out_path) as z:
        assert z.namelist() == ["Admin/Admin_Guide.md"]


def test_the_childs_own_zip_is_never_nested(tmp_path):
    src = _child_with_bundle(tmp_path)
    out_path = tmp_path / "batch.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        _append_export(out, str(src), "User_Guide")
    with zipfile.ZipFile(out_path) as z:
        assert not [n for n in z.namelist() if n.endswith(".zip")], (
            "a zip of zips can't be browsed and defeats the point of one download"
        )


def test_two_sources_stay_in_separate_folders(tmp_path):
    a, b = _child_with_bundle(tmp_path), _child_text_only(tmp_path)
    out_path = tmp_path / "batch.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        _append_export(out, str(a), "User_Guide")
        _append_export(out, str(b), "Admin_Guide")
    with zipfile.ZipFile(out_path) as z:
        names = z.namelist()
    assert "User_Guide/Guide.md" in names and "Admin_Guide/Admin_Guide.md" in names
    # Same-named files across sources must not collide.
    assert len(names) == len(set(names))


def test_a_purged_child_contributes_nothing_rather_than_raising(tmp_path):
    out_path = tmp_path / "batch.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        # Directory exists but is empty — retention removed its contents.
        empty = tmp_path / "gone"
        empty.mkdir()
        assert _append_export(out, str(empty), "Gone") == 0


def test_batch_zip_name_is_filesystem_safe():
    assert _batch_zip_name("Veeam / Veeam Backup & Replication") == (
        "Veeam_Veeam_Backup_Replication.zip"
    )
    assert _batch_zip_name("Dell / NetWorker") == "Dell_NetWorker.zip"
    assert _batch_zip_name("///") == "export.zip"
