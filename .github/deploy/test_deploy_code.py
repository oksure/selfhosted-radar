"""Offline CI deployment contract: synthetic CLI and HTTP, no real hosts."""
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
    self.after_publish = None
    self.fail = None
    self.events = []
    self.plan = {"toDelete": ["pb_hooks/obsolete.js"], "missing": []}
    self.extra = None
    self.check_error = False
    self.health_error = False
    self.mutate_stage = False
    self.environment = patch.dict(os.environ, {
      "POCKETHOST_TOKEN": "fixture-secret", "POCKETHOST_MOTHERSHIP_URL": "https://example.invalid",
      "PHIO_MOTHERSHIP_URL": "https://example.invalid",
    })
    self.environment.start()
    self.addCleanup(self.environment.stop)

  def run_cli(self, argv, *, cwd, env, **kwargs):
    self.assertNotIn("fixture-secret", " ".join(argv))
    self.assertEqual(env["POCKETHOST_TOKEN"], "fixture-secret")
    self.assertEqual(env["POCKETHOST_MOTHERSHIP_URL"], "https://mothership.pockethost.io")
    self.assertNotIn("PHIO_MOTHERSHIP_URL", env)
    self.assertEqual(env["POCKETHOST_PROJECT_DIR"], str(cwd))
    self.assertEqual(env["INIT_CWD"], str(cwd))
    self.assertEqual(json.loads((Path(cwd) / ".pockethostconfig").read_text()),
                     {"instanceName": "fixture"})
    self.assertIn("--no-node-modules", argv) if argv[1] == "deploy" else None
    out, code = "", 0
    if "--out" in argv:
      self.events.append("stage")
      self.stage = Path(argv[argv.index("--out") + 1])
      shutil.copytree(cwd, self.stage, ignore=shutil.ignore_patterns(".pockethostconfig"))
      if self.extra:
        target = self.stage / self.extra
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("outside")
    elif argv[1] == "show":
      self.events.append("show")
      out = json.dumps({"id": "fixture-id", "subdomain": "fixture", "power": self.state, "status": "idle"})
      if self.fail == "show":
        out = "{}"
    elif argv[1] == "power":
      self.events.append("power-" + argv[2])
      self.state = argv[2] == "on"
      if not self.state:
        self.assertIn("--wait", argv)
    elif argv[1] == "pull":
      self.events.append("preserve")
      output = Path(argv[argv.index("--dir") + 1]) / argv[2]
      output.parent.mkdir(parents=True)
      output.write_text("private remote fixture")
    else:
      self.events.append("publish")
      self.assertEqual(Path(cwd), self.stage)
      self.assertIn("--yes", argv)
      self.assertEqual(deploy_code.manifest(self.stage), self.checked)
      if "pb_public/index.html" in self.checked:
        self.assertEqual((self.stage / "pb_public/index.html").read_text(), "original")
      if self.after_publish is not None:
        self.state = self.after_publish
      if self.fail == "exit":
        code, out = 1, "fixture-secret"
      if self.fail == "start":
        out = "Instance failed to start: fixture-secret"
      if self.fail == "timeout":
        raise subprocess.TimeoutExpired(argv, 240)
    return subprocess.CompletedProcess(argv, code, out, "")

  def open_http(self, req, timeout):
    self.assertEqual(timeout, 15)
    self.assertEqual(req.get_header("User-agent"), deploy_code.USER_AGENT)
    if req.full_url.endswith("/check"):
      self.events.append("check")
      self.assertEqual(req.full_url, "https://fixture.pockethost.io/_ph/deploy/v1/check")
      self.assertEqual(req.get_method(), "POST")
      self.assertEqual(req.get_header("Authorization"), "Bearer fixture-secret")
      body = json.loads(req.data)
      self.assertIs(body["nodeModules"], False)
      self.checked = body["files"]
      self.assertEqual(self.checked, deploy_code.manifest(self.stage))
      # Changing live sources after staging must not affect the publish set.
      (self.source / "pb_public/index.html").write_text("changed after staging")
      if self.mutate_stage:
        (self.stage / "pb_public/index.html").write_text("changed stage")
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

  def deploy(self):
    if (self.source / "pb_public/index.html").exists():
      (self.source / "pb_public/index.html").write_text("original")
    opener = unittest.mock.Mock()
    opener.open.side_effect = self.open_http
    with patch.object(deploy_code.subprocess, "run", side_effect=self.run_cli), \
         patch.object(deploy_code.urllib.request, "build_opener", return_value=opener), \
         patch.object(deploy_code.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
      deploy_code.deploy("fixture", self.source)

  def test_allowed_obsolete_code_and_exact_staged_content(self):
    self.deploy()
    self.assertLess(self.events.index("stage"), self.events.index("check"))
    self.assertLess(self.events.index("check"), self.events.index("publish"))
    self.assertIn("pb_public/.well-known/example", self.checked)
    self.assertEqual(self.events[-1], "health")

  def test_unsafe_remote_deletions_fail_before_publish(self):
    for name in ["pb_data/data.db", "node_modules/pkg/a.js", "package.json", "pb_other/x",
                 "pb_publicity/x", "pb_hooks/../pb_data/x", "/pb_hooks/a", "./pb_hooks/a",
                 "pb_hooks//a", "pb_hooks/./a", "pb_hooks/a/../../x", "pb_hooks\\x",
                 "pb_hooks", "pb_hooks/pb_data/x", "pb_hooks/\nfile",
                 "pb_hooks/%2e%2e/other", "pb_hooks/_ph_admin_sync.pb.js",
                 "pb_migrations/1699198734_pockethost_defaults.js", None, 3, {}]:
      with self.subTest(name=name):
        self.events.clear()
        self.plan = {"toDelete": [name], "missing": []}
        with self.assertRaises(deploy_code.DeployError):
          self.deploy()
        self.assertNotIn("publish", self.events)

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
    self.assertNotIn("private remote fixture", json.dumps(self.checked))
    self.assertFalse((self.source / "pb_hooks/private.pb.js").exists())
    self.assertIn("preserve", self.events)

  def test_private_file_repository_conflict_blocks_publish(self):
    (self.source / "pb_hooks/private.pb.js").write_text("do not publish")
    with patch.dict(deploy_code.PRESERVED, {"fixture": ("pb_hooks/private.pb.js",)}):
      with self.assertRaises(deploy_code.DeployError):
        self.deploy()
    self.assertNotIn("publish", self.events)

  def test_avrec_is_never_a_deployment_target(self):
    with self.assertRaises(deploy_code.DeployError):
      deploy_code.deploy("avrec", self.source)
    self.assertEqual(self.events, [])

  def test_invalid_check_responses_fail_closed(self):
    for plan in [{}, None, [], {"toDelete": None}, {"toDelete": "pb_hooks/x"},
                 {"toDelete": []}, {"toDelete": [], "missing": ["not-a-hash"]},
                 {"toDelete": [], "missing": {}}, b"not JSON", b""]:
      with self.subTest(plan=plan):
        self.events.clear()
        self.plan = plan
        with self.assertRaises(deploy_code.DeployError):
          self.deploy()
        self.assertNotIn("publish", self.events)

  def test_missing_token_stops_before_cli(self):
    with patch.dict(os.environ, {"POCKETHOST_TOKEN": " "}):
      with self.assertRaises(deploy_code.DeployError):
        self.deploy()
    self.assertEqual(self.events, [])

  def test_missing_folder_and_symlink_fail(self):
    shutil.rmtree(self.source / "pb_hooks")
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    (self.source / "pb_hooks").mkdir()
    (self.source / "pb_hooks/link").symlink_to(self.source / "pb_public/index.html")
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()

  def test_stage_outside_code_fails_before_network(self):
    for name in ["pb_extra/file", "package.json", "node_modules/file", "pb_public/pb_data/file"]:
      with self.subTest(name=name):
        self.events.clear()
        self.extra = name
        with self.assertRaises(deploy_code.DeployError):
          self.deploy()
        self.assertNotIn("check", self.events)

  def test_empty_manifest_still_requires_guarded_remote_plan(self):
    shutil.rmtree(self.source / "pb_public")
    (self.source / "pb_public").mkdir()
    self.deploy()
    self.assertEqual(self.checked, {})
    self.assertIn("check", self.events)

  def test_cli_failure_and_zero_exit_start_error_are_redacted(self):
    for failure in ("exit", "start", "timeout"):
      with self.subTest(failure=failure):
        self.state = True
        self.events.clear()
        self.fail = failure
        self.after_publish = False
        with self.assertRaises(deploy_code.DeployError) as caught:
          self.deploy()
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertEqual(self.state, failure == "start")
        if failure != "start":
          self.assertNotIn("power-on", self.events)
          self.assertEqual(self.events[-1], "publish")

  def test_original_off_restored_without_health_wake(self):
    self.state = False
    self.after_publish = True
    self.deploy()
    self.assertFalse(self.state)
    self.assertIn("power-off", self.events)
    self.assertNotIn("health", self.events)

  def test_original_on_restored_and_health_bounded(self):
    self.after_publish = False
    self.health_error = True
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    self.assertTrue(self.state)
    self.assertIn("power-on", self.events)
    self.assertEqual(self.events.count("health"), 12)

  def test_invalid_show_and_http_failure_block_publish(self):
    self.fail = "show"
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    self.assertNotIn("check", self.events)
    self.fail = None
    self.check_error = True
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    self.assertNotIn("publish", self.events)

  def test_stage_change_after_check_blocks_publish(self):
    self.mutate_stage = True
    with self.assertRaises(deploy_code.DeployError):
      self.deploy()
    self.assertNotIn("publish", self.events)

  def test_plain_instance_required(self):
    for name in ("https://example.invalid", "../x", "fixture.example.invalid", "-bad"):
      with self.assertRaises(deploy_code.DeployError):
        deploy_code.deploy(name, self.source)
    self.assertEqual(self.events, [])

  def test_workflow_contract(self):
    text = Path(__file__).parents[1].joinpath("workflows/deploy.yml").read_text()
    for fragment in ("node-version: '24'", "pockethost@4.3.0", "max-parallel: 1",
                     "group: pockethost-${{ matrix.instance }}", "cancel-in-progress: false",
                     "POCKETHOST_TOKEN: ${{ secrets.POCKETHOST_TOKEN }}", "'.github/deploy/**'"):
      self.assertIn(fragment, text)
    self.assertNotIn("SFTP", text)
    self.assertNotIn("lftp", text)
    expected = "selfhosted" if Path(__file__).parents[2].name == "selfhosted-radar" else "bible, console, gloria, sitefb, taskq"
    self.assertIn("instance: [" + expected + "]", text)

  @unittest.skipUnless(shutil.which("pockethost"), "official CLI not installed")
  def test_official_cli_local_stage_ignores_ancestor_package_and_environment(self):
    # Real CLI local-only regression: cwd alone does not prevent ancestor lookup.
    (self.source.parent / "package.json").write_text('{"name":"wrong-ancestor"}')
    (self.source / ".pockethostconfig").write_text('{"instanceName":"fixture"}')
    stage = self.source.parent / "official-stage"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "POCKETHOST_HOME": str(self.source.parent / "cli-home"),
           "POCKETHOST_PROJECT_DIR": str(self.source),
           "INIT_CWD": str(self.source.parent),
           "POCKETHOST_MOTHERSHIP_URL": "https://mothership.pockethost.io"}
    args = ["pockethost", "deploy", "fixture", "--out", str(stage),
            "--no-node-modules", "--include", *deploy_code.INCLUDES]
    result = subprocess.run(args, cwd=self.source, env=env, capture_output=True,
                            text=True, timeout=30)
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(deploy_code.manifest(stage),
                     {"pb_public/index.html": deploy_code.hashlib.sha256(b"original").hexdigest(),
                      "pb_public/.well-known/example": deploy_code.hashlib.sha256(b"dotfile").hexdigest()})


if __name__ == "__main__":
  unittest.main()
