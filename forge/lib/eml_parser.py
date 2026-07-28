#!/usr/bin/env python3

"""Deterministic RFC 5322/MIME parsing for Forge email workflows."""

import argparse
import hashlib
import html
import json
import mimetypes
import re
import unicodedata
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path


SELECTED_HEADERS = (
    ("subject", "Subject"),
    ("from", "From"),
    ("to", "To"),
    ("cc", "Cc"),
    ("bcc", "Bcc"),
    ("date", "Date"),
    ("messageId", "Message-ID"),
    ("inReplyTo", "In-Reply-To"),
    ("references", "References"),
)
MESSAGE_ID_PATTERN = re.compile(r"<[^<>\s]+>")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def decode_header_value(value, warnings, label):
    if value is None:
        return None
    decoded = []
    try:
        fragments = decode_header(str(value))
    except (LookupError, ValueError) as error:
        warnings.append(f"Could not decode {label}: {error}.")
        return str(value)
    for fragment, charset in fragments:
        if isinstance(fragment, str):
            decoded.append(fragment)
            continue
        encoding = charset or "ascii"
        try:
            decoded.append(fragment.decode(encoding))
        except (LookupError, UnicodeDecodeError):
            decoded.append(fragment.decode("utf-8", errors="replace"))
            warnings.append(f"Decoded {label} with UTF-8 replacement after the declared charset {encoding!r} failed.")
    return "".join(decoded).replace("\r", " ").replace("\n", " ").strip()


def decode_text_part(part, warnings, part_path):
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw.replace("\r\n", "\n").replace("\r", "\n"), part.get_content_charset()
        return "", part.get_content_charset()
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        text = payload.decode("utf-8", errors="replace")
        warnings.append(f"Decoded MIME part {part_path} with UTF-8 replacement after charset {charset!r} failed.")
    return text.replace("\r\n", "\n").replace("\r", "\n"), charset


class MarkdownHTMLParser(HTMLParser):
    BLOCK_TAGS = {"article", "aside", "blockquote", "div", "footer", "header", "main", "nav", "p", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.list_stack = []
        self.link_stack = []

    def newline(self, count=1):
        current = "".join(self.parts)
        existing = len(current) - len(current.rstrip("\n"))
        self.parts.append("\n" * max(0, count - existing))

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in self.BLOCK_TAGS:
            self.newline(2)
        elif tag == "br":
            self.newline()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.newline(2)
            self.parts.append(f"{'#' * int(tag[1])} ")
        elif tag in {"ul", "ol"}:
            self.list_stack.append(tag)
            self.newline()
        elif tag == "li":
            self.newline()
            indent = "  " * max(0, len(self.list_stack) - 1)
            marker = "1. " if self.list_stack and self.list_stack[-1] == "ol" else "- "
            self.parts.append(f"{indent}{marker}")
        elif tag == "blockquote":
            self.newline(2)
            self.parts.append("> ")
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "a":
            self.link_stack.append(attributes.get("href"))
            self.parts.append("[")
        elif tag == "img":
            alternate = attributes.get("alt", "").strip()
            source = attributes.get("src", "").strip()
            if alternate:
                self.parts.append(f"![{alternate}]({source})" if source and not source.lower().startswith(("http://", "https://")) else f"[Image: {alternate}]")

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS or tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.newline(2)
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self.newline(2)
        elif tag in {"strong", "b"}:
            self.parts.append("**")
        elif tag in {"em", "i"}:
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("`")
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else None
            self.parts.append("]")
            if href:
                self.parts.append(f"({href})")

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def markdown(self):
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def html_to_markdown(value):
    parser = MarkdownHTMLParser()
    parser.feed(value)
    parser.close()
    return parser.markdown()


def safe_filename(value, fallback):
    raw = unicodedata.normalize("NFKC", value or fallback).strip()
    raw = Path(raw.replace("\\", "/")).name
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", raw)
    safe = re.sub(r"\s+", " ", safe).strip(" .-")
    return safe or fallback


def unique_filename(directory, desired, used):
    stem = Path(desired).stem or "attachment"
    suffix = Path(desired).suffix
    candidate = desired
    number = 1
    while candidate.casefold() in used or (directory / candidate).exists():
        number += 1
        candidate = f"{stem}-{number}{suffix}"
    used.add(candidate.casefold())
    return candidate


def part_payload_bytes(part):
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    if part.get_content_type() == "message/rfc822":
        nested = part.get_payload()
        if isinstance(nested, list) and nested:
            return nested[0].as_bytes(policy=policy.default)
    raw = part.get_payload()
    return raw.encode("utf-8") if isinstance(raw, str) else b""


def walk_parts(message, prefix="1"):
    if not message.is_multipart() or message.get_content_type() == "message/rfc822":
        yield prefix, message
        return
    for index, part in enumerate(message.iter_parts(), start=1):
        yield from walk_parts(part, f"{prefix}.{index}")


def is_attachment(part):
    return (
        part.get_content_disposition() == "attachment"
        or part.get_filename() is not None
        or part.get_content_type() == "message/rfc822"
    )


def selected_body(message, warnings):
    plain = []
    html_parts = []
    for part_path, part in walk_parts(message):
        if is_attachment(part):
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        text, charset = decode_text_part(part, warnings, part_path)
        if not text.strip():
            continue
        candidate = {"partPath": part_path, "mimeType": content_type, "charset": charset, "text": text}
        if content_type == "text/plain":
            plain.append(candidate)
        else:
            html_parts.append(candidate)
    if plain:
        chosen = plain[0]
        chosen["markdown"] = chosen["text"].strip()
        return chosen
    if html_parts:
        chosen = html_parts[0]
        chosen["markdown"] = html_to_markdown(chosen["text"])
        warnings.append(f"Email had no usable text/plain body; converted HTML MIME part {chosen['partPath']} conservatively.")
        return chosen
    warnings.append("Email contained no usable text/plain or text/html body.")
    return {"partPath": None, "mimeType": None, "charset": None, "text": "", "markdown": ""}


def parsed_date(value, warnings):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        warnings.append(f"Could not parse the Date header: {value}.")
        return None
    if parsed is None:
        return None
    return parsed.isoformat()


def message_ids(value):
    return MESSAGE_ID_PATTERN.findall(value or "")


def markdown_line_count(lines):
    return len(lines)


def parse_eml(source, attachment_directory=None, attachment_link_prefix=None):
    source = Path(source).expanduser().resolve()
    warnings = []
    raw = source.read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    for defect in message.defects:
        warnings.append(f"MIME parser reported {defect.__class__.__name__}: {defect}.")

    selected = {}
    for key, header_name in SELECTED_HEADERS:
        selected[key] = decode_header_value(message.get(header_name), warnings, header_name)
    all_headers = [
        {"name": name, "value": decode_header_value(value, warnings, name)}
        for name, value in message.raw_items()
    ]
    selected["dateIso"] = parsed_date(selected["date"], warnings)
    selected["referencesIds"] = message_ids(selected["references"])
    selected["inReplyToIds"] = message_ids(selected["inReplyTo"])

    body = selected_body(message, warnings)
    attachments = []
    used_names = set()
    attachment_directory = Path(attachment_directory).expanduser().resolve() if attachment_directory else None
    if attachment_directory:
        attachment_directory.mkdir(parents=True, exist_ok=True)
    attachment_index = 0
    for part_path, part in walk_parts(message):
        if not is_attachment(part):
            continue
        attachment_index += 1
        content_type = part.get_content_type()
        decoded_name = decode_header_value(part.get_filename(), warnings, f"attachment filename at MIME part {part_path}")
        guessed_extension = mimetypes.guess_extension(content_type) or ""
        desired = safe_filename(decoded_name, f"attachment-{attachment_index:04d}{guessed_extension}")
        payload = part_payload_bytes(part)
        output_name = unique_filename(attachment_directory or Path("."), desired, used_names)
        output_path = attachment_directory / output_name if attachment_directory else None
        if output_path:
            with output_path.open("xb") as handle:
                handle.write(payload)
        link = f"{attachment_link_prefix.rstrip('/')}/{output_name}" if attachment_link_prefix else output_name
        attachments.append(
            {
                "partPath": part_path,
                "filename": output_name,
                "originalFilename": decoded_name,
                "mimeType": content_type,
                "disposition": part.get_content_disposition() or ("inline" if part.get("Content-ID") else "attachment"),
                "contentId": decode_header_value(part.get("Content-ID"), warnings, f"Content-ID at MIME part {part_path}"),
                "byteSize": len(payload),
                "sha256": sha256_bytes(payload),
                "path": str(output_path) if output_path else None,
                "link": link,
            }
        )

    title = selected["subject"] or source.stem or "Email"
    lines = [f"# {title}", "", "## Email Metadata", ""]
    source_map = []
    for key, label in SELECTED_HEADERS:
        value = selected[key]
        if not value:
            continue
        line = markdown_line_count(lines) + 1
        rendered = value.replace("`", "\\`") if label in {"Message-ID", "In-Reply-To", "References"} else value
        lines.append(f"- {label}: {rendered}")
        source_map.append(
            {
                "markdownStartLine": line,
                "markdownEndLine": line,
                "sourceLocator": {"type": "email-header", "header": label},
                "method": "document-conversion",
                "confidence": "high",
            }
        )
    lines.extend(["", "## Body", ""])
    body_start = markdown_line_count(lines) + 1 if body["markdown"] else None
    if body["markdown"]:
        lines.extend(body["markdown"].split("\n"))
    body_end = markdown_line_count(lines) if body["markdown"] else None
    if body_start is not None:
        source_map.append(
            {
                "markdownStartLine": body_start,
                "markdownEndLine": body_end,
                "sourceLocator": {
                    "type": "email-mime-part",
                    "partPath": body["partPath"],
                    "mimeType": body["mimeType"],
                },
                "method": "document-conversion",
                "confidence": "high" if body["mimeType"] == "text/plain" else "medium",
            }
        )
    if attachments:
        lines.extend(["", "## Attachments", ""])
        for attachment in attachments:
            line = markdown_line_count(lines) + 1
            lines.append(
                f"- [{attachment['filename']}]({attachment['link']}) — "
                f"{attachment['mimeType']}; {attachment['byteSize']} bytes; SHA-256 `{attachment['sha256']}`"
            )
            source_map.append(
                {
                    "markdownStartLine": line,
                    "markdownEndLine": line,
                    "sourceLocator": {"type": "email-mime-part", "partPath": attachment["partPath"]},
                    "method": "document-conversion",
                    "confidence": "high",
                }
            )
    markdown = "\n".join(lines).rstrip() + "\n"
    return {
        "schemaVersion": 1,
        "source": {"path": str(source), "sha256": sha256_bytes(raw), "byteSize": len(raw)},
        "markdown": markdown,
        "warnings": list(dict.fromkeys(warnings)),
        "email": {
            "headers": all_headers,
            "selectedHeaders": selected,
            "body": {
                "partPath": body["partPath"],
                "mimeType": body["mimeType"],
                "charset": body["charset"],
                "markdownStartLine": body_start,
                "markdownEndLine": body_end,
            },
            "attachments": attachments,
        },
        "sourceMapEntries": source_map,
    }


def main():
    parser = argparse.ArgumentParser(description="Parse one .eml file into deterministic Markdown and provenance.")
    parser.add_argument("source")
    parser.add_argument("--attachments")
    parser.add_argument("--attachment-link-prefix")
    args = parser.parse_args()
    result = parse_eml(args.source, args.attachments, args.attachment_link_prefix)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
