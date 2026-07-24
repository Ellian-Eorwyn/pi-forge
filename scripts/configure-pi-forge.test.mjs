import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));

function configure(agentDirectory) {
	const result = spawnSync(
		process.execPath,
		[join(repositoryRoot, "scripts", "configure-pi-forge.mjs"), agentDirectory, join(repositoryRoot, "forge")],
		{ encoding: "utf8" },
	);
	assert.equal(result.status, 0, result.stderr);
	return JSON.parse(readFileSync(join(agentDirectory, "settings.json"), "utf8"));
}

function withAgentDirectory(existingSettings, body) {
	const workspace = mkdtempSync(join(tmpdir(), "pi-forge-models-"));
	const agentDirectory = join(workspace, "agent");
	mkdirSync(agentDirectory);
	if (existingSettings) {
		writeFileSync(join(agentDirectory, "settings.json"), JSON.stringify(existingSettings, undefined, "\t"));
	}
	try {
		body(agentDirectory);
	} finally {
		rmSync(workspace, { recursive: true, force: true });
	}
}

test("profile configuration exposes code and non-thinking chat models", () => {
	withAgentDirectory(undefined, (agentDirectory) => {
		const settings = configure(agentDirectory);
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
	});
});

test("bulk chat work defaults to the non-thinking backend and think to the thinking one", () => {
	withAgentDirectory(undefined, (agentDirectory) => {
		const { chat, think } = configure(agentDirectory).connectedServices;
		assert.equal(chat.baseUrl, "http://llms:8004/v1/chat/completions");
		assert.equal(chat.model, "chat");
		assert.equal(chat.scheduling.enabled, false);
		assert.equal(think.baseUrl, "http://llms:8008/v1/chat/completions");
		assert.equal(think.model, "code");
		assert.equal(think.enabled, true);
		// Verification runs against the interactive agent's own server, so it
		// pins the background slot rather than evicting the session prefix.
		assert.equal(think.scheduling.enabled, true);
		assert.equal(think.scheduling.interactiveSlot, 0);
		assert.equal(think.scheduling.backgroundSlot, 1);
	});
});

test("an install still on the pre-split chat default is migrated to the non-thinking backend", () => {
	const legacy = {
		connectedServices: {
			chat: {
				enabled: true,
				baseUrl: "http://llms:8008/v1/chat/completions",
				model: "code",
				scheduling: { enabled: true, interactiveSlot: 0, backgroundSlot: 3, idleGraceMs: 2000, yieldMs: 1000, backgroundOutputTokens: 4096 },
			},
		},
	};
	withAgentDirectory(legacy, (agentDirectory) => {
		const { chat, think } = configure(agentDirectory).connectedServices;
		assert.equal(chat.baseUrl, "http://llms:8004/v1/chat/completions");
		assert.equal(chat.model, "chat");
		// Migration replaces the endpoint that an older install wrote, not the
		// scheduling the user tuned.
		assert.equal(chat.scheduling.backgroundSlot, 3);
		assert.equal(think.baseUrl, "http://llms:8008/v1/chat/completions");
	});
});

test("a customized chat endpoint survives configuration", () => {
	const customized = {
		connectedServices: {
			chat: { enabled: true, baseUrl: "http://elsewhere:9000/v1/chat/completions", model: "code" },
		},
	};
	withAgentDirectory(customized, (agentDirectory) => {
		const { chat } = configure(agentDirectory).connectedServices;
		assert.equal(chat.baseUrl, "http://elsewhere:9000/v1/chat/completions");
		assert.equal(chat.model, "code");
	});
});
