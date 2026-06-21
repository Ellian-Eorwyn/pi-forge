---
name: pi-forge-delegation
description: Delegate local transcription and file-conversion work to the pi-forge MCP server and consume its structured results.
---

# Pi-forge Delegation

Use the configured pi-forge MCP tools for deterministic transcription and file
conversion. Calls are synchronous: wait for the result before taking the next
action.

## Tool selection

- Use `forge_transcribe` for one audio or video file. Supply its absolute path,
  an explicit output root, and the recording type. The result contains raw
  recognition and dictionary corrections, not a model-based cleanup pass.
- Use `forge_convert_files` for supported conversions. Supply absolute input
  paths, the target format, and an explicit output root. EPUB metadata and cover
  fields apply only to Markdown-to-EPUB conversion.

Ask the user for required information before calling a tool. Do not guess a
recording type, conversion target, output root, cover path, or book metadata.

## Result handling

1. Treat `status`, not prose or file existence, as the completion signal.
2. Report `runDirectory`, relevant artifact paths, counts, and all warnings.
3. For `needs_review`, explain what requires review and keep the artifacts.
4. For `failed`, report `error.code`, `error.message`, and each remediation item.
5. For `canceled`, do not claim the operation completed.
6. Never retry automatically unless `error.retryable` is true; even then, ask
   before retrying.
7. Never modify source files or interpret partial artifacts as complete.

`dependency_not_ready` means transcription setup must be run explicitly outside
the delegated request. Do not install the runtime or download the model through
the MCP bridge.
