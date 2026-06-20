import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
	chmodSync,
	copyFileSync,
	mkdtempSync,
	mkdirSync,
	readFileSync,
	realpathSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const repositoryRoot = resolve(import.meta.dirname, "..");
const skillsRoot = join(repositoryRoot, "forge", "skills");
const python = process.env.PYTHON ?? "python3";
const placeholder = "<!-- TODO: author this section -->";
const environment = {
	...process.env,
	FORGE_SEARXNG_URL: "",
	PYTHONDONTWRITEBYTECODE: "1",
};

function script(skill, name) {
	return join(skillsRoot, skill, "scripts", name);
}

function run(command, args, expectedStatus = 0) {
	const result = spawnSync(command, args, {
		cwd: repositoryRoot,
		encoding: "utf8",
		env: environment,
		maxBuffer: 64 * 1024 * 1024,
	});
	assert.equal(
		result.status,
		expectedStatus,
		`${command} ${args.join(" ")} exited ${result.status}\nstdout:\n${result.stdout}\nstderr:\n${result.stderr}`,
	);
	return result;
}

function runFailure(command, args, expectedText) {
	const result = spawnSync(command, args, {
		cwd: repositoryRoot,
		encoding: "utf8",
		env: environment,
		maxBuffer: 64 * 1024 * 1024,
	});
	assert.notEqual(result.status, 0, `${command} ${args.join(" ")} unexpectedly succeeded`);
	assert.match(`${result.stdout}\n${result.stderr}`, expectedText);
	return result;
}

function jsonOutput(result) {
	return JSON.parse(result.stdout);
}

function sha256(path) {
	return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function authorFiles(directory, names) {
	for (const name of names) {
		const path = join(directory, name);
		const current = readFileSync(path, "utf8");
		assert.match(current, /<!-- TODO: author this section -->/);
		writeFileSync(path, current.replace(placeholder, "Authored from the staged source evidence."));
	}
}

function withWorkspace(callback) {
	const workspace = mkdtempSync(join(tmpdir(), "pi-forge-skills-"));
	try {
		callback(workspace);
	} finally {
		rmSync(workspace, { recursive: true, force: true });
	}
}

test("transcript cleanup and file conversion preserve their source", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "transcript.txt");
		const original = "Speaker 1: Um, send the draft Friday.  \r\n\r\n\r\nSpeaker 2: Agreed.\r\n";
		writeFileSync(source, original);
		const sourceHash = sha256(source);

		const extracted = join(workspace, "extracted.md");
		const transcriptResult = jsonOutput(
			run("node", [script("transcript-cleanup", "extract-transcript.mjs"), source, extracted]),
		);
		assert.equal(transcriptResult.sha256, sourceHash);
		assert.equal(sha256(source), sourceHash);
		assert.match(readFileSync(extracted, "utf8"), /send the draft Friday/);

		const conversionRun = join(workspace, "conversion");
		const conversion = jsonOutput(
			run(python, [script("file-conversion", "file-conversion.py"), "convert", source, "--to", "txt", "--output", conversionRun]),
		);
		assert.equal(conversion.success, 1);
		run(python, [script("file-conversion", "file-conversion.py"), "validate", conversionRun]);
		assert.equal(sha256(source), sourceHash);
		runFailure(
			python,
			[script("file-conversion", "file-conversion.py"), "convert", source, "--to", "txt", "--output", conversionRun],
			/output already exists/,
		);

		writeFileSync(source, `${original}changed\n`);
		runFailure(
			python,
			[script("file-conversion", "file-conversion.py"), "validate", conversionRun],
			/source file hash differs/,
		);
	});
});

test("literature extraction rejects scaffolds and accepts authored deliverables", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "study.md");
		writeFileSync(source, "# Study\n\nThe study reports a 12 percent increase.\n");
		const sourceHash = sha256(source);
		const runDirectory = join(workspace, "literature");
		run(python, [script("literature-extraction", "literature-extraction.py"), "init", source, "--output", runDirectory]);
		const pending = jsonOutput(
			run(python, [script("literature-extraction", "literature-extraction.py"), "next", runDirectory]),
		);
		const extractionPath = join(runDirectory, "working", "items.json");
		writeFileSync(
			extractionPath,
			`${JSON.stringify([
				{
					item_type: "finding",
					text: "The study reports a 12 percent increase.",
					evidence_quote: "The study reports a 12 percent increase.",
					locator: "Study",
					interpretation: "explicit",
					confidence: "high",
					notes: null,
				},
			])}\n`,
		);
		run(python, [
			script("literature-extraction", "literature-extraction.py"),
			"record",
			runDirectory,
			"--doc-id",
			pending.documentId,
			"--extraction-file",
			extractionPath,
		]);
		run(python, [script("literature-extraction", "literature-extraction.py"), "build", runDirectory]);
		runFailure(
			python,
			[script("literature-extraction", "literature-extraction.py"), "validate", runDirectory],
			/unresolved placeholder/,
		);
		authorFiles(runDirectory, ["literature_summary.md", "claims_matrix.md", "research_gaps.md", "citation_notes.md"]);
		const validation = jsonOutput(
			run(python, [script("literature-extraction", "literature-extraction.py"), "validate", runDirectory]),
		);
		assert.equal(validation.valid, true);
		assert.equal(sha256(source), sourceHash);
		runFailure(
			python,
			[script("literature-extraction", "literature-extraction.py"), "init", source, "--output", runDirectory],
			/output already exists/,
		);
		writeFileSync(source, "# Study\n\nChanged after initialization.\n");
		runFailure(
			python,
			[script("literature-extraction", "literature-extraction.py"), "validate", runDirectory],
			/source file hash differs/,
		);
	});
});

test("personal admin and report output enforce authored deliverables", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "notice.txt");
		writeFileSync(source, "Invoice 123 is due on 2026-07-01. Call 555-0100.\n");
		const adminRun = join(workspace, "admin");
		run(python, [script("personal-admin", "personal-admin.py"), "init", source, "--output", adminRun]);
		const pending = jsonOutput(run(python, [script("personal-admin", "personal-admin.py"), "next", adminRun]));
		const factsPath = join(adminRun, "working", "facts.json");
		writeFileSync(
			factsPath,
			`${JSON.stringify([
				{
					fact_type: "deadline",
					text: "Invoice 123 is due on 2026-07-01.",
					value: "123",
					due_date: "2026-07-01",
					locator: "line 1",
					confidence: "high",
					notes: null,
				},
			])}\n`,
		);
		run(python, [
			script("personal-admin", "personal-admin.py"),
			"record",
			adminRun,
			"--doc-id",
			pending.documentId,
			"--facts-file",
			factsPath,
		]);
		run(python, [script("personal-admin", "personal-admin.py"), "build", adminRun]);
		runFailure(python, [script("personal-admin", "personal-admin.py"), "validate", adminRun], /unresolved placeholder/);
		authorFiles(adminRun, ["admin_summary.md", "next_steps.md"]);
		assert.equal(jsonOutput(run(python, [script("personal-admin", "personal-admin.py"), "validate", adminRun])).valid, true);

		const reportRun = join(workspace, "report");
		run(python, [
			script("report-output", "report-output.py"),
			"init",
			join(adminRun, "extracted_facts.csv"),
			"--output",
			reportRun,
			"--detail",
			"brief",
		]);
		runFailure(python, [script("report-output", "report-output.py"), "validate", reportRun], /unresolved placeholder/);
		authorFiles(reportRun, ["executive_summary.md", "assumptions_and_limits.md"]);
		assert.equal(jsonOutput(run(python, [script("report-output", "report-output.py"), "validate", reportRun])).valid, true);
	});
});

test("spreadsheet inspection and row enrichment are reproducible", () => {
	withWorkspace((workspace) => {
		const source = join(workspace, "data.csv");
		writeFileSync(source, "name,amount\nalpha,10\nbeta,20\n");
		const sourceHash = sha256(source);
		const profileRun = join(workspace, "profile");
		run(python, [script("spreadsheet-analysis", "spreadsheet-analysis.py"), "inspect", source, "--output", profileRun]);
		assert.match(readFileSync(join(profileRun, "transform_log.md"), "utf8"), /No data transformations were performed/);

		const rowRun = join(workspace, "rows");
		run(python, [
			script("spreadsheet-analysis", "spreadsheet-analysis.py"),
			"row-init",
			source,
			"--output",
			rowRun,
			"--column",
			"Review",
		]);
		for (const value of ["reviewed alpha", "reviewed beta"]) {
			const pending = jsonOutput(run(python, [script("spreadsheet-analysis", "spreadsheet-analysis.py"), "row-next", rowRun]));
			const valuePath = join(workspace, `value-${pending.rowId}.txt`);
			writeFileSync(valuePath, `${value}\n`);
			run(python, [
				script("spreadsheet-analysis", "spreadsheet-analysis.py"),
				"row-record",
				rowRun,
				"--row-id",
				String(pending.rowId),
				"--value-file",
				valuePath,
			]);
		}
		run(python, [script("spreadsheet-analysis", "spreadsheet-analysis.py"), "row-finalize", rowRun]);
		assert.equal(jsonOutput(run(python, [script("spreadsheet-analysis", "spreadsheet-analysis.py"), "validate", rowRun])).valid, true);
		assert.equal(sha256(source), sourceHash);
		runFailure(
			python,
			[script("spreadsheet-analysis", "spreadsheet-analysis.py"), "row-finalize", rowRun],
			/output already exists/,
		);
	});
});

test("document ingest, coding, and web collection expose review and safety boundaries", () => {
	withWorkspace((workspace) => {
		const document = join(workspace, "document.md");
		writeFileSync(document, "# Source document\n\nSource-backed content.\n");
		const documentHash = sha256(document);
		const ingestRun = join(workspace, "ingest");
		const ingest = jsonOutput(
			run("node", [script("document-ingest", "document-ingest.mjs"), "prepare", document, "--output", ingestRun]),
		);
		assert.equal(ingest.documents, 1);
		runFailure("node", [script("document-ingest", "document-ingest.mjs"), "validate", ingestRun], /model review is not marked complete/);
		assert.equal(sha256(document), documentHash);

		const repository = join(workspace, "repository");
		mkdirSync(repository);
		writeFileSync(join(repository, "package.json"), '{"scripts":{"check":"node --check index.js"}}\n');
		writeFileSync(join(repository, "index.js"), "console.log('ok');\n");
		const codingRun = join(workspace, "coding");
		run("node", [script("coding", "coding.mjs"), "inspect", repository, "--output", codingRun]);
		runFailure("node", [script("coding", "coding.mjs"), "validate", codingRun], /missing required artifact/);
		writeFileSync(
			join(codingRun, "change_summary.md"),
			"# Change Summary\n\n## Summary\nNone.\n\n## Motivation\nSmoke test.\n\n## Files changed\nNone.\n\n## Verification\nLocal.\n\n## Follow-ups & uncertainties\nNone.\n",
		);
		writeFileSync(join(codingRun, "run_log.md"), "# Run Log\n\n- Inspected repository.\n");
		assert.equal(jsonOutput(run("node", [script("coding", "coding.mjs"), "validate", codingRun])).valid, true);

		const doctor = jsonOutput(run("node", [script("web-collection", "web-collection.mjs"), "doctor", "--json"]));
		assert.equal(doctor.capabilities.httpCollect, true);
		assert.equal(doctor.capabilities.searxngSearch, false);
		assert.match(doctor.searxng.detail, /FORGE_SEARXNG_URL is not set/);
		const rejectedWebRun = join(workspace, "web-rejected");
		const rejected = jsonOutput(
			run("node", [
				script("web-collection", "web-collection.mjs"),
				"collect",
				"http://127.0.0.1/test",
				"--output",
				rejectedWebRun,
			]),
		);
		assert.equal(rejected.counts.failed, 1);
		assert.match(readFileSync(join(rejectedWebRun, "failed_downloads.csv"), "utf8"), /refused loopback or metadata host/);
		assert.equal(
			jsonOutput(run("node", [script("web-collection", "web-collection.mjs"), "validate", rejectedWebRun])).valid,
			true,
		);

		const webRun = join(workspace, "web-validation");
		mkdirSync(webRun);
		writeFileSync(
			join(webRun, "web_manifest.csv"),
			"resource_id,source_url,final_url,access_date,status,http_status,content_type,title,filename,output_path,sha256,byte_size,capture_method,rendered,duplicate_of,error\n",
		);
		writeFileSync(join(webRun, "web_manifest.json"), '{"schemaVersion":1,"resources":[]}\n');
		writeFileSync(join(webRun, "failed_downloads.csv"), "source_url,status,http_status,reason,access_date\n");
		writeFileSync(
			join(webRun, "collection_report.md"),
			"# Collection Report\n\n## Status\n\n## Run Summary\n\n## Sources\n\n## Captures\n\n## Duplicates\n\n## Failures and Blocks\n\n## Search\n\n## Review\n",
		);
		assert.equal(jsonOutput(run("node", [script("web-collection", "web-collection.mjs"), "validate", webRun])).valid, true);
	});
});

test("installed launcher symlinks resolve their source checkout", () => {
	withWorkspace((workspace) => {
		const repository = join(workspace, "repository");
		const scriptsDirectory = join(repository, "scripts");
		const binaryDirectory = join(workspace, "bin");
		const fakeBin = join(workspace, "fake-bin");
		mkdirSync(scriptsDirectory, { recursive: true });
		mkdirSync(join(repository, "packages", "coding-agent", "dist"), { recursive: true });
		mkdirSync(binaryDirectory);
		mkdirSync(fakeBin);
		copyFileSync(join(repositoryRoot, "scripts", "pi-forge-run.sh"), join(scriptsDirectory, "pi-forge-run.sh"));
		copyFileSync(join(repositoryRoot, "update.sh"), join(repository, "update.sh"));
		writeFileSync(join(repository, "packages", "coding-agent", "dist", "cli.js"), "");
		writeFileSync(join(fakeBin, "node"), "#!/usr/bin/env bash\nprintf '%s\\n' \"$FORGE_SEARXNG_URL\" \"$@\"\n");
		chmodSync(join(fakeBin, "node"), 0o755);
		symlinkSync(join(scriptsDirectory, "pi-forge-run.sh"), join(binaryDirectory, "pi-forge"));
		symlinkSync(join(repository, "update.sh"), join(binaryDirectory, "pi-forge-update"));

		const launcher = spawnSync(join(binaryDirectory, "pi-forge"), ["--version"], {
			encoding: "utf8",
			env: { ...environment, PATH: `${fakeBin}:${process.env.PATH}` },
		});
		assert.equal(launcher.status, 0, launcher.stderr);
		assert.equal(
			launcher.stdout.trim(),
			`http://llms/searxng\n${join(realpathSync(repository), "packages", "coding-agent", "dist", "cli.js")}\n--version`,
		);

		const updater = runFailure(join(binaryDirectory, "pi-forge-update"), [], /pi-forge update requires a git checkout/);
		assert.match(updater.stderr, new RegExp(realpathSync(repository).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
	});
});

test("profile configuration installs local service defaults without dropping user configuration", () => {
	withWorkspace((workspace) => {
		const agentDirectory = join(workspace, "agent");
		mkdirSync(agentDirectory);
		writeFileSync(
			join(agentDirectory, "settings.json"),
			`${JSON.stringify({ theme: "light", packages: ["/user/package"] })}\n`,
		);
		writeFileSync(
			join(agentDirectory, "models.json"),
			`${JSON.stringify({ providers: { existing: { baseUrl: "https://example.invalid/v1" } } })}\n`,
		);
		run("node", [join(repositoryRoot, "scripts", "configure-pi-forge.mjs"), agentDirectory, join(repositoryRoot, "forge")]);

		const settings = JSON.parse(readFileSync(join(agentDirectory, "settings.json"), "utf8"));
		assert.equal(settings.defaultProvider, "forge-local");
		assert.equal(settings.defaultModel, "code");
		assert.equal(settings.theme, "light");
		assert.deepEqual(settings.packages, [join(repositoryRoot, "forge"), "/user/package"]);

		const models = JSON.parse(readFileSync(join(agentDirectory, "models.json"), "utf8"));
		assert.equal(models.providers.existing.baseUrl, "https://example.invalid/v1");
		assert.equal(models.providers["forge-local"].baseUrl, "http://llms:8008/v1");
		assert.equal(models.providers["forge-local"].models[0].id, "code");
		assert.equal(models.providers["forge-local"].compat.supportsDeveloperRole, false);
	});
});

test("piped installer ignores the caller's checkout and uses the configured repository", () => {
	withWorkspace((workspace) => {
		const fakeBin = join(workspace, "bin");
		const installDirectory = join(workspace, "install");
		const gitLog = join(workspace, "git-args.txt");
		mkdirSync(fakeBin);
		writeFileSync(
			join(fakeBin, "git"),
			"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$INSTALLER_GIT_LOG\"\nexit 23\n",
		);
		chmodSync(join(fakeBin, "git"), 0o755);

		const result = spawnSync("bash", [], {
			cwd: repositoryRoot,
			encoding: "utf8",
			env: {
				...environment,
				HOME: workspace,
				INSTALLER_GIT_LOG: gitLog,
				PATH: `${fakeBin}:${process.env.PATH}`,
				PI_FORGE_INSTALL_DIR: installDirectory,
				PI_FORGE_REPOSITORY: "https://example.invalid/pi-forge.git",
			},
			input: readFileSync(join(repositoryRoot, "install.sh"), "utf8"),
		});
		assert.equal(result.status, 23);
		assert.doesNotMatch(result.stderr, /BASH_SOURCE/);
		assert.equal(
			readFileSync(gitLog, "utf8"),
			`clone\nhttps://example.invalid/pi-forge.git\n${join(installDirectory, "repository")}\n`,
		);

		const packageJson = JSON.parse(readFileSync(join(repositoryRoot, "package.json"), "utf8"));
		assert.equal(typeof packageJson.scripts["build:install"], "string");
		assert.match(readFileSync(join(repositoryRoot, "scripts", "pi-forge-install.sh"), "utf8"), /run build:install/);
	});
});
