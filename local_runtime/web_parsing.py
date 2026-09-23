"""Browser-upload additions without changing existing CLI parsing/cache identities."""

from .portable_paths import map_metadata

import json
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .material_errors import MaterialError
from .materials import SUPPORTED, Parser, digest, write_json

WEB_EXTENSIONS = (SUPPORTED - {".doc"}) | {".json", ".csv"}
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class WebParser(Parser):
    def parse(self, source: Path, sha: str) -> list[dict]:
        suffix = source.suffix.lower()
        fixed_pdf = self.cache_dir / "rendered" / sha / "render.json"
        word_fallback = (
            suffix == ".docx"
            and not fixed_pdf.exists()
            and not (shutil.which("soffice") or shutil.which("libreoffice"))
        )
        if not word_fallback and suffix not in {".json", ".csv"}:
            return super().parse(source, sha)
        key = digest(("web-text-v1" + sha + self.model + self.settings.llm.base_url).encode())
        target = self.cache_dir / "parsed" / (key + ".json")
        if target.exists():
            return map_metadata(json.loads(target.read_text())["units"])
        if word_fallback:
            units = self.docx_units(source)
        else:
            if source.stat().st_size > 8 * 1024 * 1024:
                raise MaterialError("JSON/CSV 文件最多 8 MB，请先拆分文件")
            text = source.read_text(encoding="utf-8-sig")
            if suffix == ".json":
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            units = [{"text": text, "metadata": {"extraction_method": "text", "page_basis": "not_paginated"}}]
        if not any(unit["text"].strip() for unit in units):
            raise MaterialError("文件中没有可提取的文字或图片")
        write_json(target, {"source_sha256": sha, "parser_version": "web-text-v1", "units": units})
        return units

    def docx_units(self, source: Path) -> list[dict]:
        from PIL import Image

        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 5000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
                raise MaterialError("Word 文件解压后过大，请拆分或转换为 PDF")
            document = ElementTree.fromstring(archive.read("word/document.xml"))
            body = document.find(WORD_NS + "body")
            if body is None:
                raise MaterialError("Word 文件没有正文")
            pieces = []
            for element in body:
                if element.tag == WORD_NS + "tbl":
                    for row in element.findall(WORD_NS + "tr"):
                        pieces.append(
                            " | ".join(
                                "".join(t.text or "" for t in cell.iter(WORD_NS + "t"))
                                for cell in row.findall(WORD_NS + "tc")
                            )
                        )
                else:
                    pieces.append("".join(t.text or "" for t in element.iter(WORD_NS + "t")))
            meta = {
                "page_basis": "not_paginated",
                "extraction_method": "docx_text",
                "parsing_note": "按 Word 正文和表格提取；未固定排版，无物理页码。精确页码请上传 PDF。",
            }
            units = [{"text": "\n".join(pieces), "metadata": meta}]
            images = [i for i in entries if i.filename.startswith("word/media/")]
            if len(images) > 100:
                raise MaterialError("Word 内嵌图片超过 100 张，请拆分或转换为 PDF")
            for item in images:
                if Path(item.filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff"}:
                    units.append({"text": f"Word 中有未解析的图形：{Path(item.filename).name}", "metadata": meta})
                    continue
                with archive.open(item) as handle, Image.open(handle) as image:
                    units.extend(
                        self.visual_units(
                            image,
                            {
                                **meta,
                                "embedded_image": Path(item.filename).name,
                                "parsing_note": "Word 内嵌图片，未建立图片与物理页码的对应关系。",
                            },
                        )
                    )
            return units
