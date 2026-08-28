import unicodedata
from pathlib import Path

from knowledge import KnowledgeBase


def test_empty_directory(tmp_path: Path):
    assert KnowledgeBase(tmp_path / "missing").search("bedrock") == []


def test_vietnamese_accentless_and_nfd_queries(tmp_path: Path):
    (tmp_path / "joining.md").write_text(
        "# Kết nối Bedrock\nCổng Bedrock là 19132. Không vào được máy chủ thì kiểm tra cổng.",
        encoding="utf-8",
    )
    kb = KnowledgeBase(tmp_path)
    accentless = kb.search("cong bedrock khong vao duoc may chu")
    nfd = kb.search(unicodedata.normalize("NFD", "Cổng Bedrock"))
    assert accentless and "19132" in accentless[0].chunk.text
    assert nfd and nfd[0].chunk.source == "joining.md"


def test_recursive_loading_and_duplicate_suppression(tmp_path: Path):
    (tmp_path / "guides").mkdir()
    content = "# Lệnh\nDùng /warp survival để đến khu sinh tồn."
    (tmp_path / "guides/commands.md").write_text(content, encoding="utf-8")
    (tmp_path / "duplicate.txt").write_text(content, encoding="utf-8")
    kb = KnowledgeBase(tmp_path)
    results = kb.search("warp survival")
    assert len(results) == 1
    assert not Path(results[0].chunk.source).is_absolute()
