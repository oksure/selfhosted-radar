"""Credential selection fixtures: no managed secrets or network."""
import sys
from pathlib import Path

import pytest

from collector import pbwrite as W
from collector import sync


@pytest.fixture
def helper(tmp_path, monkeypatch):
  monkeypatch.setattr(Path, 'home', lambda: tmp_path)
  # Register restoration even when the module was absent before this test.
  monkeypatch.setitem(sys.modules, 'pb_credentials', None)
  del sys.modules['pb_credentials']
  monkeypatch.setattr(sync, 'BASE', 'https://selfhosted.pockethost.io')
  for key in ('SELFHOSTED_PB_EMAIL', 'SELFHOSTED_PB_PASS', 'SELFHOSTED_PB_URL'):
    monkeypatch.delenv(key, raising=False)
  directory = tmp_path / 'dotfiles/python'
  directory.mkdir(parents=True)
  module = directory / 'pb_credentials.py'
  module.write_text("from types import SimpleNamespace\n"
                    "def resolve(target):\n"
                    "  assert target == 'selfhosted'\n"
                    "  return SimpleNamespace(url='https://selfhosted.pockethost.io', "
                    "email='fixture@example.invalid', password='fixture-only')\n")
  return module


def test_local_module_selected_before_login(helper, monkeypatch):
  calls = []
  monkeypatch.setattr(W.PB, '_req', lambda *a, **kw: {})
  monkeypatch.setattr(W.PB, '_login', lambda self, email, password: calls.append((email, password)))
  client = W.PB('https://selfhosted.pockethost.io')
  assert client.base == 'https://selfhosted.pockethost.io'
  assert calls == [('fixture@example.invalid', 'fixture-only')]


def test_explicit_ci_pair_allows_fixture_base_without_helper(helper):
  helper.unlink()
  assert W.credentials('https://fixture.example.invalid', 'ci@example.invalid', 'ci-only') == (
    'ci@example.invalid', 'ci-only')


@pytest.mark.parametrize('email,password', [('ci@example.invalid', None), (None, 'ci-only'), ('', '')])
def test_partial_credentials_never_fall_back(helper, email, password):
  with pytest.raises(ValueError, match='complete pair'):
    W.credentials('https://selfhosted.pockethost.io', email, password)
  assert 'pb_credentials' not in sys.modules


def test_custom_base_requires_explicit_pair(helper):
  with pytest.raises(ValueError, match='custom'):
    W.credentials('https://fixture.example.invalid')
  assert 'pb_credentials' not in sys.modules


def test_wrong_helper_target_is_rejected(helper):
  helper.write_text(helper.read_text().replace('https://selfhosted.pockethost.io', 'https://wrong.example.invalid'))
  with pytest.raises(RuntimeError, match='unavailable'):
    W.credentials('https://selfhosted.pockethost.io')


def test_helper_failure_has_safe_cli_report(helper, capsys):
  helper.write_text("def resolve(target):\n  raise RuntimeError('fixture-secret-do-not-log')\n")
  assert sync.main(['--sources', 'awesome']) == 2
  output = capsys.readouterr()
  assert 'MissingCredentials' in output.out
  assert 'fixture-secret' not in output.out + output.err


def test_dry_run_does_not_resolve_local_credentials(helper, monkeypatch, capsys):
  helper.write_text("raise AssertionError('dry run must not import credentials')\n")
  monkeypatch.setattr(sync, 'collect', lambda names: [])
  assert sync.main(['--dry-run']) == 0
  assert '"dry_run": true' in capsys.readouterr().out
  assert 'pb_credentials' not in sys.modules


def test_sync_passes_local_credentials_to_client(helper, monkeypatch, capsys):
  calls = []
  def stop(base, email, password, *args):
    calls.append((base, email, password))
    raise RuntimeError('stop before transport')
  monkeypatch.setattr(W, 'PB', stop)
  monkeypatch.setattr(sync, 'BASE', 'https://selfhosted.pockethost.io')
  assert sync.main(['--resume']) == 1
  assert calls == [('https://selfhosted.pockethost.io', 'fixture@example.invalid', 'fixture-only')]
  assert 'authentication' in capsys.readouterr().out
