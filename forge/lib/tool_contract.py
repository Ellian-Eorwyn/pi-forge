#!/usr/bin/env python3

import argparse
import json
import sys


class ToolInputError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def parse_tool_arguments():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--input")
    parser.add_argument("--output")
    return parser.parse_args()


def read_tool_input(args):
    raw = ""
    if args.input:
        with open(args.input, "r", encoding="utf-8") as handle:
            raw = handle.read()
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ToolInputError("invalid_json", str(error)) from error


def ok_result(artifacts=None, warnings=None, data=None):
    return {
        "status": "ok",
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "errors": [],
        "data": data,
    }


def error_result(error):
    code = getattr(error, "code", "tool_error")
    return {
        "status": "error",
        "artifacts": [],
        "warnings": [],
        "errors": [{"code": code, "message": str(error)}],
    }


def write_tool_result(result, args):
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)


def run_tool(handler):
    args = parse_tool_arguments()
    try:
        write_tool_result(handler(read_tool_input(args)), args)
    except BaseException as error:
        write_tool_result(error_result(error), args)
        raise SystemExit(1) from None


def required_string(payload, key):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError("missing_required_field", f"{key} is required")
    return value
