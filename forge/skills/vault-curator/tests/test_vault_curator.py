#!/usr/bin/env python3
"""End-to-end tests for researched schema proposals, with no network.

The whole run is exercised against a synthetic vault, a stubbed chat/think
endpoint, and a recorded claim register standing in for a `web-research deep`
run. That combination is the point: the pipeline's value is that a weak model
cannot get a bad row past the deterministic gate, and the only way to prove that
is to hand it a bad row.

The insertion primitives themselves are proven in
`forge/lib/tests/test_schema_insert.py`. What is proven here is the skill: that
research with no citation is dropped, that a colliding row is held rather than
proposed, that a thin route is demoted, and that `apply` refuses anything but an
additive write.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills" / "vault-curator" / "scripts" / "vault-curator.py"
sys.path.insert(0, str(ROOT / "lib"))
import vault_schema as vs  # noqa: E402

_spec = importlib.util.spec_from_file_location("vault_curator", SCRIPT)
curator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(curator)


SCHEMA = """---
type: system
status: active
domain: meta
subdomain: schemas
---
# Vault Schema

## Approved properties

| Property | Required | Shape | Definition |
| --- | --- | --- | --- |
| `type` | yes | controlled scalar | What kind of note this is. |
| `status` | yes | controlled scalar | Lifecycle state. |
| `domain` | yes | controlled scalar | Broad area. |
| `subdomain` | no | controlled scalar | Nested area. |
| `parent` | no | quoted wikilink | Nearest durable topic. |
| `related` | no | list of quoted wikilinks | Cross-cutting links. |
| `date` | no | scalar, human-owned | The date the note is about. |

## Note types

- `note` — A general knowledge note.
- `concept` — A named idea other notes can link to.
- `place` — A geographic or physical location.
- `index` — A dashboard or hub.
- `system` — Vault infrastructure.

## Status values

- `raw` — Captured but unfiled.
- `active` — Currently relevant.

## Domains

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `craft` | `2` | `Craft` | Making things. |
| `wiki` | `9` | `Wiki` | Reference cards other notes link to. |
| `meta` | `99` | `Meta` | Vault infrastructure. |

## Subdomains

### craft

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `gardening` | `1` | `Gardening` | Plants and growing. |

### wiki

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `concepts` | `1` | `Concepts` | Named ideas. |
| `places` | `3` | `Places` | Geographic locations. |

### meta

| Value | Number | Label | Definition |
| --- | --- | --- | --- |
| `schemas` | `2` | `Schemas` | Controlled vocabularies. |

## Project registry

| Approved value | Domain | Subdomain | Number | Definition |
| --- | --- | --- | --- | --- |
| `"[[Greenhouse]]"` | `craft` | `gardening` | `1` | Building the greenhouse. |

## Source kinds

- `book` — A book.

## Capture types

- `manual` — Typed by hand.

## Legacy normalization map

| Legacy input | Canonical output |
| --- | --- |
| `type: daily` | `type: note` |

## Folder routing

### Derived names

```text
domain-folder(domain):
  <pad2(domain.number)> <domain.label>
```
"""

CLAIMS = [
    {"id": "c-001", "claim": "Mineral species names are governed by the IMA Commission on New "
                             "Minerals, Nomenclature and Classification, which approves every new name.",
     "sourceIds": ["s-1"], "confidence": "high"},
    {"id": "c-002", "claim": "A specimen label records the locality at which the specimen was "
                             "collected, and collections treat locality as the primary provenance field.",
     "sourceIds": ["s-1", "s-2"], "confidence": "high"},
    {"id": "c-003", "claim": "Collectors record each acquisition as a dated event with a source, "
                             "so a single specimen accumulates a chain of acquisition records over time.",
     "sourceIds": ["s-2"], "confidence": "medium"},
    {"id": "c-004", "claim": "Hardness is reported on the Mohs scale and specimen dimensions in "
                             "millimetres.", "sourceIds": ["s-3"], "confidence": "high"},
]


def practice_for(dimension):
    table = {
        "identity": ("Mineral species names are approved by the IMA-CNMNC, which is the naming "
                     "authority.", ["c-001"], "IMA-CNMNC"),
        "provenance": ("Collections record the collecting locality as the primary provenance "
                       "field on the specimen label.", ["c-002"], ""),
        "record_split": ("An acquisition is recorded as a dated event separate from the specimen "
                         "record, and they accumulate.", ["c-003"], ""),
        "measurement": ("Hardness is reported on the Mohs scale and dimensions in millimetres.",
                        ["c-004"], "Mohs"),
    }
    return table.get(dimension)


class StubHandler(BaseHTTPRequestHandler):
    """Answers each stage plausibly, keyed off its system prompt.

    Scripted responses take priority, so a test can inject one bad answer for a
    single stage and let everything else behave.
    """

    requests = []
    scripted = {}

    def stage_of(self, payload):
        system = payload["messages"][0]["content"]
        if "verdicts" in system:
            return "verify"
        if system.startswith("You are scoping a research brief"):
            return "frame"
        if system.startswith("You are reading research findings"):
            return "practice"
        if system.startswith("You are deciding how one piece"):
            return "reconcile"
        if system.startswith("You are writing the section specification"):
            return "kind"
        return "unknown"

    def default_for(self, stage, payload):
        if len(payload["messages"]) < 2:
            return "ready"
        user = json.loads(payload["messages"][1]["content"])
        if stage == "frame":
            wanted = {"identity", "provenance", "record_split", "measurement"}
            return {
                "field": "mineralogy",
                "cluster": "geoscience",
                "dimensions": [
                    {"id": row["id"], "applies": row["id"] in wanted, "term": row["label"].lower(),
                     "why": "stub"}
                    for row in user["dimensions"]
                ],
                "queries": ["mineral specimen cataloguing standard"],
            }
        if stage == "practice":
            found = practice_for(user["dimension"]["id"])
            if not found:
                return {"practice": "", "claims": [], "standard": "", "confidence": "low"}
            return {"practice": found[0], "claims": found[1], "standard": found[2],
                    "confidence": "high"}
        if stage == "reconcile":
            return RECONCILE_BY_DIMENSION.get(
                user["dimension"]["id"],
                {"move": "already-covered", "reason": "the vault already expresses this",
                 "value": "", "label": "", "definition": "", "domain": "", "kind": "",
                 "heading": ""},
            )
        if stage == "kind":
            return {
                "lead_guidance": "One sentence naming the species and what it is.",
                "sections": [
                    {"id": "identification", "heading": "Identification", "fill": "bullets",
                     "guidance": "Diagnostic properties.", "owner": False},
                    {"id": "occurrence", "heading": "Occurrence", "fill": "prose",
                     "guidance": "Where it forms.", "owner": False},
                    {"id": "measurements", "heading": "Measurements", "fill": "bullets",
                     "guidance": "Hardness on the Mohs scale.", "owner": False},
                ],
            }
        if stage == "verify":
            return {"verdicts": [{"id": item["id"], "verdict": "ok"} for item in user["items"]]}
        return {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        stage = self.stage_of(payload)
        queue = self.__class__.scripted.get(stage)
        if queue:
            response = queue.pop(0)
        else:
            response = self.default_for(stage, payload)
        content = response if isinstance(response, str) else json.dumps(response)
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


RECONCILE_BY_DIMENSION = {
    "identity": {"move": "naming", "reason": "titles carry the IMA-approved name",
                 "value": "", "label": "", "definition": "", "domain": "", "kind": "",
                 "heading": ""},
    "provenance": {"move": "body-table", "reason": "locality is a repeating per-specimen row",
                   "value": "", "label": "", "definition": "", "domain": "", "kind": "concept",
                   "heading": "Locality"},
    "record_split": {"move": "domain", "reason": "acquisitions are dated records, not cards",
                     "value": "collection", "label": "Collection",
                     "definition": "Specimen acquisition records.", "domain": "", "kind": "",
                     "heading": ""},
    "measurement": {"move": "subdomain", "reason": "mineral cards need a home in the wiki",
                    "value": "minerals", "label": "Minerals",
                    "definition": "Mineral reference cards.", "domain": "wiki", "kind": "",
                    "heading": ""},
}


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        return


class StubServer:
    def __init__(self, scripted=None):
        self.scripted = {key: list(value) for key, value in (scripted or {}).items()}

    def __enter__(self):
        StubHandler.requests = []
        StubHandler.scripted = self.scripted
        self.server = QuietServer(("127.0.0.1", 0), StubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/v1/chat/completions"
        return self

    def __exit__(self, *exc):
        StubHandler.scripted = {}
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def requests(self):
        return StubHandler.requests

    def stage_requests(self, stage):
        return [
            payload
            for payload in StubHandler.requests
            if StubHandler.stage_of(StubHandler, payload) == stage
        ]


class CuratorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "Loom"
        schemas = self.vault / "99 Meta" / "99.02 Schemas"
        schemas.mkdir(parents=True)
        self.schema_path = schemas / "0.00 Vault Schema.md"
        self.schema_path.write_text(SCHEMA, encoding="utf-8")
        for folder in ("00 Inbox", "02 Craft/2.01 Gardening", "09 Wiki/9.01 Concepts",
                       "09 Wiki/9.03 Places"):
            (self.vault / folder).mkdir(parents=True, exist_ok=True)
        (self.vault / "09 Wiki/9.01 Concepts" / "Cleavage.md").write_text(
            "---\ntype: concept\nstatus: active\ndomain: wiki\nsubdomain: concepts\n---\n\n# Cleavage\n",
            encoding="utf-8",
        )
        self.research = Path(self.tmp.name) / "research"
        self.research.mkdir()
        with (self.research / "claim_register.jsonl").open("w", encoding="utf-8") as handle:
            for claim in CLAIMS:
                handle.write(json.dumps(claim) + "\n")

    def agent_directory(self):
        """A settings file that turns the interactive idle grace off.

        Every background call otherwise sleeps `idleGraceMs` (2s) waiting for an
        interactive turn that cannot exist here, which is minutes across a suite
        that runs the whole pipeline twenty times. Overriding it through
        `connectedServices` exercises the same resolution path a real deployment
        uses rather than special-casing the library for tests.
        """
        directory = Path(self.tmp.name) / "agent"
        if not directory.is_dir():
            directory.mkdir(parents=True)
            scheduling = {"scheduling": {"idleGraceMs": 0, "yieldMs": 0}}
            (directory / "settings.json").write_text(
                json.dumps({"connectedServices": {"chat": scheduling, "think": scheduling}}),
                encoding="utf-8",
            )
        return directory

    def run_script(self, *args, expect=0):
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            # Point endpoint resolution away from whoever is running the tests.
            "PI_FORGE_AGENT_DIR": str(self.agent_directory()),
        }
        env.pop("PI_FORGE_HOME", None)
        env.pop("PI_CODING_AGENT_DIR", None)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(
            completed.returncode, expect,
            f"exit {completed.returncode}\nstdout: {completed.stdout}\nstderr: {completed.stderr}",
        )
        return json.loads(completed.stdout)

    def propose(self, server, *extra):
        return self.run_script(
            "propose", "--vault", str(self.vault), "--subject", "mineral specimens",
            "--research-dir", str(self.research),
            "--chat-url", server.url, "--think-url", server.url,
            "--chat-model", "chat", "--think-model", "code", *extra,
        )

    def proposals(self, run_dir):
        rows = []
        with (Path(run_dir) / "proposals.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
        return rows


class ProposeTests(CuratorTestCase):
    def test_a_full_run_proves_every_row_it_proposes(self):
        with StubServer() as server:
            result = self.propose(server)
        data = result["data"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(data["field"], "mineralogy")
        self.assertEqual(data["practicesEstablished"], 4)

        proposals = self.proposals(data["run"])
        by_move = {row["move"]: row for row in proposals}
        self.assertIn("domain", by_move)
        self.assertIn("subdomain", by_move)
        self.assertIn("body-table", by_move)

        # The number was chosen by code from the free slots, not by the model.
        self.assertNotIn("`2`", by_move["domain"]["rendered"])
        self.assertEqual(by_move["domain"]["insertion"]["cells"]["Number"], 1)
        self.assertEqual(by_move["subdomain"]["insertion"]["table"], "Subdomains/wiki")
        self.assertEqual(by_move["subdomain"]["insertion"]["cells"]["Number"], 2)

        # Every schema proposal carries the claims behind the practice it implements.
        for row in proposals:
            self.assertTrue(row["claims"], row["id"])
            self.assertEqual(row["verdict"], "ok")

        report = (Path(data["run"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Field practice", report)
        self.assertIn("IMA-CNMNC", report)
        handoff = (Path(data["run"]) / "handoff.md").read_text(encoding="utf-8")
        self.assertIn("--reuse-frontmatter", handoff)

    def test_the_schema_note_is_untouched_by_a_proposal_run(self):
        before = self.schema_path.read_text(encoding="utf-8")
        with StubServer() as server:
            self.propose(server)
        self.assertEqual(self.schema_path.read_text(encoding="utf-8"), before)

    def test_a_dimension_the_model_drops_is_restored_rather_than_treated_as_absent(self):
        thin = {
            "field": "mineralogy",
            "cluster": "geoscience",
            "dimensions": [{"id": "description", "applies": True, "term": "description", "why": ""}],
            "queries": ["q"],
        }
        with StubServer(scripted={"frame": [thin]}) as server:
            result = self.propose(server)
        restored = [w for w in result["warnings"] if "omitted dimension" in w]
        self.assertGreaterEqual(len(restored), 13)
        brief = json.loads((Path(result["data"]["run"]) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(len(brief["dimensions"]), 14)
        self.assertTrue(all(row["applies"] for row in brief["dimensions"] if row["id"] != "description"))

    def test_a_practice_citing_no_claim_is_dropped(self):
        uncited = {"practice": "Minerals are catalogued by colour.", "claims": [],
                   "standard": "", "confidence": "high"}
        with StubServer(scripted={"practice": [uncited]}) as server:
            result = self.propose(server)
        self.assertTrue(any("no claim cited" in w for w in result["warnings"]))
        practices = [
            json.loads(line)
            for line in (Path(result["data"]["run"]) / "practices.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        dropped = [row for row in practices if "cited no claim" in row["note"]]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["practice"], "")

    def test_a_run_with_no_research_proposes_nothing_and_says_so(self):
        with StubServer() as server:
            result = self.run_script(
                "propose", "--vault", str(self.vault), "--subject", "mineral specimens",
                "--no-web", "--chat-url", server.url, "--think-url", server.url,
            )
        self.assertEqual(result["data"]["proposals"], 0)
        self.assertTrue(any("--no-web" in w for w in result["warnings"]))
        report = (Path(result["data"]["run"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("Nothing was established", report)

    def test_an_unknown_move_is_recorded_as_refused_rather_than_guessed_at(self):
        rogue = {"move": "add-property", "reason": "specimens need a hardness property",
                 "value": "hardness", "label": "Hardness", "definition": "Mohs hardness.",
                 "domain": "", "kind": "", "heading": ""}
        with StubServer(scripted={"reconcile": [rogue]}) as server:
            result = self.propose(server)
        self.assertTrue(any("unknown move" in w for w in result["warnings"]))
        moves = [
            json.loads(line)
            for line in (Path(result["data"]["run"]) / "moves.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(moves[0]["move"], "refused")

    def test_a_colliding_row_is_held_with_its_reason_rather_than_proposed(self):
        # `concepts` is already registered under wiki, so the row cannot be added.
        collide = dict(RECONCILE_BY_DIMENSION["measurement"], value="concepts", label="Concepts")
        with StubServer(scripted={"reconcile": [
            RECONCILE_BY_DIMENSION["identity"],
            RECONCILE_BY_DIMENSION["provenance"],
            RECONCILE_BY_DIMENSION["record_split"],
            collide,
        ]}) as server:
            result = self.propose(server)
        validation = json.loads((Path(result["data"]["run"]) / "validation.json").read_text(encoding="utf-8"))
        held = [row for row in validation["held"] if row["move"] == "subdomain"]
        self.assertEqual(len(held), 1)
        self.assertIn("already registered", held[0]["detail"])
        self.assertNotIn("subdomain", {row["move"] for row in self.proposals(result["data"]["run"])})

    def test_the_notAvailable_list_names_the_property_refusal_to_the_model(self):
        with StubServer() as server:
            self.propose(server)
            reconcile = server.stage_requests("reconcile")
        self.assertTrue(reconcile)
        payload = json.loads(reconcile[0]["messages"][1]["content"])
        refusals = {row["id"] for row in payload["notAvailable"]}
        self.assertIn("approved-property", refusals)
        self.assertIn("project", refusals)

    def test_the_system_prompt_is_byte_stable_across_a_stage(self):
        with StubServer() as server:
            self.propose(server)
            practice = server.stage_requests("practice")
        self.assertGreater(len(practice), 1)
        systems = {payload["messages"][0]["content"] for payload in practice}
        self.assertEqual(len(systems), 1)


class TopicHubTests(CuratorTestCase):
    def test_a_route_with_no_notes_survives_when_the_field_accumulates_records(self):
        with StubServer() as server:
            result = self.propose(server)
        proposals = self.proposals(result["data"]["run"])
        domain = next(row for row in proposals if row["move"] == "domain")
        self.assertIn("accumulate", domain["routeEvidence"])

    def test_a_thin_route_is_demoted_to_a_topic_hub_when_nothing_accumulates(self):
        # Practice calls run once per *applicable* dimension, in the order the
        # shipped list declares them: identity, provenance, measurement,
        # record_split. Dropping the last one leaves nothing establishing that
        # this field's records accumulate.
        empty = {"practice": "", "claims": [], "standard": "", "confidence": "low"}
        scripted = {"practice": []}
        for identifier in ("identity", "provenance", "measurement"):
            found = practice_for(identifier)
            scripted["practice"].append(
                {"practice": found[0], "claims": found[1], "standard": found[2], "confidence": "high"}
            )
        scripted["practice"].append(empty)
        with StubServer(scripted=scripted) as server:
            result = self.propose(server)
        self.assertTrue(any("demoted to topic-hub" in w for w in result["warnings"]))
        moves = [
            json.loads(line)
            for line in (Path(result["data"]["run"]) / "moves.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        demoted = [row for row in moves if row.get("demotedFrom")]
        self.assertTrue(demoted)
        self.assertEqual(demoted[0]["move"], "topic-hub")
        self.assertIn(str(curator.SUBDOMAIN_NOTE_THRESHOLD), demoted[0]["routeEvidence"])
        # The report has to say why, not just that it did not propose one.
        report = (Path(result["data"]["run"]) / "report.md").read_text(encoding="utf-8")
        self.assertIn("proposed as a subdomain, but", report)
        self.assertIn(str(curator.SUBDOMAIN_NOTE_THRESHOLD), report)


class ResumeTests(CuratorTestCase):
    def test_the_same_invocation_resumes_the_named_run(self):
        with StubServer() as server:
            first = self.propose(server)
            run_dir = first["data"]["run"]
            second = self.propose(server, "--run", run_dir)
        self.assertEqual(second["data"]["run"], run_dir)
        self.assertEqual(second["data"]["proposals"], first["data"]["proposals"])

    def test_resuming_with_different_options_is_refused(self):
        with StubServer() as server:
            first = self.propose(server)
            run_dir = first["data"]["run"]
            result = self.run_script(
                "propose", "--vault", str(self.vault), "--subject", "something else",
                "--research-dir", str(self.research), "--run", run_dir,
                "--chat-url", server.url, "--think-url", server.url,
                "--chat-model", "chat", "--think-model", "code",
                expect=1,
            )
        self.assertIn("do not match this invocation", result["errors"][0]["message"])

    def test_a_second_run_reuses_a_claim_register_rather_than_researching_again(self):
        # The register lives outside the run directory here, so this also covers
        # --research-dir pointing at a web-research run made earlier.
        with StubServer() as server:
            result = self.propose(server)
        self.assertFalse((Path(result["data"]["run"]) / "research").exists())
        self.assertEqual(result["data"]["practicesEstablished"], 4)


class WikiKindTests(CuratorTestCase):
    """A drafted kind spec has to prove itself against the same checks the shipped ten pass."""

    def kind_run(self, scripted_kind=None):
        reconcile = dict(
            RECONCILE_BY_DIMENSION["measurement"],
            move="wiki-kind",
            value="mineral",
            label="Mineral",
            definition="Mineral reference cards.",
        )
        scripted = {"reconcile": [
            RECONCILE_BY_DIMENSION["identity"],
            RECONCILE_BY_DIMENSION["provenance"],
            reconcile,
            RECONCILE_BY_DIMENSION["record_split"],
        ]}
        if scripted_kind is not None:
            scripted["kind"] = [scripted_kind]
        with StubServer(scripted=scripted) as server:
            return self.propose(server)

    def test_a_drafted_spec_and_its_template_are_proved_to_agree(self):
        result = self.kind_run()
        proposal = next(row for row in self.proposals(result["data"]["run"]) if row["move"] == "wiki-kind")
        # The spec validates through the same function load_kind_specs uses.
        validated = curator.vault_wiki.validate_proposed_kind_spec("mineral", proposal["spec"])
        self.assertEqual(
            curator.vault_wiki.template_spec_drift(proposal["template"], validated, "Wiki Mineral.md"),
            [],
        )
        # The rendered template is in the shape the shipped ten have.
        template = proposal["template"]
        self.assertTrue(template.startswith("---\ntype: template\n"))
        for required in ("# {{title}}", "> [!abstract]", "## Sources", "## Evidence",
                         "## Provenance", "## Notes", "{{footnotes}}"):
            self.assertIn(required, template)

    def test_the_patch_files_are_written_and_the_handoff_names_the_code_change(self):
        result = self.kind_run()
        run_dir = Path(result["data"]["run"])
        names = sorted(path.name for path in (run_dir / "patches").glob("*"))
        self.assertTrue(any(name.endswith("wiki-kinds-mineral.json") for name in names), names)
        self.assertTrue(any(name.endswith("Wiki Mineral.md") for name in names), names)
        handoff = (run_dir / "handoff.md").read_text(encoding="utf-8")
        # Registering a kind is three lines of code the tool deliberately leaves alone.
        self.assertIn("WIKI_KIND_SUBDOMAIN", handoff)
        self.assertIn("forge/lib/vault_wiki.py", handoff)

    def test_a_spec_that_cannot_be_validated_is_held_rather_than_proposed(self):
        # Two sections, where the contract needs at least three.
        thin = {
            "lead_guidance": "One sentence.",
            "sections": [
                {"id": "identification", "heading": "Identification", "fill": "bullets",
                 "guidance": "Diagnostics.", "owner": False},
                {"id": "occurrence", "heading": "Occurrence", "fill": "prose",
                 "guidance": "Where.", "owner": False},
            ],
        }
        result = self.kind_run(scripted_kind=thin)
        self.assertTrue(any("usable sections" in w for w in result["warnings"]))
        moves = {row["move"] for row in self.proposals(result["data"]["run"])}
        self.assertNotIn("wiki-kind", moves)
        validation = json.loads((Path(result["data"]["run"]) / "validation.json").read_text(encoding="utf-8"))
        held = [row for row in validation["held"] if row["move"] == "wiki-kind"]
        self.assertEqual(len(held), 1)
        self.assertIn("could not be drafted", held[0]["detail"])

    def test_a_section_colliding_with_a_reserved_heading_is_dropped(self):
        greedy = {
            "lead_guidance": "One sentence.",
            "sections": [
                {"id": "identification", "heading": "Identification", "fill": "bullets",
                 "guidance": "Diagnostics.", "owner": False},
                {"id": "occurrence", "heading": "Occurrence", "fill": "prose",
                 "guidance": "Where.", "owner": False},
                {"id": "measurements", "heading": "Measurements", "fill": "bullets",
                 "guidance": "Mohs.", "owner": False},
                {"id": "sources", "heading": "Sources", "fill": "prose",
                 "guidance": "Where I read it.", "owner": False},
            ],
        }
        result = self.kind_run(scripted_kind=greedy)
        proposal = next(row for row in self.proposals(result["data"]["run"]) if row["move"] == "wiki-kind")
        headings = [
            section["heading"] for section in proposal["spec"]["sections"] if section.get("heading")
        ]
        self.assertEqual(headings.count("Sources"), 1)
        self.assertEqual(proposal["spec"]["sections"][-2]["id"], "notes")


class ApplyTests(CuratorTestCase):
    def apply_run(self, *extra):
        with StubServer() as server:
            result = self.propose(server)
        run_dir = result["data"]["run"]
        schema_ids = [row["id"] for row in self.proposals(run_dir) if row["side"] == "schema"]
        return run_dir, schema_ids

    def test_accepted_rows_are_written_and_the_note_still_parses(self):
        run_dir, schema_ids = self.apply_run()
        result = self.run_script(
            "apply", "--vault", str(self.vault), "--run", run_dir, "--accept", ",".join(schema_ids)
        )
        self.assertEqual(result["data"]["rowsWritten"], len(schema_ids))
        text = self.schema_path.read_text(encoding="utf-8")
        schema = vs.parse_schema_note(text)
        vs.validate_derived_paths(schema)
        self.assertIn("collection", schema["domains"])
        self.assertIn("minerals", schema["subdomains"]["wiki"])
        # The backup is the note as it was, byte for byte.
        backup = Path(run_dir) / "backup" / self.schema_path.name
        self.assertEqual(backup.read_text(encoding="utf-8"), SCHEMA)
        self.assertTrue(any("--reuse-frontmatter" in w for w in result["warnings"]))

    def test_nothing_outside_the_accepted_rows_moves(self):
        run_dir, schema_ids = self.apply_run()
        self.run_script("apply", "--vault", str(self.vault), "--run", run_dir,
                        "--accept", ",".join(schema_ids))
        before = SCHEMA.splitlines()
        after = self.schema_path.read_text(encoding="utf-8").splitlines()
        removed = [line for line in before if before.count(line) > after.count(line)]
        self.assertEqual(removed, [])

    def test_a_dry_run_writes_nothing(self):
        run_dir, schema_ids = self.apply_run()
        result = self.run_script("apply", "--vault", str(self.vault), "--run", run_dir,
                                 "--accept", schema_ids[0], "--dry-run")
        self.assertTrue(result["data"]["dryRun"])
        self.assertEqual(self.schema_path.read_text(encoding="utf-8"), SCHEMA)

    def test_an_unknown_id_is_refused_with_the_ids_the_run_has(self):
        run_dir, _ = self.apply_run()
        result = self.run_script("apply", "--vault", str(self.vault), "--run", run_dir,
                                 "--accept", "s-999", expect=1)
        self.assertEqual(result["errors"][0]["code"], "user_error")
        self.assertIn("unknown proposal id", result["errors"][0]["message"])

    def test_apply_with_no_ids_is_refused(self):
        run_dir, _ = self.apply_run()
        result = self.run_script("apply", "--vault", str(self.vault), "--run", run_dir, expect=1)
        self.assertIn("needs --accept", result["errors"][0]["message"])

    def test_a_row_proved_against_a_stale_note_is_refused_at_apply(self):
        run_dir, schema_ids = self.apply_run()
        # The owner registers the same subdomain by hand in the meantime.
        text, _ = vs.insert_schema_row(
            self.schema_path.read_text(encoding="utf-8"),
            "Subdomains/wiki",
            {"Value": "minerals", "Number": 4, "Label": "Minerals", "Definition": "Mine."},
        )
        self.schema_path.write_text(text, encoding="utf-8")
        result = self.run_script("apply", "--vault", str(self.vault), "--run", run_dir,
                                 "--accept", ",".join(schema_ids), expect=1)
        self.assertIn("refusing to write", result["errors"][0]["message"])
        self.assertEqual(self.schema_path.read_text(encoding="utf-8"), text)

    def test_a_rejected_proposal_is_not_offered_again(self):
        run_dir, schema_ids = self.apply_run()
        self.run_script("apply", "--vault", str(self.vault), "--run", run_dir,
                        "--reject", schema_ids[0])
        with StubServer() as server:
            second = self.propose(server)
        validation = json.loads((Path(second["data"]["run"]) / "validation.json").read_text(encoding="utf-8"))
        settled = [row for row in validation["held"] if row["outcome"] == "settled"]
        self.assertTrue(settled)
        self.assertIn("rejected", settled[0]["detail"])

    def test_repo_side_proposals_are_patch_files_and_edit_nothing(self):
        with StubServer() as server:
            result = self.propose(server)
        patches = sorted((Path(result["data"]["run"]) / "patches").glob("*"))
        self.assertTrue(patches)
        payload = json.loads(patches[0].read_text(encoding="utf-8"))
        self.assertIn("section", payload)
        shipped = ROOT / "skills" / "vault-wiki" / "references" / "wiki-kinds.json"
        self.assertNotIn("mineral", shipped.read_text(encoding="utf-8"))


class DoctorAndStatusTests(CuratorTestCase):
    def test_doctor_reports_the_drift_baseline_and_the_references(self):
        with StubServer() as server:
            result = self.run_script(
                "doctor", "--vault", str(self.vault),
                "--chat-url", server.url, "--think-url", server.url,
            )
        checks = result["data"]["checks"]
        self.assertEqual(checks["dimensions"], 14)
        self.assertIn("drift", checks)
        self.assertEqual(checks["schema"]["domains"], 3)

    def test_status_reports_the_phase_a_run_reached(self):
        with StubServer() as server:
            result = self.propose(server)
        status = self.run_script("status", "--vault", str(self.vault), "--run", result["data"]["run"])
        self.assertEqual(status["data"]["phase"], "complete")
        self.assertEqual(status["data"]["proposals"], result["data"]["proposals"])

    def test_review_pages_through_the_proposals(self):
        with StubServer() as server:
            result = self.propose(server)
        page = self.run_script("review", "--vault", str(self.vault), "--run", result["data"]["run"],
                               "--limit", "1", "--offset", "0")
        self.assertEqual(page["data"]["shown"], 1)
        self.assertEqual(page["data"]["total"], result["data"]["proposals"])


class EntryPointTests(CuratorTestCase):
    def test_refine_needs_a_route_the_schema_has(self):
        result = self.run_script("propose", "--vault", str(self.vault), "--refine", "wiki/animals",
                                 expect=1)
        self.assertIn("unknown subdomain", result["errors"][0]["message"])

    def test_exactly_one_entry_point_is_required(self):
        result = self.run_script("propose", "--vault", str(self.vault), expect=1)
        self.assertIn("exactly one of", result["errors"][0]["message"])

    def test_from_vault_refuses_when_the_organizer_has_recorded_nothing(self):
        result = self.run_script("propose", "--vault", str(self.vault), "--from-vault", expect=1)
        self.assertIn("no schema suggestions", result["errors"][0]["message"])

    def test_refine_runs_against_an_existing_route(self):
        with StubServer() as server:
            result = self.run_script(
                "propose", "--vault", str(self.vault), "--refine", "wiki/concepts",
                "--research-dir", str(self.research),
                "--chat-url", server.url, "--think-url", server.url,
            )
        brief = json.loads((Path(result["data"]["run"]) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["entryPoint"], "refine")
        self.assertEqual(brief["refine"], {"domain": "wiki", "subdomain": "concepts"})
        # The subject comes from the route's own definition, so the research is
        # about what is filed there rather than about the folder name.
        self.assertEqual(brief["subject"], "Named ideas.")
        survey = json.loads((Path(result["data"]["run"]) / "survey.json").read_text(encoding="utf-8"))
        self.assertEqual(survey["refineRoute"], "wiki/concepts")
        self.assertEqual(survey["refineNoteCount"], 1)

    def test_from_vault_reads_the_organizers_own_artifacts(self):
        run = self.vault / ".vault-organizer" / "runs" / "20260801T000000Z"
        run.mkdir(parents=True)
        (run / "plan.json").write_text(
            json.dumps({"schemaSuggestions": [{"suggestion": "a subdomain for mineral specimens"}]}),
            encoding="utf-8",
        )
        with StubServer() as server:
            result = self.run_script(
                "propose", "--vault", str(self.vault), "--from-vault",
                "--research-dir", str(self.research),
                "--chat-url", server.url, "--think-url", server.url,
            )
        brief = json.loads((Path(result["data"]["run"]) / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["entryPoint"], "from-vault")
        self.assertIn("mineral specimens", brief["subject"])


if __name__ == "__main__":
    unittest.main()
