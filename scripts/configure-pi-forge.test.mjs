import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));

function configure(agentDirectory, extraEnv = {}) {
	const result = spawnSync(
		process.execPath,
		[join(repositoryRoot, "scripts", "configure-pi-forge.mjs"), agentDirectory, join(repositoryRoot, "forge")],
		{
			encoding: "utf8",
			env: {
				...process.env,
				// These tests cover settings and models seeding. Left enabled, the
				// optional Moshi step would invoke the host's real moshi-hook binary
				// and restart its daemon as a side effect of running the suite.
				PI_FORGE_SKIP_MOSHI_HOOK: "1",
				// Capacity discovery reads the deployment's state API. On a developer
				// machine that host is up, which would make these assertions depend on
				// what it happens to be serving; in CI it is not, which would spend a
				// timeout per run. Tests that want discovery opt in via extraEnv.
				PI_FORGE_SKIP_STACK_DISCOVERY: "1",
				...extraEnv,
			},
		},
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
		// :8004 and :8008 are two profiles in front of one llama-server, so bulk
		// work has to pin its slot here too or it can evict slot 0.
		assert.equal(chat.scheduling.enabled, true);
		assert.equal(chat.scheduling.backgroundSlot, 1);
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
				scheduling: {
					enabled: true,
					interactiveSlot: 0,
					backgroundSlot: 3,
					idleGraceMs: 2000,
					yieldMs: 1000,
					backgroundOutputTokens: 4096,
				},
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

test("chat scheduling seeded off by an older install is turned on", () => {
	const legacy = {
		connectedServices: {
			chat: {
				enabled: true,
				scheduling: {
					enabled: false,
					interactiveSlot: 0,
					backgroundSlot: 1,
					idleGraceMs: 2000,
					yieldMs: 1000,
					backgroundOutputTokens: 4096,
				},
			},
		},
	};
	withAgentDirectory(legacy, (agentDirectory) => {
		const { chat } = configure(agentDirectory).connectedServices;
		assert.equal(chat.scheduling.enabled, true);
		assert.equal(chat.scheduling.backgroundSlot, 1);
	});
});

test("a deliberate chat scheduling opt-out survives configuration", () => {
	const tuned = {
		connectedServices: {
			chat: {
				enabled: true,
				// Byte-different from the old default, so it was chosen rather than seeded.
				scheduling: {
					enabled: false,
					interactiveSlot: 0,
					backgroundSlot: 2,
					idleGraceMs: 2000,
					yieldMs: 1000,
					backgroundOutputTokens: 4096,
				},
			},
		},
	};
	withAgentDirectory(tuned, (agentDirectory) => {
		const { chat } = configure(agentDirectory).connectedServices;
		assert.equal(chat.scheduling.enabled, false);
		assert.equal(chat.scheduling.backgroundSlot, 2);
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

test("an install with no provider keys seeds an empty block", () => {
	withAgentDirectory(undefined, (agentDirectory) => {
		// The no-key provider tier has to work on a machine nobody has configured.
		assert.deepEqual(configure(agentDirectory).connectedServices.apiKeys, {});
	});
});

test("persisted provider keys survive configuration, half-finished ones are dropped", () => {
	const existing = {
		connectedServices: {
			apiKeys: { openalex: "live-key", core: "  spaced  ", guardian: "", nyt: null, wolfram: 42 },
		},
	};
	withAgentDirectory(existing, (agentDirectory) => {
		const { apiKeys } = configure(agentDirectory).connectedServices;
		assert.equal(apiKeys.openalex, "live-key");
		assert.equal(apiKeys.core, "spaced");
		// An empty, null or non-string value is a half-finished edit. Keeping it
		// would make the provider send a malformed credential and read the 401 as
		// the provider refusing us rather than as our own bad config.
		assert.equal("guardian" in apiKeys, false);
		assert.equal("nyt" in apiKeys, false);
		assert.equal("wolfram" in apiKeys, false);
	});
});

test("provider keys resolve explicit over env over persisted", async () => {
	const { apiKeyEnvName, resolveApiKey, resolveConnectedServices } = await import(
		join(repositoryRoot, "forge", "lib", "connected-services.mjs")
	);
	assert.equal(apiKeyEnvName("semantic-scholar"), "FORGE_API_KEY_SEMANTIC_SCHOLAR");

	const settings = { connectedServices: { apiKeys: { openalex: "from-settings", core: "from-settings" } } };
	const persisted = resolveConnectedServices({ settings, env: {} });
	assert.equal(persisted.apiKeys.openalex, "from-settings");

	const env = { FORGE_API_KEY_OPENALEX: "from-env", FORGE_API_KEY_SEMANTIC_SCHOLAR: "env-only" };
	const withEnv = resolveConnectedServices({ settings, env });
	assert.equal(withEnv.apiKeys.openalex, "from-env");
	// An env var alone configures a provider that settings.json never mentioned.
	assert.equal(withEnv.apiKeys["semantic-scholar"], "env-only");
	assert.equal(withEnv.apiKeys.core, "from-settings");

	const explicit = resolveConnectedServices({ settings, env, apiKeys: { openalex: "from-option" } });
	assert.equal(explicit.apiKeys.openalex, "from-option");

	// An empty env var turns a persisted key off for this process, matching how
	// FORGE_SEARXNG_URL="" disables search.
	const disabled = resolveConnectedServices({ settings, env: { FORGE_API_KEY_OPENALEX: "" } });
	assert.equal("openalex" in disabled.apiKeys, false);

	assert.equal(resolveApiKey("core", { settings, env: {} }), "from-settings");
	// A provider with no key is absent, not an error: the router skips it.
	assert.equal(resolveApiKey("guardian", { settings, env: {} }), null);
});

function installedProfile(agentDirectory) {
	return readFileSync(join(agentDirectory, "AGENTS.md"), "utf8");
}

test("a declared forgeUser is rendered into the installed profile", () => {
	withAgentDirectory({ forgeUser: { name: "Ellie", pronouns: "they/them" } }, (agentDirectory) => {
		const settings = configure(agentDirectory);
		const profile = installedProfile(agentDirectory);
		assert.match(profile, /## Who You Are Working With/);
		assert.match(profile, /You are working with Ellie \(they\/them\)\./);
		assert.match(profile, /Address them by name/);
		// The setting itself is preserved, or the next update would lose the name.
		assert.deepEqual(settings.forgeUser, { name: "Ellie", pronouns: "they/them" });
	});
});

test("configuring twice regenerates the identity block rather than stacking it", () => {
	withAgentDirectory({ forgeUser: { name: "Ellie" } }, (agentDirectory) => {
		configure(agentDirectory);
		configure(agentDirectory);
		const profile = installedProfile(agentDirectory);
		assert.equal(profile.match(/## Who You Are Working With/g)?.length, 1);
		assert.match(profile, /You are working with Ellie\. Address them by name/);
	});
});

test("an install with no forgeUser gets the packaged profile unchanged", () => {
	withAgentDirectory(undefined, (agentDirectory) => {
		configure(agentDirectory);
		const profile = installedProfile(agentDirectory);
		assert.doesNotMatch(profile, /Who You Are Working With/);
		assert.equal(profile, readFileSync(join(repositoryRoot, "forge", "AGENTS.md"), "utf8"));
	});
});

test("an unusable forgeUser is ignored rather than half-rendered", () => {
	for (const forgeUser of [{ pronouns: "they/them" }, { name: "   " }, { name: "x".repeat(41) }, "Ellie", []]) {
		withAgentDirectory({ forgeUser }, (agentDirectory) => {
			configure(agentDirectory);
			assert.doesNotMatch(installedProfile(agentDirectory), /Who You Are Working With/);
		});
	}
});

/** A vault is a directory that exists; `.obsidian/` is not required, because the vault skills do not require it. */
function withVault(body, { inbox = true } = {}) {
	const root = mkdtempSync(join(tmpdir(), "pi-forge-vault-"));
	if (inbox) mkdirSync(join(root, "00 Inbox"));
	try {
		body(root);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
}

test("a declared forgeVault is rendered into the installed profile with its inbox", () => {
	withVault((vault) => {
		withAgentDirectory({ forgeVault: vault }, (agentDirectory) => {
			const settings = configure(agentDirectory);
			const profile = installedProfile(agentDirectory);
			assert.match(profile, /## Your Vault/);
			assert.ok(profile.includes(`- Vault root: ${vault}`));
			assert.ok(profile.includes(`- Inbox: ${join(vault, "00 Inbox")}`));
			// The setting itself is preserved, or the next update would lose the path.
			assert.equal(settings.forgeVault, vault);
		});
	});
});

test("configuring twice regenerates the vault block rather than stacking it", () => {
	withVault((vault) => {
		withAgentDirectory({ forgeVault: vault }, (agentDirectory) => {
			configure(agentDirectory);
			configure(agentDirectory);
			assert.equal(installedProfile(agentDirectory).match(/## Your Vault/g)?.length, 1);
		});
	});
});

test("a forgeVault whose inbox does not exist yet is still declared", () => {
	withVault(
		(vault) => {
			withAgentDirectory({ forgeVault: vault }, (agentDirectory) => {
				configure(agentDirectory);
				assert.match(installedProfile(agentDirectory), /## Your Vault/);
			});
		},
		{ inbox: false },
	);
});

test("a forgeVault that is not a usable absolute directory is ignored rather than declared", () => {
	for (const forgeVault of ["", "   ", "relative/vault", "~", join(tmpdir(), "pi-forge-absent-vault"), 7, ["/tmp"]]) {
		withAgentDirectory({ forgeVault }, (agentDirectory) => {
			configure(agentDirectory);
			assert.doesNotMatch(installedProfile(agentDirectory), /Your Vault/);
		});
	}
});

test("a forgeVault written with a leading ~ is expanded to the real home path", () => {
	// `~/Documents/…` is how a person writes this by hand, and nothing expands it
	// for us: the shell never sees the value, and `resolve` treats it as a literal.
	const root = mkdtempSync(join(homedir(), ".pi-forge-vault-test-"));
	try {
		mkdirSync(join(root, "00 Inbox"));
		withAgentDirectory({ forgeVault: `~/${basename(root)}` }, (agentDirectory) => {
			configure(agentDirectory);
			assert.ok(installedProfile(agentDirectory).includes(`- Vault root: ${root}`));
		});
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("a forgeVault pointing at a file is ignored rather than declared", () => {
	withVault((vault) => {
		const file = join(vault, "note.md");
		writeFileSync(file, "not a vault\n");
		withAgentDirectory({ forgeVault: file }, (agentDirectory) => {
			configure(agentDirectory);
			assert.doesNotMatch(installedProfile(agentDirectory), /Your Vault/);
		});
	});
});

test("identity and vault blocks coexist without either swallowing the other", () => {
	withVault((vault) => {
		withAgentDirectory(
			{ forgeUser: { name: "Ellie", pronouns: "they/them" }, forgeVault: vault },
			(agentDirectory) => {
				configure(agentDirectory);
				const profile = installedProfile(agentDirectory);
				assert.match(profile, /## Who You Are Working With/);
				assert.match(profile, /## Your Vault/);
				assert.ok(profile.indexOf("## Who You Are Working With") < profile.indexOf("## Your Vault"));
			},
		);
	});
});
