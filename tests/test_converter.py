import json
import zipfile
from pathlib import Path

from converter import convert_oraxen_pack


def test_converts_flat_item(tmp_path: Path):
    source_dir = tmp_path / "src"
    (source_dir / "assets/oraxen/items").mkdir(parents=True)
    (source_dir / "assets/oraxen/models/item").mkdir(parents=True)
    (source_dir / "assets/oraxen/textures/item").mkdir(parents=True)
    (source_dir / "assets/oraxen/items/ruby.json").write_text(
        json.dumps({"model": {"type": "minecraft:model", "model": "oraxen:item/ruby"}})
    )
    (source_dir / "assets/oraxen/models/item/ruby.json").write_text(
        json.dumps({"parent": "minecraft:item/generated", "textures": {"layer0": "oraxen:item/ruby"}})
    )
    (source_dir / "assets/oraxen/textures/item/ruby.png").write_bytes(b"png")
    source_zip = tmp_path / "source.zip"
    with zipfile.ZipFile(source_zip, "w") as archive:
        for path in source_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir))
    output = tmp_path / "output.zip"
    summary = convert_oraxen_pack(source_zip, output, "paper")
    assert summary.item_count == 1
    with zipfile.ZipFile(output) as archive:
        mapping = json.loads(archive.read("geyser-mappings.json"))
        assert mapping["format_version"] == 2
        assert mapping["items"]["minecraft:paper"][0]["model"] == "oraxen:ruby"
        assert "bedrock-pack/manifest.json" in archive.namelist()


def test_combined_zip_uses_oraxen_material_and_name(tmp_path: Path):
    source_dir = tmp_path / "combined"
    pack = source_dir / "resource-pack"
    (pack / "assets/oraxen/items").mkdir(parents=True)
    (pack / "assets/oraxen/models/item").mkdir(parents=True)
    (pack / "assets/oraxen/textures/item").mkdir(parents=True)
    (source_dir / "Oraxen/items").mkdir(parents=True)
    (pack / "assets/oraxen/items/ruby.json").write_text(
        json.dumps({"model": {"type": "minecraft:model", "model": "oraxen:item/ruby"}})
    )
    (pack / "assets/oraxen/models/item/ruby.json").write_text(
        json.dumps({"parent": "minecraft:item/generated", "textures": {"layer0": "oraxen:item/ruby"}})
    )
    (pack / "assets/oraxen/textures/item/ruby.png").write_bytes(b"png")
    (source_dir / "Oraxen/items/gems.yml").write_text(
        "ruby:\n  material: DIAMOND\n  displayname: '<red>Ruby Gem'\n", encoding="utf-8"
    )
    source_zip = tmp_path / "source.zip"
    with zipfile.ZipFile(source_zip, "w") as archive:
        for path in source_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir))
    output = tmp_path / "output.zip"
    convert_oraxen_pack(source_zip, output, "paper")
    with zipfile.ZipFile(output) as archive:
        mapping = json.loads(archive.read("geyser-mappings.json"))
        definition = mapping["items"]["minecraft:diamond"][0]
        assert definition["display_name"] == "Ruby Gem"
