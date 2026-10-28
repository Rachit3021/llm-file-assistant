"""Unit tests for fs_tools. Run: pytest -v"""

from fs_tools import list_files, read_file, search_in_file, write_file


def test_write_then_read(tmp_path):
    target = tmp_path / "nested" / "dir" / "note.txt"
    res = write_file(str(target), "Hello Python world")
    assert res["success"] and res["created_directories"] and res["action"] == "created"

    doc = read_file(str(target))
    assert doc["success"]
    assert doc["content"] == "Hello Python world"
    assert doc["metadata"]["word_count"] == 3


def test_read_missing_and_unsupported(tmp_path):
    assert read_file(str(tmp_path / "nope.txt"))["success"] is False
    bad = tmp_path / "image.png"
    bad.write_bytes(b"\x89PNG")
    assert "Unsupported" in read_file(str(bad))["error"]


def test_write_refuses_binary(tmp_path):
    assert write_file(str(tmp_path / "x.pdf"), "text")["success"] is False


def test_list_files_filter(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.PDF").write_text("b")
    (tmp_path / ".hidden").write_text("h")
    assert len(list_files(str(tmp_path))) == 2
    assert [f["name"] for f in list_files(str(tmp_path), "pdf")] == ["b.PDF"]
    assert list_files(str(tmp_path / "missing"))[0]["success"] is False


def test_search_case_insensitive_with_context(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("Skills: PYTHON, SQL\nBuilt python scripts for\nmachine\nlearning")
    res = search_in_file(str(f), "python")
    assert res["match_count"] == 2
    assert res["matches"][1]["line"] == 2
    assert "Skills" in res["matches"][0]["context"]
    # phrase split across a line break still matches
    assert search_in_file(str(f), "machine learning")["found"]


def test_search_empty_keyword(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("x")
    assert search_in_file(str(f), "  ")["success"] is False