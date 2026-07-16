#!/usr/bin/env python3

import argparse
import json
import posixpath
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_ID = f"{{{NS['r']}}}id"


def fail(message):
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def xml_root(archive, name):
    try:
        return ET.fromstring(archive.read(name))
    except KeyError:
        fail(f"PPTX member is missing: {name}")
    except ET.ParseError as error:
        fail(f"PPTX member is invalid XML ({name}): {error}")


def relationships(archive, source_name):
    source_directory = posixpath.dirname(source_name)
    rels_name = posixpath.join(source_directory, "_rels", posixpath.basename(source_name) + ".rels")
    try:
        root = ET.fromstring(archive.read(rels_name))
    except KeyError:
        return {}
    except ET.ParseError as error:
        fail(f"PPTX relationships are invalid XML ({rels_name}): {error}")
    result = {}
    for relation in root.findall(f"{{{REL_NS}}}Relationship"):
        target = relation.get("Target")
        if target and relation.get("TargetMode") != "External":
            result[relation.get("Id")] = {
                "path": posixpath.normpath(posixpath.join(source_directory, target)),
                "type": relation.get("Type", ""),
            }
    return result


def text_lines(element):
    lines = []
    for paragraph in element.findall(".//a:p", NS):
        text = "".join(node.text or "" for node in paragraph.findall(".//a:t", NS)).strip()
        if text:
            lines.append(text)
    return lines


def placeholder_type(shape):
    placeholder = shape.find("./p:nvSpPr/p:nvPr/p:ph", NS)
    return placeholder.get("type", "body") if placeholder is not None else None


def shape_text(shape):
    return text_lines(shape)


def markdown_table(table):
    rows = []
    for row in table.findall("./a:tr", NS):
        cells = []
        for cell in row.findall("./a:tc", NS):
            value = " ".join(text_lines(cell)).replace("|", "\\|").strip()
            cells.append(value)
        if cells:
            rows.append(cells)
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    output = [f"| {' | '.join(header)} |", f"| {' | '.join(['---'] * width)} |"]
    output.extend(f"| {' | '.join(row)} |" for row in rows[1:])
    return output


def slide_parts(archive, slide_name, slide_number):
    root = xml_root(archive, slide_name)
    title = None
    body = []
    for shape in root.findall(".//p:sp", NS):
        lines = shape_text(shape)
        if not lines:
            continue
        if placeholder_type(shape) in {"title", "ctrTitle"} and title is None:
            title = " ".join(lines)
        else:
            body.extend(lines)
    tables = [markdown_table(table) for table in root.findall(".//a:tbl", NS)]
    tables = [table for table in tables if table]
    alt_text = []
    for properties in root.findall(".//p:pic/p:nvPicPr/p:cNvPr", NS):
        description = (properties.get("descr") or properties.get("title") or "").strip()
        if description:
            alt_text.append(description)
    chart_count = len(root.findall(".//a:graphicData", NS)) - len(tables)
    image_count = len(root.findall(".//p:pic", NS))
    embedded_count = len(root.findall(".//p:oleObj", NS))

    notes = []
    for relation in relationships(archive, slide_name).values():
        if relation["type"].endswith("/notesSlide"):
            notes_root = xml_root(archive, relation["path"])
            for shape in notes_root.findall(".//p:sp", NS):
                if placeholder_type(shape) not in {"hdr", "ftr", "dt", "sldNum"}:
                    notes.extend(shape_text(shape))

    warnings = []
    if image_count:
        warnings.append(f"Slide {slide_number} contains {image_count} image(s); extracted alt text does not verify visual meaning.")
    if chart_count > 0:
        warnings.append(f"Slide {slide_number} contains {chart_count} chart or unsupported drawing object(s) requiring visual review.")
    if embedded_count:
        warnings.append(f"Slide {slide_number} contains {embedded_count} embedded object(s) not extracted.")
    if not body and not tables and not notes and (image_count or chart_count > 0 or embedded_count):
        warnings.append(f"Slide {slide_number} is visual-only or has no extractable explanatory text.")
    return title, body, tables, alt_text, notes, warnings


def core_properties(archive):
    try:
        root = ET.fromstring(archive.read("docProps/core.xml"))
    except (KeyError, ET.ParseError):
        return {"title": None, "author": None, "date": None, "source": None}

    def value(xpath):
        node = root.find(xpath, NS)
        return node.text.strip() if node is not None and node.text and node.text.strip() else None

    return {
        "title": value("dc:title"),
        "author": value("dc:creator"),
        "date": value("dcterms:created"),
        "source": None,
    }


def convert(path):
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        fail(f"unable to open PPTX: {error}")
    with archive:
        presentation_name = "ppt/presentation.xml"
        presentation = xml_root(archive, presentation_name)
        rels = relationships(archive, presentation_name)
        slide_names = []
        for slide_id in presentation.findall("./p:sldIdLst/p:sldId", NS):
            relation = rels.get(slide_id.get(REL_ID))
            if relation:
                slide_names.append(relation["path"])
        if not slide_names:
            fail("presentation contains no ordered slides")

        markdown_lines = []
        source_map = []
        warnings = []
        for slide_number, slide_name in enumerate(slide_names, 1):
            title, body, tables, alt_text, notes, slide_warnings = slide_parts(archive, slide_name, slide_number)
            start_line = len(markdown_lines) + 1
            heading = f"# Slide {slide_number}"
            if title:
                heading += f": {title}"
            markdown_lines.extend([heading, ""])
            if body:
                markdown_lines.extend(body)
                markdown_lines.append("")
            for table in tables:
                markdown_lines.extend(table)
                markdown_lines.append("")
            if alt_text:
                markdown_lines.extend(["## Image Alt Text", ""])
                markdown_lines.extend(f"- {value}" for value in alt_text)
                markdown_lines.append("")
            if notes:
                markdown_lines.extend(["## Speaker Notes", ""])
                markdown_lines.extend(notes)
                markdown_lines.append("")
            while markdown_lines and markdown_lines[-1] == "" and len(markdown_lines) > start_line and markdown_lines[-2] == "":
                markdown_lines.pop()
            end_line = len(markdown_lines)
            markdown_lines.append("")
            source_map.append(
                {
                    "markdownStartLine": start_line,
                    "markdownEndLine": end_line,
                    "sourceLocator": {"type": "slide", "value": slide_number, "title": title},
                    "method": "document-conversion",
                    "confidence": "high",
                }
            )
            warnings.extend(slide_warnings)
        markdown = "\n".join(markdown_lines).rstrip() + "\n"
        return {
            "markdown": markdown,
            "method": "pptx-ooxml",
            "pageCount": len(slide_names),
            "warnings": warnings,
            "embedded": core_properties(archive),
            "sourceMapEntries": source_map,
        }


def main():
    parser = argparse.ArgumentParser(description="Extract ordered PPTX text, tables, notes, alt text, and slide source maps.")
    parser.add_argument("input")
    args = parser.parse_args()
    path = Path(args.input).expanduser().resolve()
    if path.suffix.lower() != ".pptx":
        fail("input must be a .pptx file")
    if not path.is_file():
        fail(f"input does not exist: {path}")
    print(json.dumps(convert(path), ensure_ascii=False))


if __name__ == "__main__":
    main()
