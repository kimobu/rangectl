from __future__ import annotations

import base64
import platform
import re
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape


def register(add: Callable[[str, Any], None]) -> None:
    add("office.word", word)
    add("office.excel", excel)
    add("office.powerpoint", powerpoint)


def word(context: Any, action: Any) -> dict[str, Any]:
    topic = str(action.args.get("topic", "work notes"))
    directory = Path(str(action.args.get("directory", "~/Documents"))).expanduser()
    filename = str(action.args.get("filename") or _filename(topic, "docx"))
    target = directory / filename
    if platform.system().lower() == "windows" and bool(action.args.get("use_app", True)):
        if context.dry_run:
            return {"target": str(target), "mode": "com"}
        content = context.content.generate(persona=context.config.persona, topic=topic, kind="document", seed=filename)
        return _word_com(context, target, content)
    if context.dry_run:
        return {"target": str(target), "mode": "docx"}
    content = context.content.generate(persona=context.config.persona, topic=topic, kind="document", seed=filename)
    return _write_docx(context, target, content)


def excel(context: Any, action: Any) -> dict[str, Any]:
    topic = str(action.args.get("topic", "status tracker"))
    directory = Path(str(action.args.get("directory", "~/Documents"))).expanduser()
    filename = str(action.args.get("filename") or _filename(topic, "xlsx"))
    target = directory / filename
    if platform.system().lower() == "windows" and bool(action.args.get("use_app", True)):
        if context.dry_run:
            return {"target": str(target), "mode": "com"}
        content = context.content.generate(persona=context.config.persona, topic=topic, kind="csv", seed=filename)
        return _excel_com(context, target, content)
    if context.dry_run:
        return {"target": str(target), "mode": "xlsx"}
    content = context.content.generate(persona=context.config.persona, topic=topic, kind="csv", seed=filename)
    return _write_xlsx(context, target, content)


def powerpoint(context: Any, action: Any) -> dict[str, Any]:
    topic = str(action.args.get("topic", "team update"))
    directory = Path(str(action.args.get("directory", "~/Documents"))).expanduser()
    filename = str(action.args.get("filename") or _filename(topic, "pptx"))
    target = directory / filename
    if platform.system().lower() == "windows" and bool(action.args.get("use_app", True)):
        if context.dry_run:
            return {"target": str(target), "mode": "com"}
        content = context.content.generate(persona=context.config.persona, topic=topic, kind="presentation", seed=filename)
        return _powerpoint_com(context, target, content)
    if context.dry_run:
        return {"target": str(target), "mode": "pptx"}
    content = context.content.generate(persona=context.config.persona, topic=topic, kind="presentation", seed=filename)
    return _write_pptx(context, target, content)


def _word_com(context: Any, target: Path, content: str) -> dict[str, Any]:
    script = rf"""
$ErrorActionPreference = "Stop"
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$doc = $word.Documents.Add()
$selection = $word.Selection
$selection.TypeText("{_ps_escape(content)}")
$dir = Split-Path "{_ps_escape(str(target))}"
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$doc.SaveAs([ref]"{_ps_escape(str(target))}")
$doc.Close()
$word.Quit()
"""
    return _run_or_dry(context, target, script)


def _excel_com(context: Any, target: Path, content: str) -> dict[str, Any]:
    rows = [line.split(",") for line in content.splitlines() if line.strip()]
    assignments = []
    for r_index, row in enumerate(rows[:40], start=1):
        for c_index, value in enumerate(row[:20], start=1):
            assignments.append(f'$sheet.Cells.Item({r_index},{c_index}).Value2 = "{_ps_escape(value)}"')
    script = rf"""
$ErrorActionPreference = "Stop"
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$book = $excel.Workbooks.Add()
$sheet = $book.Worksheets.Item(1)
{chr(10).join(assignments)}
$dir = Split-Path "{_ps_escape(str(target))}"
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$book.SaveAs("{_ps_escape(str(target))}")
$book.Close($false)
$excel.Quit()
"""
    return _run_or_dry(context, target, script)


def _powerpoint_com(context: Any, target: Path, content: str) -> dict[str, Any]:
    title, _, body = content.partition("\n")
    script = rf"""
$ErrorActionPreference = "Stop"
$ppt = New-Object -ComObject PowerPoint.Application
$presentation = $ppt.Presentations.Add()
$slide = $presentation.Slides.Add(1, 1)
$slide.Shapes.Title.TextFrame.TextRange.Text = "{_ps_escape(title or 'Team Update')}"
$slide.Shapes.Item(2).TextFrame.TextRange.Text = "{_ps_escape(body or content)}"
$dir = Split-Path "{_ps_escape(str(target))}"
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$presentation.SaveAs("{_ps_escape(str(target))}")
$presentation.Close()
$ppt.Quit()
"""
    return _run_or_dry(context, target, script)


def _run_or_dry(context: Any, target: Path, script: str) -> dict[str, Any]:
    if context.dry_run:
        return {"target": str(target), "mode": "com"}
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return {"target": str(target), "mode": "com"}


def _write_docx(context: Any, target: Path, content: str) -> dict[str, Any]:
    if context.dry_run:
        return {"target": str(target), "mode": "docx"}
    target.parent.mkdir(parents=True, exist_ok=True)
    paragraphs = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in content.splitlines() or [content])
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_DOCX)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/document.xml", f"{_DOC_START}{paragraphs}{_DOC_END}")
    return {"target": str(target), "mode": "docx", "bytes": target.stat().st_size}


def _write_xlsx(context: Any, target: Path, content: str) -> dict[str, Any]:
    if context.dry_run:
        return {"target": str(target), "mode": "xlsx"}
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for r_index, row in enumerate((line.split(",") for line in content.splitlines() if line.strip()), start=1):
        cells = "".join(
            f'<c r="{chr(64 + c_index)}{r_index}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            for c_index, value in enumerate(row[:26], start=1)
        )
        rows.append(f'<row r="{r_index}">{cells}</row>')
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XLSX)
        archive.writestr("_rels/.rels", _XLSX_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", f'{_SHEET_START}{"".join(rows)}{_SHEET_END}')
    return {"target": str(target), "mode": "xlsx", "bytes": target.stat().st_size}


def _write_pptx(context: Any, target: Path, content: str) -> dict[str, Any]:
    if context.dry_run:
        return {"target": str(target), "mode": "pptx"}
    target.parent.mkdir(parents=True, exist_ok=True)
    title, _, body = content.partition("\n")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_PPTX)
        archive.writestr("_rels/.rels", _PPTX_RELS)
        archive.writestr("ppt/presentation.xml", _PRESENTATION)
        archive.writestr("ppt/_rels/presentation.xml.rels", _PRESENTATION_RELS)
        archive.writestr("ppt/slides/slide1.xml", _slide_xml(title or "Team Update", body or content))
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", "")
    return {"target": str(target), "mode": "pptx", "bytes": target.stat().st_size}


def _filename(topic: str, suffix: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in topic).strip("-") or "artifact"
    return f"{slug}-{int(time.time())}.{suffix}"


def _ps_escape(value: str) -> str:
    return re.sub(r'(["`$])', r"`\1", value).replace("\r", "").replace("\n", "`r`n")


_CONTENT_TYPES_DOCX = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
_RELS = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
_DOC_START = '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
_DOC_END = "</w:body></w:document>"
_CONTENT_TYPES_XLSX = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
_XLSX_RELS = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
_WORKBOOK = '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
_WORKBOOK_RELS = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
_SHEET_START = '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
_SHEET_END = "</sheetData></worksheet>"
_CONTENT_TYPES_PPTX = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>'
_PPTX_RELS = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>'
_PRESENTATION = '<?xml version="1.0" encoding="UTF-8"?><p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/></p:presentation>'
_PRESENTATION_RELS = '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>'


def _slide_xml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{escape(title)}</a:t></a:r></a:p></p:txBody></p:sp>'
        f'<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{escape(body)}</a:t></a:r></a:p></p:txBody></p:sp>'
        '</p:spTree></p:cSld></p:sld>'
    )
