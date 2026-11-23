"""Offline CI deployment contract for the v5 CLI: synthetic processes and HTTP."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("deploy_code", Path(__file__).with_name("deploy-code.py"))
deploy_code = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_code)

TOKEN = "pbt_fixture-secret"


class DeploymentTests(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.source = Path(self.tmp.name) / "source"
    self.source.mkdir()
    for folder in deploy_code.ROOTS:
      (self.source / folder).mkdir()
    (self.source / "pb_public/index.html").write_text("original")
    (self.source / "pb_public/.well-known").mkdir()
    (self.source / "pb_public/.well-known/example").write_text("dotfile")
    self.state = True
    self.events = []
    self.checked = None
    self.plan = {"missing": []}
    self.check_error = False
    self.health_error = False
    self.failure = None
    self.environment = patch.dict(os.environ, {"POCKETHOST_TOKEN": TOKEN})
    self.environment.start()
    self.addCleanup(self.environment.stop)
    self.which = patch.object(deploy_code.shutil, "which", return_value="/synthetic/phost")
    self.which.start()
    self.addCleanup(self.which.stop)

  # --- process seams ---------------------------------------------------
  def run_process(self, argv, **kwargs):
    self.assertNotIn(TOKEN, " ".join(argv))
    if argv[0] == "phost":
      self.events.append("power:" + argv[1])
      if self.failure == "show" and argv[1] == "status":
        return subprocess.CompletedProcess(argv, 0, "{}", "")
      if argv[1] in ("on", "off"):
        self.state = argv[1] == "on"
        return subprocess.CompletedProcess(argv, 0, "", "")
      record = {"id": "fixture-id", "subdomain": "fixture", "power": self.state}
      return subprocess.CompletedProcess(argv, 0, json.dumps(record) + "\nlive health: HTTP 200\n", "")
    self.assertEqual(argv[:2], ["pockethost", "link"] if "link" in argv[1] else argv[:2])
    if argv[1] == "link":
      self.events.append("link")
      self.assertEqual(kwargs["input"], TOKEN)
      self.assertIn("--token-stdin", argv)
      # v5 records the remote in the project before probing its health.
      remotes = Path(kwargs["cwd"]) / ".pocketbase/remotes.json"
      remotes.parent.mkdir(parents=True, exist_ok=True)
      remotes.write_text(json.dumps({"default": "deploy",
                                     "remotes": {"deploy": {"url": argv[2]}}}))
      return subprocess.CompletedProcess(argv, 0, "Linked\n", "")
    if argv[1] == "deploy" and "--out" in argv:
      self.events.append("stage")
      stage = Path(argv[argv.index("--out") + 1])
      # Mirror the real CLI: only files present in the source reach the stage.
      stage.mkdir(parents=True, exist_ok=True)
      for folder in deploy_code.ROOTS:
        shutil.copytree(kwargs["cwd"] / folder, stage / folder,
                        ignore=shutil.ignore_patterns(".gitkeep"))
      return subprocess.CompletedProcess(argv, 0, "Wrote files\n", "")
    if argv[1] == "deploy":
      self.events.append("publish")
      self.assertEqual(kwargs["cwd"], getattr(self, "stage", None))
      if self.failure == "exit":
        return subprocess.CompletedProcess(argv, 1, TOKEN, "")
      if self.failure == "timeout":
        raise subprocess.TimeoutExpired(argv, 240)
      return subprocess.CompletedProcess(argv, 0, "", "")
    if argv[1] == "pull":
      self.events.append("preserve")
      # v5 pulls into the project directory and also fetches pb_data.
      output = Path(kwargs["cwd"]) / argv[-1]
      output.parent.mkdir(parents=True, exist_ok=True)
      output.write_text("private remote fixture")
      # v5 pull also drags pb_data into the project; it must never ship.
      (Path(kwargs["cwd"]) / "pb_data").mkdir(exist_ok=True)
      (Path(kwargs["cwd"]) / "pb_data/data.db").write_bytes(b"never ship")
      return subprocess.CompletedProcess(argv, 0, "", "")
    raise AssertionError(argv)

  def open_http(self, req, timeout):
    self.assertEqual(req.get_header("User-agent"), deploy_code.USER_AGENT)
    if req.full_url.endswith("/check"):
      self.events.append("check")
      self.assertEqual(req.full_url, "https://fixture.pockethost.io/api/_c/deploy/v1/check")
      self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
      body = json.loads(req.data)
      self.assertEqual(set(body), {"files"})
      self.assertNotIn("pb_data/data.db", body["files"])
      self.checked = body["files"]
      (self.source / "pb_public/index.html").write_text("changed after staging")
      if self.check_error:
        raise OSError("fixture-secret")
      payload = self.plan
    else:
      self.events.append("health")
      self.assertEqual(req.full_url, "https://fixture.pockethost.io/api/health")
      self.assertIsNone(req.get_header("Authorization"))
      payload = {"code": 503 if self.health_error else 200}
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    response = unittest.mock.MagicMock()
    response.__enter__.return_value.read.return_value = raw
    return response

  def deploy(self, instance="fixture", source=None):
    if not getattr(self, "empty_source", False):
      (self.source / "pb_public/index.html").write_text("original")
    self.stage = None
    real_run = self.run_process
    def run(argv, **kwargs):
      if argv[1:2] == ["deploy"] and "--out" in argv:
        self.stage = Path(argv[argv.index("--out") + 1])
      return real_run(argv, **kwargs)
    opener = unittest.mock.Mock()
    opener.open.side_effect = self.open_http
    with patch.object(deploy_code.subprocess, "run", side_effect=run), \
         patch.object(deploy_code.urllib.request, "build_opener", return_value=opener), \
         patch.object(deploy_code.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
      deploy_code.deploy(instance, source or self.source)

  # --- contract --------------------------------------------------------
  def test_stage_then_check_then_publish_keeps_pb_data_out(self):
    (self.source / "pb_data").mkdir()
    (self.source / "pb_data/data.db").write_bytes(b"never ship")
    self.deploy()
    self.assertLess(self.events.index("stage"), self.events.index("check"))
    self.assertLess(self.events.index("check"), self.events.index("publish"))
    self.assertIn("pb_public/.well-known/example", self.checked)
    self.assertFalse(any(p.startswith("pb_data/") for p in self.checked))
    self.assertEqual(self.events[-1], "health")

  def test_publish_uses_the_linked_remote_name(self):
    self.deploy()
    self.assertIn("link", self.events)

  def test_invalid_check_responses_fail_closed(self):
    for plan in [{}, None, [], {"missing": None}, {"missing": ["not-a-hash"]},
                 {"missing": {}, "toDelete": []}, b"not JSON", b""]:
      with self.subTest(plan=plan):
        self.events.clear()
        self.plan = plan
        with self.assertRaises(deploy_code.DeployError):
          self.deploy()
        self.assertNotIn("publish", self.events)

  def test_stage_outside_code_fails_before_network(self):
    (self.source / "package.json").write_text("{}")
    with patch.object(deploy_code, "manifest", side_effect=deploy_code.DeployError("outside")):
      with self.assertRaises(deploy_code.DeployError):
        self.deploy()
    self.assertNotIn("check", self.events)

  def test_empty_code_folder_still_deploys(self):
    # v5 writes no stage entry for a folder with no files; the stage must
    # still exist so the remaining folders and the scope check can run.
    shutil.rmtree(self.source / "pb_public")
    (self.source / "pb_public").mkdir()
    self.deploy()
    # The point is that an empty folder no longer aborts staging.
    self.assertIn("check", self.events)
    self.assertIn("publish", self.events)

  def test_empty_manifest_never_wakes_a_stopped_instance(self):
    self.state = False
    for folder in deploy_code.ROOTS:
      shutil.rmtree(self.source / folder)
      (self.source / folder).mkdir()
      (self.source / folder / ".gitkeep").write_text("")
    self.empty_source = True
    self.deploy()
    self.assertNotIn("power:on", self.events)
    self.assertNotIn("publish", self.events)
    self.assertFalse(self.state)

  def test_missing_folder_and_symlink_fail(self):
    shutil.rmtree(self.source / "pb_hooks")
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    (self.source / "pb_hooks").mkdir()
    (self.source / "pb_hooks/link").symlink_to(self.source / "pb_public/index.html")
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()

  def test_provider_files_in_repository_are_preserved_not_published(self):
    paths = ["pb_hooks/_ph_admin_sync.pb.js", "pb_hooks/_ph_lib.js",
             "pb_migrations/1699198734_pockethost_defaults.js"]
    for name in paths:
      (self.source / name).write_text("provider reference")
    self.deploy()
    self.assertTrue(all(name not in self.checked for name in paths))
    self.assertTrue(all((self.source / name).is_file() for name in paths))

  def test_private_remote_file_is_never_copied_to_repository(self):
    with patch.dict(deploy_code.PRESERVED, {"fixture": ("pb_hooks/private.pb.js",)}):
      self.deploy()
    self.assertIn("pb_hooks/private.pb.js", self.checked)
    self.assertFalse((self.source / "pb_hooks/private.pb.js").exists())
    self.assertIn("preserve", self.events)

  def test_avrec_and_non_pbt_tokens_are_never_deployment_targets(self):
    with self.assertRaises(deploy_code.DeployError):
      self.deploy("avrec")
    self.assertEqual(self.events, [])
    with patch.dict(os.environ, {"POCKETHOST_TOKEN": "ph_pat_old"}):
      with self.assertRaises(deploy_code.DeployError):
        self.deploy()
    self.assertEqual(self.events, [])

  def test_plain_instance_required(self):
    for name in ("https://example.invalid", "../x", "fixture.example.invalid", "-bad"):
      with self.assertRaises(deploy_code.DeployError):
        self.deploy(name)
    self.assertEqual(self.events, [])

  def test_missing_token_stops_before_cli(self):
    with patch.dict(os.environ, {"POCKETHOST_TOKEN": " "}):
      with self.assertRaises(deploy_code.DeployError):
        self.deploy()
    self.assertEqual(self.events, [])

  def test_publish_failure_is_uncertain_and_redacted(self):
    for failure in ("exit", "timeout"):
      with self.subTest(failure=failure):
        self.events.clear()
        self.failure = failure
        with self.assertRaises(deploy_code.UncertainDeploy) as caught:
          self.deploy()
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.failure = None

  def test_invalid_status_and_check_failure_block_publish(self):
    self.failure = "show"
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    self.assertNotIn("check", self.events)
    self.failure = None
    self.check_error = True
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    self.assertNotIn("publish", self.events)

  def test_off_instance_is_woken_only_to_publish_and_left_off(self):
    # v5 refuses to publish to a stopped instance, so it is woken for the
    # publication and then returned to its recorded off state.
    self.state = False
    self.deploy()
    self.assertFalse(self.state)
    self.assertLess(self.events.index("power:on"), self.events.index("publish"))
    self.assertLess(self.events.index("publish"), self.events.index("power:off"))
    # Wake it, confirm the app answers, then check scope before publishing.
    self.assertLess(self.events.index("power:on"), self.events.index("health"))
    self.assertLess(self.events.index("health"), self.events.index("check"))
    self.assertLess(self.events.index("check"), self.events.index("publish"))

  def test_on_instance_is_probed_after_publish_without_power_changes(self):
    self.deploy()
    self.assertTrue(self.state)
    self.assertNotIn("power:on", self.events)
    self.assertNotIn("power:off", self.events)
    self.assertEqual(self.events.count("health"), 1)

  def test_unhealthy_instance_fails_with_a_bounded_probe(self):
    self.health_error = True
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    self.assertEqual(self.events.count("health"), 12)

  def test_workflow_contract(self):
    text = Path(__file__).parents[1].joinpath("workflows/deploy.yml").read_text()
    for fragment in ("POCKETHOST_VERSION: 5.0.0-alpha.10",
                     "releases/download/v${POCKETHOST_VERSION}", "sha256sum -c -",
                     "max-parallel: 1",
                     "group: pockethost-${{ matrix.instance }}", "cancel-in-progress: false",
                     "POCKETHOST_TOKEN: ${{ secrets.POCKETHOST_TOKEN }}", "'.github/deploy/**'"):
      self.assertIn(fragment, text)
    # v5 ships as release binaries only; the pre-releases are not on npm.
    self.assertNotIn("npm install", text)
    self.assertNotIn("SFTP", text)
    self.assertNotIn("lftp", text)
    expected = "selfhosted" if Path(__file__).parents[2].name == "selfhosted-radar" else "bible, console, gloria, sitefb, taskq"
    self.assertIn("instance: [" + expected + "]", text)


if __name__ == "__main__":
  unittest.main()
