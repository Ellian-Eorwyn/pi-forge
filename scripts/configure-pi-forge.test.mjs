import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));

test("profile configuration exposes code and non-thinking chat models", () => {
	const workspace = mkdtempSync(join(tmpdir(), "pi-forge-models-"));
	const agentDirectory = join(workspace, "agent");
	mkdirSync(agentDirectory);

	try {
		const result = spawnSync(
			process.execPath,
			[
				join(repositoryRoot, "scripts", "configure-pi-forge.mjs"),
				agentDirectory,
				join(repositoryRoot, "forge"),
			],
			{ encoding: "utf8" },
		);
		assert.equal(result.status, 0, result.stderr);

		const settings = JSON.parse(readFileSync(join(agentDirectory, "settings.json"), "utf8"));
		assert.equal(settings.defaultProvider, "forge-local");
		assert.equal(settings.defaultModel, "code");

		const { providers } = JSON.parse(readFileSync(join(agentDirectory, "models.json"), "utf8"));
		assert.equal(providers["forge-local"].baseUrl, "http://llms:8008/v1");
		assert.equal(providers["forge-local"].models[0].id, "code");
		assert.equal(providers["forge-chat-local"].baseUrl, "http://llms:8004/v1");
		assert.equal(providers["forge-chat-local"].models[0].id, "chat");
		assert.equal(providers["forge-chat-local"].models[0].reasoning, false);
		assert.equal(providers["forge-chat-local"].compat.supportsReasoningEffort, false);
		assert.equal("thinkingFormat" in providers["forge-chat-local"].compat, false);
	} finally {
		rmSync(workspace, { recursive: true, force: true });
	}
});
