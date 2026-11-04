from __future__ import annotations

import base64
import copy
import io
import json
import os
import ssl
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reqslim.zcode import tls, wiring
from reqslim.zcode import __main__ as zcode_cli
from reqslim.zcode.__main__ import load_autostart_config
from tools import install_zcode as installer


UP_A = "https://a.example/anthropic"
UP_B = "https://b.example/anthropic"
LOCAL_A = "https://127.0.0.1:18788"
LOCAL_B = "https://127.0.0.1:18789"
REAL_SET_ENVIRONMENT = installer._set_user_environment
REAL_READ_ENVIRONMENT = installer._read_user_environment
TEMP_ROOT = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Temp" / "opencode"
    if os.name == "nt" else Path(tempfile.gettempdir()).resolve() / "opencode"
)
TEMP_ROOT = TEMP_ROOT if TEMP_ROOT.is_dir() else None


def entry(ident: str, url: str, protocol: str = "anthropic-messages", template: bool = False) -> dict:
    return {
        "templateId" if template else "providerId": ident,
        "config": {"api": {"type": protocol, "baseUrl": url}},
    }


def write_table(path: Path, entries: list[dict], templates: list[dict] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    path.write_text(json.dumps({
        "config": {"providerConfigRules": {"providerRules": entries, "templateRules": templates or []}},
    }), encoding="ascii")


def urls(path: Path) -> dict[str, str]:
    rules = json.loads(path.read_text(encoding="utf-8"))["config"]["providerConfigRules"]
    return {
        item.get("providerId", item.get("templateId")): item["config"]["api"]["baseUrl"]
        for section in ("providerRules", "templateRules") for item in rules[section]
    }


def file_states(root: Path) -> dict[Path, tuple[bytes, int, int]]:
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            info = path.stat()
            result[path] = (path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    return result


class InstallRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="reqslim-install-tests-", dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.package = self.root / "package"
        self.startup = self.root / "startup"
        for path in (self.home, self.package, self.startup):
            path.mkdir()
        self.config = self.home / ".zcode" / "reqslim"
        self.resources = self.root / "ZCode" / "resources"
        self.table = self.resources / wiring.PROVIDER_TABLE
        write_table(self.table, [entry("a", UP_A), entry("b", UP_B)])
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.errors))
        self.stack.enter_context(mock.patch.object(Path, "home", return_value=self.home))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "APPDATA": str(self.root / "appdata"), "LOCALAPPDATA": str(self.root / "local"),
        }))
        windows_os = types.SimpleNamespace(**vars(os))
        windows_os.name = "nt"
        self.stack.enter_context(mock.patch.object(installer, "os", windows_os))
        self.stack.enter_context(mock.patch.object(installer, "_startup_dir", return_value=self.startup))
        self.run = self.stack.enter_context(mock.patch.object(
            subprocess, "run", side_effect=AssertionError("unexpected real subprocess"),
        ))
        self.popen = self.stack.enter_context(mock.patch.object(
            subprocess, "Popen", side_effect=AssertionError("unexpected real proxy process"),
        ))
        self.health = self.stack.enter_context(mock.patch.object(installer, "health_on_port", return_value=None))
        self.shutdown = self.stack.enter_context(mock.patch.object(
            installer, "stop_running_proxy", side_effect=AssertionError("unexpected proxy shutdown"),
        ))
        self.stack.enter_context(mock.patch.object(
            installer, "_powershell", side_effect=AssertionError("unexpected PowerShell"),
        ))
        self.stack.enter_context(mock.patch.object(installer, "_notify_environment"))
        self.environment = {}
        self.environment_writes = []
        self.read_environment = self.stack.enter_context(mock.patch.object(
            installer, "_read_user_environment", side_effect=lambda name: copy.deepcopy(self.environment.get(name)),
        ))

        def set_environment(name, value):
            self.environment_writes.append((name, copy.deepcopy(value)))
            if value is None:
                self.environment.pop(name, None)
            else:
                self.environment[name] = copy.deepcopy(value)
            return True

        self.set_environment = self.stack.enter_context(mock.patch.object(
            installer, "_set_user_environment", side_effect=set_environment,
        ))

    def cache(self, version: str = "1", entries: list[dict] | None = None) -> Path:
        path = self.home / ".zcode" / "v2" / "runtime" / "provider" / "windows" / version / "endpoint-test" / "zcode-builtin.json"
        write_table(path, entries if entries is not None else [entry("a", UP_A), entry("b", UP_B)])
        return path

    def configure(self, listeners=None, **options) -> Path:
        self.config.mkdir(parents=True, exist_ok=True)
        return installer.write_autostart_config(
            self.config, listeners or [(18788, UP_A), (18789, UP_B)],
            self.table, self.config / "requests.jsonl", **options,
        )

    def install(self, *args) -> int:
        return installer.main([
            "--zcode-dir", str(self.resources), "--package-dir", str(self.package),
            "--upstream", UP_A, "--extra-upstream", UP_B, "--port", "18788", *args,
        ])

    def owned_artifacts(self) -> None:
        self.configure()
        for name in installer.MANAGED_FILES:
            path = self.config / name
            if name != "autostart.json" and name != "environment-backup.json":
                path.write_text(name, encoding="ascii")
        material = self.config / "tls"
        material.mkdir()
        (material / "server.key").write_text("private test key", encoding="ascii")
        (self.startup / "reqslim proxy.lnk").write_bytes(b"startup test shortcut")
        (self.config / "requests.jsonl").write_bytes(b"test traffic history")

    def test_rewrite_only_anthropic_messages(self) -> None:
        write_table(self.table, [entry("a", UP_A), entry("openai", UP_A, "openai-chat-completions")], [
            entry("template", UP_A, template=True), entry("other-template", UP_A, "other", True),
        ])
        data, touched = wiring.rewrite_config(self.table, {UP_A: LOCAL_A})
        rules = data["config"]["providerConfigRules"]
        self.assertEqual(touched, [("a", LOCAL_A), ("template", LOCAL_A)])
        self.assertEqual(rules["providerRules"][1]["config"]["api"]["baseUrl"], UP_A)
        self.assertEqual(rules["templateRules"][1]["config"]["api"]["baseUrl"], UP_A)

    def test_port_swap_uses_identity_and_preserves_other_proxies_and_metadata(self) -> None:
        path = self.cache(entries=[entry("a", UP_A), entry("b", UP_B), entry("other-proxy", "http://127.0.0.1:19999")])
        backup = path.with_name(path.name + wiring.CACHE_BACKUP_SUFFIX)
        self.assertEqual(wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B}), "rewritten")
        original = backup.read_bytes()
        write_table(path, [entry("b", LOCAL_B), entry("other-proxy", "http://127.0.0.1:19999"),
                           entry("a", LOCAL_A), entry("new-proxy", "http://localhost:20000")])
        data = json.loads(path.read_text(encoding="ascii"))
        data["new-cloud-metadata"] = "preserved"
        path.write_text(json.dumps(data), encoding="ascii")
        mapping = {UP_B: LOCAL_A, UP_A: LOCAL_B}
        self.assertEqual(wiring.rewrite_runtime_cache_file(path, mapping), "rewritten")
        self.assertEqual(urls(path), {"a": LOCAL_B, "b": LOCAL_A,
                                     "other-proxy": "http://127.0.0.1:19999", "new-proxy": "http://localhost:20000"})
        self.assertEqual(json.loads(path.read_text(encoding="ascii"))["new-cloud-metadata"], "preserved")
        before = file_states(path.parent)
        self.assertEqual(wiring.rewrite_runtime_cache_file(path, mapping), "untouched")
        self.assertEqual(file_states(path.parent), before)
        self.assertEqual(backup.read_bytes(), original)

    def test_removing_extra_restores_original_upstream(self) -> None:
        path = self.cache(entries=[entry("a", UP_A), entry("b", UP_B), entry("other", "http://localhost:9999")])
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B})
        self.assertEqual(wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A}), "rewritten")
        self.assertEqual(urls(path), {"a": LOCAL_A, "b": UP_B, "other": "http://localhost:9999"})
        self.assertEqual(wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A}), "untouched")

    def test_partial_mapped_provider_table_is_idempotent(self) -> None:
        path = self.cache(entries=[entry("a", UP_A)])
        mapping = {UP_A: LOCAL_A, UP_B: LOCAL_B}
        self.assertEqual(wiring.rewrite_runtime_cache_file(path, mapping), "rewritten")
        before = file_states(path.parent)
        self.assertEqual(wiring.rewrite_runtime_cache_file(path, mapping), "untouched")
        self.assertEqual(file_states(path.parent), before)
        self.assertEqual(wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_B, UP_B: LOCAL_A}), "rewritten")
        self.assertEqual(urls(path), {"a": LOCAL_B})

    def test_template_remapping_is_not_position_based(self) -> None:
        path = self.cache(entries=[])
        write_table(path, [], [entry("a", UP_A, template=True), entry("b", UP_B, template=True)])
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B})
        write_table(path, [], [entry("b", LOCAL_B, template=True), entry("a", LOCAL_A, template=True)])
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_B, UP_B: LOCAL_A})
        self.assertEqual(urls(path), {"a": LOCAL_B, "b": LOCAL_A})

    def test_runtime_dry_run_does_not_change_permissions_or_create_backups(self) -> None:
        path = self.cache()
        before = file_states(path.parent)
        with mock.patch.object(wiring.os, "chmod", side_effect=AssertionError("dry-run chmod")):
            self.assertEqual(wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A}, dry_run=True), "rewritten")
        self.assertEqual(file_states(path.parent), before)

    def test_restore_collects_failures_and_keeps_list_api(self) -> None:
        path = self.cache()
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A})
        backup = path.with_name(path.name + wiring.CACHE_BACKUP_SUFFIX)
        before = file_states(path.parent)
        errors = []
        with mock.patch.object(wiring.os, "replace", side_effect=PermissionError("locked cache")):
            result = wiring.restore_runtime_caches(errors=errors)
            self.assertEqual(result, [])
            self.assertIn("locked cache", errors[0])
            with self.assertRaisesRegex(OSError, "restoration failed"):
                wiring.restore_runtime_caches(strict=True)
        self.assertEqual(file_states(path.parent), before)
        self.assertEqual(wiring.restore_runtime_caches(), [path])
        self.assertFalse(backup.exists())

    def test_inverse_restore_reports_write_failure_and_does_not_touch_other_protocols(self) -> None:
        path = self.cache(entries=[entry("a", LOCAL_A), entry("other", LOCAL_A, "openai-chat-completions")])
        errors = []
        with mock.patch.object(wiring, "_atomic_write", side_effect=PermissionError("inverse locked")):
            self.assertEqual(wiring.restore_runtime_caches(reverse_mapping={LOCAL_A: UP_A}, errors=errors), [])
        self.assertIn("inverse locked", errors[0])
        self.assertEqual(wiring.restore_runtime_caches(reverse_mapping={LOCAL_A: UP_A}), [path])
        self.assertEqual(urls(path), {"a": UP_A, "other": LOCAL_A})

    def test_corrupt_backup_is_not_restored_over_a_good_table(self) -> None:
        path = self.cache()
        backup = path.with_name(path.name + wiring.CACHE_BACKUP_SUFFIX)
        backup.write_text("[]", encoding="ascii")
        before = path.read_bytes()
        errors = []
        self.assertEqual(wiring.restore_runtime_caches(errors=errors), [])
        self.assertTrue(errors)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(backup.exists())

    def test_uninstall_dry_run_is_read_only(self) -> None:
        self.owned_artifacts()
        path = self.cache()
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A})
        before = file_states(self.root)
        with mock.patch.object(installer, "stop_configured_proxy", side_effect=AssertionError("dry-run stop")), \
             mock.patch.object(Path, "chmod", side_effect=AssertionError("dry-run chmod")), \
             mock.patch.object(Path, "unlink", side_effect=AssertionError("dry-run unlink")), \
             mock.patch.object(installer.shutil, "rmtree", side_effect=AssertionError("dry-run rmtree")):
            self.assertEqual(installer.main(["--uninstall", "--dry-run"]), 0)
        self.assertEqual(file_states(self.root), before)
        self.health.assert_not_called()
        self.shutdown.assert_not_called()
        self.set_environment.assert_not_called()
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_uninstall_failed_restore_preserves_startup_tls_config_and_environment(self) -> None:
        self.owned_artifacts()
        path = self.cache()
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A})
        before = file_states(self.config), file_states(self.startup)
        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "_configured_running", return_value=True), \
             mock.patch.object(installer, "_restart_previous") as restart, \
             mock.patch.object(wiring.os, "replace", side_effect=PermissionError("cache busy")):
            self.assertEqual(installer.uninstall(self.config), 1)
        self.assertEqual((file_states(self.config), file_states(self.startup)), before)
        self.assertTrue(path.with_name(path.name + wiring.CACHE_BACKUP_SUFFIX).exists())
        self.set_environment.assert_not_called()
        restart.assert_called_once()
        self.assertIn("recovery configuration", self.errors.getvalue())

    def test_uninstall_handles_raised_restore_errors_without_removing_artifacts(self) -> None:
        self.owned_artifacts()
        before = file_states(self.config), file_states(self.startup)
        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "restore_runtime_caches", side_effect=OSError("cache inspection failed")):
            self.assertEqual(installer.uninstall(self.config), 1)
        self.assertEqual((file_states(self.config), file_states(self.startup)), before)
        self.set_environment.assert_not_called()

    def test_environment_snapshot_restores_preexisting_values_and_types(self) -> None:
        self.configure()
        initial = {installer.ENV_VAR: {"value": "%TEST_HOME%/providers.json", "type": 2},
                   installer.CA_ENV_VAR: {"value": "", "type": 1}}
        self.environment.update(copy.deepcopy(initial))
        self.assertEqual(installer._install_environment(
            self.config, self.config / "proxied.json", self.config / "bundle.pem",
        ), (True, True))
        saved = installer._load_environment_backup(self.config)
        self.assertEqual({name: value["previous"] for name, value in saved.items()}, initial)
        self.assertEqual(installer._restore_environment(self.config), [])
        self.assertEqual(self.environment, initial)
        self.run.assert_not_called()

    def test_environment_manual_changes_survive_uninstall(self) -> None:
        self.configure()
        self.environment[installer.CA_ENV_VAR] = {"value": "old-ca", "type": 1}
        installer._install_environment(self.config, self.config / "proxied.json", self.config / "bundle.pem")
        manually_changed = {"value": "user-later-providers.json", "type": 1}
        self.environment[installer.ENV_VAR] = manually_changed
        self.assertEqual(installer.uninstall(self.config), 0)
        self.assertEqual(self.environment[installer.ENV_VAR], manually_changed)
        self.assertEqual(self.environment[installer.CA_ENV_VAR], {"value": "old-ca", "type": 1})
        self.assertFalse((self.config / "environment-backup.json").exists())

    def test_uninstall_requires_environment_restoration_readback(self) -> None:
        self.configure()
        installer._install_environment(
            self.config, self.config / "zcode-builtin.proxied.json", self.config / "tls" / "ca-bundle.pem",
        )
        (self.config / "tls").mkdir()
        (self.config / "tls" / "server.key").write_text("synthetic key material", encoding="ascii")
        self.set_environment.side_effect = lambda name, value: True
        self.assertEqual(installer.uninstall(self.config), 1)
        self.assertTrue((self.config / "autostart.json").is_file())
        self.assertTrue((self.config / "environment-backup.json").is_file())
        self.assertTrue((self.config / "tls" / "server.key").is_file())

    def test_environment_reinstall_retains_original_and_no_tls_restores_old_ca(self) -> None:
        self.configure()
        initial = {installer.ENV_VAR: {"value": "before.json", "type": 1},
                   installer.CA_ENV_VAR: {"value": "%OLD_CA%", "type": 2}}
        self.environment.update(copy.deepcopy(initial))
        installer._install_environment(self.config, self.config / "one.json", self.config / "ca-one.pem")
        installer._install_environment(self.config, self.config / "two.json", self.config / "ca-two.pem")
        saved = installer._load_environment_backup(self.config)
        self.assertEqual({name: value["previous"] for name, value in saved.items()}, initial)
        installer._install_environment(self.config, self.config / "three.json", None)
        self.assertEqual(self.environment[installer.CA_ENV_VAR], initial[installer.CA_ENV_VAR])
        self.assertEqual(installer._restore_environment(self.config), [])
        self.assertEqual(self.environment, initial)

    def test_environment_reinstall_saves_intervening_manual_values(self) -> None:
        self.configure()
        installer._install_environment(self.config, self.config / "one.json", None)
        new_value = {"value": "manual.json", "type": 1}
        self.environment[installer.ENV_VAR] = new_value.copy()
        installer._install_environment(self.config, self.config / "two.json", None)
        installer._restore_environment(self.config)
        self.assertEqual(self.environment[installer.ENV_VAR], new_value)
        self.assertNotIn(installer.CA_ENV_VAR, self.environment)

    def test_registry_helpers_are_tested_with_fakes_only(self) -> None:
        fake_key = mock.MagicMock()
        registry = types.SimpleNamespace(
            HKEY_CURRENT_USER=object(), REG_SZ=1, REG_EXPAND_SZ=2,
            OpenKey=mock.Mock(return_value=fake_key),
            QueryValueEx=mock.Mock(return_value=("%PREVIOUS%", 2)),
        )
        with mock.patch.dict(sys.modules, {"winreg": registry}):
            self.assertEqual(REAL_READ_ENVIRONMENT(installer.ENV_VAR), {"value": "%PREVIOUS%", "type": 2})
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(REAL_SET_ENVIRONMENT(installer.ENV_VAR, {"value": "%PREVIOUS%", "type": 2}))
            self.assertIn("REG_EXPAND_SZ", run.call_args.args[0])
            self.assertTrue(REAL_SET_ENVIRONMENT(installer.ENV_VAR, None))
            self.assertEqual(run.call_args.args[0][:2], ["reg", "delete"])

    def test_resources_argument_finds_windows_executable(self) -> None:
        exe = self.resources.parent / "ZCode.exe"
        exe.write_bytes(b"test executable, never launched")
        found = installer.find_zcode(str(self.resources))
        self.assertEqual(found.exe, exe)
        self.assertEqual(found.resources, self.resources)

    def test_malformed_old_autostart_shapes_allow_repair_and_uninstall(self) -> None:
        for malformed in ([], None, "bad", 7, {"listeners": 8}, {"listeners": [None, {}, "ab", [True, UP_A]]}):
            with self.subTest(shape=malformed):
                self.config.mkdir(parents=True, exist_ok=True)
                (self.config / "autostart.json").write_text(json.dumps(malformed), encoding="ascii")
                self.assertEqual(installer.configured_listeners(self.config), [])
                self.assertEqual(installer.configured_reverse_mapping(self.config), {})
                self.assertEqual(self.install("--no-tls", "--no-autostart"), 0)
                self.assertTrue(installer.configured_listeners(self.config))
                (self.config / "autostart.json").write_text(json.dumps(malformed), encoding="ascii")
                self.assertEqual(installer.uninstall(self.config), 0)
        self.set_environment.assert_not_called()

    def test_fresh_install_startup_failure_rolls_back_files_caches_and_backups(self) -> None:
        self.cache()
        before = file_states(self.root)
        with mock.patch.object(installer, "stop_configured_proxy", return_value=True) as stop, \
             mock.patch.object(installer, "start_proxy_now", return_value=False), \
             mock.patch.object(installer, "install_autostart_windows") as register:
            self.assertEqual(self.install("--no-tls"), 1)
        self.assertEqual(file_states(self.root), before)
        self.assertEqual(stop.call_count, 2)
        register.assert_not_called()
        self.set_environment.assert_not_called()

    def test_reinstall_startup_failure_restores_old_tls_mapping_and_running_instance(self) -> None:
        self.owned_artifacts()
        path = self.cache()
        wiring.rewrite_runtime_cache_file(path, {UP_A: "http://127.0.0.1:18888", UP_B: "http://127.0.0.1:18889"})
        self.configure([(18888, UP_A), (18889, UP_B)], ttl=19, cache=False)
        for name in ("start-zcode.bat", "zcode-stats.bat", "zcode-report.bat"):
            (self.package / name).write_bytes(b"old manual helper")
        before = file_states(self.root)

        def fake_tls(config_dir):
            directory = config_dir / "tls"
            for name in ("ca.crt", "ca-bundle.pem", "server.crt", "server.key"):
                (directory / name).write_bytes(b"new test TLS")
            return tls.TlsFiles(directory / "ca.crt", directory / "ca-bundle.pem", directory / "server.crt", directory / "server.key")

        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "_configured_running", return_value=True), \
             mock.patch.object(installer, "ensure_tls", side_effect=fake_tls), \
             mock.patch.object(installer, "start_proxy_now", side_effect=[False, True]) as start, \
             mock.patch.object(installer, "install_autostart_windows") as register:
            self.assertEqual(self.install(), 1)
        self.assertEqual(file_states(self.root), before)
        self.assertEqual(start.call_count, 2)
        self.assertEqual(start.call_args.args[3:5], (18888, "http"))
        register.assert_not_called()
        self.set_environment.assert_not_called()

    def test_rollback_repairs_new_cache_created_by_startup_watcher(self) -> None:
        self.cache()
        new_path = None

        def failed_start(*args, **kwargs):
            nonlocal new_path
            new_path = self.cache("new")
            wiring.rewrite_runtime_cache_file(new_path, {UP_A: "http://127.0.0.1:18788", UP_B: "http://127.0.0.1:18789"})
            return False

        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "start_proxy_now", side_effect=failed_start):
            self.assertEqual(self.install("--no-tls"), 1)
        self.assertEqual(urls(new_path), {"a": UP_A, "b": UP_B})
        self.assertFalse(new_path.with_name(new_path.name + wiring.CACHE_BACKUP_SUFFIX).exists())
        self.assertFalse(new_path.with_name(new_path.name + wiring.CACHE_STATE_SUFFIX).exists())
        self.assertFalse((self.config / "autostart.json").exists())

    def test_blocked_rollback_saves_local_recovery_snapshot(self) -> None:
        old = self.configure().read_bytes()
        with mock.patch.object(installer, "stop_configured_proxy", side_effect=[True, False]), \
             mock.patch.object(installer, "start_proxy_now", return_value=False), \
             mock.patch.object(installer, "_restart_previous") as restart:
            self.assertEqual(self.install("--no-tls"), 1)
        recovery = list(self.config.glob("install-recovery-*.json"))
        self.assertEqual(len(recovery), 1)
        saved = json.loads(recovery[0].read_text(encoding="ascii"))
        self.assertEqual(base64.b64decode(saved["files"][str(self.config / "autostart.json")]["content"]), old)
        restart.assert_not_called()
        self.set_environment.assert_not_called()

    def test_file_write_failure_rolls_back_before_registration(self) -> None:
        self.cache()
        before = file_states(self.root)
        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "generate_proxied_table", side_effect=OSError("output locked")), \
             mock.patch.object(installer, "install_autostart_windows") as register:
            self.assertEqual(self.install("--no-tls"), 1)
        self.assertEqual(file_states(self.root), before)
        register.assert_not_called()

    def test_snapshot_is_taken_after_watchers_stop_and_failure_restarts_old_instance(self) -> None:
        self.configure()
        before = file_states(self.root)
        order = []

        def stopped(*args, **kwargs):
            order.append("stopped")
            return True

        def failed_snapshot(*args, **kwargs):
            order.append("snapshot")
            raise OSError("snapshot source locked")

        with mock.patch.object(installer, "stop_configured_proxy", side_effect=stopped), \
             mock.patch.object(installer, "_configured_running", return_value=True), \
             mock.patch.object(installer, "_InstallSnapshot", side_effect=failed_snapshot), \
             mock.patch.object(installer, "_restart_previous") as restart:
            self.assertEqual(self.install("--no-tls"), 1)
        self.assertEqual(order, ["stopped", "snapshot"])
        self.assertEqual(file_states(self.root), before)
        restart.assert_called_once_with(self.config, self.package)

    def test_successful_start_precedes_persistent_registration(self) -> None:
        self.cache()
        order = []

        def started(*args, **kwargs):
            order.append("started")
            self.assertFalse((self.config / "environment-backup.json").exists())
            return True

        def registered(*args, **kwargs):
            order.append("registered")
            return self.startup / "reqslim proxy.lnk", None, True, True

        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "start_proxy_now", side_effect=started), \
             mock.patch.object(installer, "install_autostart_windows", side_effect=registered):
            self.assertEqual(self.install("--no-tls", "--no-cache", "--no-coalesce", "--ttl", "17"), 0)
        self.assertEqual(order, ["started", "registered"])
        spec = load_autostart_config(str(self.config / "autostart.json"))
        self.assertEqual(spec.ttl, 17)
        self.assertFalse(spec.cache_enabled)
        self.assertFalse(spec.coalesce)

    def test_start_proxy_requires_all_configured_listener_mappings(self) -> None:
        config = self.configure()
        expected = {"18788": UP_A, "18789": UP_B}
        good = self.review_health(config)
        for mode in ("secondary-missing", "wrong-mapping", "wrong-tls", "secondary-not-ready", "healthy"):
            with self.subTest(mode=mode):
                process = mock.Mock()
                process.poll.return_value = None

                def health(port, **kwargs):
                    if mode == "secondary-missing" and port == 18789:
                        return None
                    result = copy.deepcopy(good)
                    result.update({
                        "upstreams": {"18788": UP_B, "18789": UP_A} if mode == "wrong-mapping" else expected,
                        "tls": mode == "wrong-tls", "ready": not (mode == "secondary-not-ready" and port == 18789),
                    })
                    return result

                with mock.patch.object(subprocess, "Popen", return_value=process) as popen, \
                     mock.patch.object(installer.time, "sleep"), \
                     mock.patch.object(installer, "health_on_port", side_effect=health) as probe:
                    self.assertEqual(installer.start_proxy_now(
                        sys.executable, self.config / "missing.pyw", self.package, 18788, config=config,
                    ), mode == "healthy")
                self.assertEqual({call.args[0] for call in probe.call_args_list}, {18788, 18789})
                self.assertIn("--autostart-config", popen.call_args.args[0])
                self.assertEqual(process.terminate.call_count, 0 if mode == "healthy" else 1)

    def test_start_proxy_preserves_legacy_signature_and_supports_custom_config_name(self) -> None:
        config = self.configure().rename(self.config / "custom.json")
        process = mock.Mock()
        process.poll.return_value = None
        self.health.return_value = self.review_health(config)
        with mock.patch.object(subprocess, "Popen", return_value=process), mock.patch.object(installer.time, "sleep"):
            self.assertTrue(installer.start_proxy_now(sys.executable, self.config / "missing.pyw", self.package, 18788, config=config))
            config.unlink()
            self.assertTrue(installer.start_proxy_now(sys.executable, self.config / "missing.pyw", self.package, 18788))
        process.terminate.assert_not_called()

    def test_start_proxy_waits_until_every_listener_is_ready(self) -> None:
        config = self.configure()
        process = mock.Mock()
        process.poll.return_value = None
        good = self.review_health(config)
        responses = [copy.deepcopy(good) for _ in range(4)]
        responses[1]["ready"] = False
        with mock.patch.object(subprocess, "Popen", return_value=process), \
             mock.patch.object(installer.time, "sleep"), \
             mock.patch.object(installer, "health_on_port", side_effect=responses) as health:
            self.assertTrue(installer.start_proxy_now(sys.executable, self.config / "missing.pyw", self.package, 18788))
        self.assertEqual(health.call_count, 4)
        process.terminate.assert_not_called()

    def test_stop_recognizes_a_live_instance_that_is_not_ready(self) -> None:
        self.configure()
        self.health.return_value = {"ok": True, "ready": False, "tls": False,
                                    "upstreams": {"18788": UP_A, "18789": UP_B}}
        with mock.patch.object(installer, "stop_running_proxy", return_value=True) as shutdown:
            self.assertTrue(installer.stop_configured_proxy(self.config))
        shutdown.assert_called_once()

    def test_start_proxy_rejects_malformed_config_without_spawning(self) -> None:
        self.configure().write_text("[]", encoding="ascii")
        self.assertFalse(installer.start_proxy_now(sys.executable, self.config / "missing.pyw", self.package, 18788))
        self.popen.assert_not_called()

    def test_manual_launchers_use_autostart_config_including_nondefault_options(self) -> None:
        config = self.configure(ttl=21, max_cache=7, cache=False, coalesce=False, coalesce_wait=8, failure_ttl=0)
        for writer in (installer.write_windows_launcher, installer.write_posix_launcher):
            for explicit in (True, False):
                with self.subTest(writer=writer.__name__, explicit=explicit):
                    path = writer(self.package, None, [(18788, UP_A), (18789, UP_B)], config=config if explicit else None)
                    text = path.read_text(encoding="utf-8")
                    self.assertIn("--autostart --autostart-config", text)
                    self.assertNotIn("--listen", text)
                    self.assertNotIn("--record", text)
                    if explicit:
                        self.assertIn(str(self.config), text)
        spec = load_autostart_config(str(config))
        self.assertEqual((spec.ttl, spec.max_cache, spec.coalesce_wait, spec.failure_ttl), (21, 7, 8, 0))
        self.assertFalse(spec.cache_enabled)
        self.assertFalse(spec.coalesce)
        self.assertEqual(spec.zcode_table, str(self.table))
        self.assertEqual(spec.config_dir, str(self.config))
        pyw = installer.write_autostart_pyw(self.config, self.package)
        self.assertIn("--autostart-config", pyw.read_text(encoding="utf-8"))

    def test_generated_launcher_health_check_requires_tls_mapping_and_readiness(self) -> None:
        config = self.configure(tls=self.review_tls(self.config))
        good = self.review_health(config)
        check = installer._launcher_health_check([(18788, UP_A), (18789, UP_B)], "https")
        for changes, code in (
            ({}, 0), ({"ready": False}, 1), ({"tls": False}, 1),
            ({"upstreams": {"18788": UP_A}}, 1),
        ):
            health = copy.deepcopy(good)
            health.update(changes)
            with self.subTest(health=health), \
                 mock.patch.object(sys, "argv", ["-c", str(config)]), \
                 mock.patch("reqslim.zcode.__main__.health_on_port", return_value=health) as probe, \
                 self.assertRaises(SystemExit) as result:
                exec(check, {})
            self.assertEqual(result.exception.code, code)
            self.assertEqual({call.args[0] for call in probe.call_args_list}, {18788, 18789} if code == 0 else {18788})

    def test_invalid_tls_certificate_is_loaded_with_ssl_and_not_reused(self) -> None:
        directory = self.config / "tls"
        directory.mkdir(parents=True)
        for name in ("ca.crt", "server.crt", "server.key"):
            (directory / name).write_text("not a certificate or key", encoding="ascii")
        with mock.patch.object(tls, "make_context", wraps=tls.make_context) as make_context, \
             mock.patch.object(tls, "_openssl", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "openssl was not found"):
                tls.ensure_tls(self.config)
        make_context.assert_called_once_with(directory / "server.crt", directory / "server.key")
        self.run.assert_not_called()

    def test_tls_expiration_verification_failure_triggers_regeneration(self) -> None:
        files = tls.TlsFiles(*(self.root / name for name in ("ca.crt", "bundle.pem", "server.crt", "server.key")))
        server_context = mock.Mock()
        client_context = mock.Mock()
        client_context.wrap_bio.return_value.do_handshake.side_effect = ssl.SSLCertVerificationError("certificate has expired")
        with mock.patch.object(tls.ssl, "SSLContext", side_effect=[server_context, client_context]):
            self.assertFalse(tls._valid_tls(files))
        server_context.load_cert_chain.assert_called_once_with(str(files.cert), str(files.key))
        client_context.load_verify_locations.assert_called_once_with(cafile=str(files.ca))

        def generate(openssl, args, cwd):
            for flag in ("-out", "-keyout"):
                if flag in args:
                    (cwd / args[args.index(flag) + 1]).write_text("new TLS test material", encoding="ascii")

        with mock.patch.object(tls, "_valid_tls", side_effect=[False, True]), \
             mock.patch.object(tls, "_openssl", return_value="mock-openssl"), \
             mock.patch.object(tls, "_run", side_effect=generate) as run, \
             mock.patch.object(tls, "_system_ca_pem", return_value=""):
            generated = tls.ensure_tls(self.config)
        self.assertEqual(run.call_count, 3)
        self.assertTrue(generated.cert.exists())
        self.assertTrue(generated.key.exists())
        self.assertIn("new TLS test material", generated.bundle.read_text(encoding="ascii"))
        self.run.assert_not_called()

    def test_failed_tls_regeneration_can_be_retried(self) -> None:
        def generate(openssl, args, cwd):
            for flag in ("-out", "-keyout"):
                if flag in args:
                    (cwd / args[args.index(flag) + 1]).write_text("TLS retry test material", encoding="ascii")

        with mock.patch.object(tls, "_valid_tls", side_effect=[False, False, False, True]), \
             mock.patch.object(tls, "_openssl", return_value="mock-openssl"), \
             mock.patch.object(tls, "_run", side_effect=generate) as run, \
             mock.patch.object(tls, "_system_ca_pem", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "generated TLS"):
                tls.ensure_tls(self.config)
            files = tls.ensure_tls(self.config)
        self.assertEqual(run.call_count, 6)
        self.assertTrue(files.cert.exists())
        self.assertTrue(files.key.exists())
        self.run.assert_not_called()

    def review_tls(self, config_dir: Path) -> tls.TlsFiles:
        directory = config_dir / "tls"
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("ca.crt", "ca-bundle.pem", "server.crt", "server.key"):
            (directory / name).write_bytes(b"review TLS test material")
        return tls.TlsFiles(directory / "ca.crt", directory / "ca-bundle.pem", directory / "server.crt", directory / "server.key")

    def review_health(self, config: Path) -> dict:
        spec = load_autostart_config(str(config))
        return {
            "ok": True, "ready": True, "version": zcode_cli.__version__,
            "upstreams": {str(port): url for port, url in spec.listeners},
            "tls": bool(spec.tls_cert and spec.tls_key),
            "settings": zcode_cli._autostart_settings(spec, "127.0.0.1", spec.record),
        }

    def review_shortcut(self, script):
        (self.startup / "reqslim proxy.lnk").write_bytes(b"review startup shortcut")
        return subprocess.CompletedProcess([], 0, "", "")

    def review_initial_environment(self) -> dict:
        initial = {
            installer.ENV_VAR: {"value": "immediate previous providers.json", "type": 2},
            installer.CA_ENV_VAR: {"value": "immediate previous ca.pem", "type": 1},
        }
        self.environment.update(copy.deepcopy(initial))
        return initial

    def review_registration_failure(self, failed_name: str) -> None:
        initial = self.review_initial_environment()
        before = file_states(self.root)
        set_environment = self.set_environment.side_effect
        desired = {
            installer.ENV_VAR: {"value": str(self.config / "zcode-builtin.proxied.json"), "type": 1},
            installer.CA_ENV_VAR: {"value": str(self.config / "tls" / "ca-bundle.pem"), "type": 1},
        }

        def set_value(name, value):
            if name == failed_name and value == desired[name]:
                return False
            return set_environment(name, value)

        self.set_environment.side_effect = set_value
        with mock.patch.object(installer, "ensure_tls", side_effect=self.review_tls), \
             mock.patch.object(installer, "rewrite_runtime_caches", return_value=[]), \
             mock.patch.object(installer, "_powershell", side_effect=self.review_shortcut), \
             mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "start_proxy_now", return_value=True):
            self.assertEqual(self.install(), 1)
        self.assertEqual(self.environment, initial)
        self.assertEqual(file_states(self.root), before)

    def test_review_provider_registration_failure_is_nonzero_and_rolls_back_ca(self) -> None:
        self.review_registration_failure(installer.ENV_VAR)

    def test_review_ca_registration_failure_is_nonzero_and_rolls_back_provider(self) -> None:
        self.review_registration_failure(installer.CA_ENV_VAR)

    def review_environment_rollback_failure(self, failure: str) -> None:
        initial = self.review_initial_environment()
        old_config = self.configure().read_bytes()
        cache = self.cache("review-before-install")
        sidecar = cache.with_name(cache.name + wiring.CACHE_STATE_SUFFIX)
        sidecar.write_bytes(b"previous opaque cache ownership")
        old_backup = {
            name: {"previous": {"value": "earliest " + name, "type": 1}, "installed": state}
            for name, state in initial.items()
        }
        backup_path = self.config / "environment-backup.json"
        backup_path.write_text(json.dumps(old_backup), encoding="ascii")
        old_backup_bytes = backup_path.read_bytes()
        set_environment = self.set_environment.side_effect
        desired_provider = {"value": str(self.config / "zcode-builtin.proxied.json"), "type": 1}
        desired_ca = {"value": str(self.config / "tls" / "ca-bundle.pem"), "type": 1}
        manual = {"value": desired_provider["value"], "type": 2}
        watcher_cache = None
        watcher_sidecar = None
        watcher_state = None

        def set_value(name, value):
            nonlocal watcher_cache, watcher_sidecar, watcher_state
            if name == installer.CA_ENV_VAR and value == desired_ca:
                sidecar.write_bytes(b"current opaque cache ownership")
                watcher_cache = self.cache("review-watcher-created")
                wiring.rewrite_runtime_cache_file(watcher_cache, {UP_A: LOCAL_A, UP_B: LOCAL_B})
                watcher_sidecar = watcher_cache.with_name(watcher_cache.name + wiring.CACHE_STATE_SUFFIX)
                watcher_state = watcher_sidecar.read_bytes()
                if failure == "manual":
                    self.environment[installer.ENV_VAR] = manual.copy()
                raise OSError("review CA registration exception")
            if name == installer.ENV_VAR and value == initial[name]:
                if failure == "raise":
                    raise OSError("review environment rollback exception")
                if failure in ("false", "unconfirmed"):
                    return failure == "unconfirmed"
            return set_environment(name, value)

        self.set_environment.side_effect = set_value
        restore_snapshot = installer._InstallSnapshot.restore
        with mock.patch.object(installer, "ensure_tls", side_effect=self.review_tls), \
             mock.patch.object(installer, "rewrite_runtime_caches", return_value=[]), \
             mock.patch.object(installer, "_powershell", side_effect=self.review_shortcut), \
             mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "start_proxy_now", return_value=True), \
             mock.patch.object(installer._InstallSnapshot, "restore", autospec=True, side_effect=restore_snapshot) as restore:
            self.assertEqual(self.install(), 1)
        restore.assert_not_called()
        self.assertEqual(self.environment[installer.ENV_VAR], manual if failure == "manual" else desired_provider)
        for name in ("autostart.json", "zcode-builtin.proxied.json", "environment-backup.json", "proxy_autostart.pyw"):
            self.assertTrue((self.config / name).is_file(), name)
        self.assertTrue((self.config / "tls" / "server.key").is_file())
        self.assertTrue((self.config / "tls" / "ca-bundle.pem").is_file())
        self.assertTrue((self.startup / "reqslim proxy.lnk").is_file())
        self.assertEqual(sidecar.read_bytes(), b"current opaque cache ownership")
        self.assertEqual(watcher_sidecar.read_bytes(), watcher_state)
        self.assertEqual(urls(watcher_cache), {"a": LOCAL_A, "b": LOCAL_B})
        manifests = [json.loads(path.read_text(encoding="ascii")) for path in self.config.glob("install-recovery-*.json")]
        self.assertTrue(manifests)
        manifest = manifests[-1]
        self.assertEqual(manifest["previous_environment"], initial)
        self.assertEqual(base64.b64decode(manifest["files"][str(self.config / "autostart.json")]["content"]), old_config)
        self.assertEqual(base64.b64decode(manifest["files"][str(backup_path)]["content"]), old_backup_bytes)
        self.assertEqual(base64.b64decode(manifest["files"][str(sidecar)]["content"]), b"previous opaque cache ownership")
        self.assertIsNone(manifest["files"][str(watcher_sidecar)])
        self.assertIn(UP_A.encode("ascii"), base64.b64decode(manifest["files"][str(watcher_cache)]["content"]))
        if failure == "manual":
            self.assertNotIn((installer.ENV_VAR, initial[installer.ENV_VAR]), self.environment_writes)

    def test_review_failed_environment_rollback_retains_current_materials_and_previous_state(self) -> None:
        self.review_environment_rollback_failure("false")

    def test_review_raised_environment_rollback_retains_current_materials_and_previous_state(self) -> None:
        self.review_environment_rollback_failure("raise")

    def test_review_success_result_without_environment_readback_retains_materials(self) -> None:
        self.review_environment_rollback_failure("unconfirmed")

    def test_review_later_manual_environment_change_is_not_overwritten_or_dangled(self) -> None:
        self.review_environment_rollback_failure("manual")

    def test_review_environment_changed_while_stopping_keeps_current_resources(self) -> None:
        initial = self.review_initial_environment()
        set_environment = self.set_environment.side_effect
        desired_ca = {"value": str(self.config / "tls" / "ca-bundle.pem"), "type": 1}
        manual = {"value": str(self.config / "zcode-builtin.proxied.json"), "type": 2}
        stops = []

        def set_value(name, value):
            if name == installer.CA_ENV_VAR and value == desired_ca:
                return False
            return set_environment(name, value)

        def stopped(*args, **kwargs):
            stops.append(True)
            if len(stops) == 2:
                self.assertEqual(self.environment, initial)
                self.environment[installer.ENV_VAR] = manual.copy()
            return True

        self.set_environment.side_effect = set_value
        with mock.patch.object(installer, "ensure_tls", side_effect=self.review_tls), \
             mock.patch.object(installer, "rewrite_runtime_caches", return_value=[]), \
             mock.patch.object(installer, "_powershell", side_effect=self.review_shortcut), \
             mock.patch.object(installer, "stop_configured_proxy", side_effect=stopped), \
             mock.patch.object(installer, "start_proxy_now", return_value=True):
            self.assertEqual(self.install(), 1)
        self.assertEqual(self.environment[installer.ENV_VAR], manual)
        self.assertTrue((self.config / "zcode-builtin.proxied.json").is_file())
        self.assertTrue((self.config / "tls" / "server.key").is_file())
        self.assertTrue((self.config / "environment-backup.json").is_file())
        manifest = json.loads(next(self.config.glob("install-recovery-*.json")).read_text(encoding="ascii"))
        self.assertEqual(manifest["previous_environment"], initial)

    def test_review_recovery_snapshot_is_durable_before_any_environment_write(self) -> None:
        initial = self.review_initial_environment()
        set_environment = self.set_environment.side_effect
        seen = []

        def set_value(name, value):
            manifests = [json.loads(path.read_text(encoding="ascii")) for path in self.config.glob("install-recovery-*.json")]
            self.assertTrue(manifests)
            self.assertEqual(manifests[-1]["previous_environment"], initial)
            self.assertIn(str(self.config / "autostart.json"), manifests[-1]["files"])
            seen.append(name)
            return set_environment(name, value)

        self.set_environment.side_effect = set_value
        with mock.patch.object(installer, "ensure_tls", side_effect=self.review_tls), \
             mock.patch.object(installer, "rewrite_runtime_caches", return_value=[]), \
             mock.patch.object(installer, "_powershell", side_effect=self.review_shortcut), \
             mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "start_proxy_now", return_value=True):
            self.assertEqual(self.install(), 0)
        self.assertEqual(seen, [installer.ENV_VAR, installer.CA_ENV_VAR])
        self.assertFalse(list(self.config.glob("install-recovery-*.json")))

    def test_review_recovery_persistence_failure_prevents_registration(self) -> None:
        self.review_initial_environment()
        before = file_states(self.root)
        with mock.patch.object(installer, "ensure_tls", side_effect=self.review_tls), \
             mock.patch.object(installer, "rewrite_runtime_caches", return_value=[]), \
             mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "start_proxy_now", return_value=True), \
             mock.patch.object(installer._InstallSnapshot, "save_recovery", side_effect=OSError("recovery disk unavailable")), \
             mock.patch.object(installer, "install_autostart_windows", return_value=(self.startup / "reqslim proxy.lnk", None, True, True)) as register:
            self.assertEqual(self.install(), 1)
        register.assert_not_called()
        self.set_environment.assert_not_called()
        self.assertEqual(file_states(self.root), before)

    def test_review_start_proxy_rejects_old_version_settings_and_watcher_mismatch(self) -> None:
        config = self.configure(ttl=17, cache=False, coalesce=False)
        good = self.review_health(config)
        for mode in ("version", "settings", "watcher", "secondary-watcher", "healthy"):
            with self.subTest(mode=mode):
                bad = copy.deepcopy(good)
                if mode == "version":
                    bad["version"] = "old review version"
                elif mode == "settings":
                    bad["settings"]["cache"] = True
                elif mode in ("watcher", "secondary-watcher"):
                    bad["settings"]["watchers"] = None
                process = mock.Mock()
                process.poll.return_value = None

                def health(port, **kwargs):
                    return good if mode == "secondary-watcher" and port == 18788 else bad

                with mock.patch.object(subprocess, "Popen", return_value=process), \
                     mock.patch.object(installer.time, "sleep"), \
                     mock.patch.object(installer, "health_on_port", side_effect=health):
                    self.assertEqual(installer.start_proxy_now(sys.executable, self.config / "missing.pyw", self.package, 18788), mode == "healthy")

    def test_review_launcher_health_check_uses_full_matcher_and_explicit_config_argument(self) -> None:
        config = self.configure(ttl=17, cache=False).rename(self.config / "review configuration with spaces.json")
        good = self.review_health(config)
        check = installer._launcher_health_check([(18788, UP_A), (18789, UP_B)], "http")
        for mode in ("version", "settings", "watcher", "healthy"):
            with self.subTest(mode=mode):
                health = copy.deepcopy(good)
                if mode == "version":
                    health["version"] = "old review version"
                elif mode == "settings":
                    health["settings"]["cache"] = True
                elif mode == "watcher":
                    health["settings"]["watchers"] = None
                with mock.patch.object(sys, "argv", ["-c", str(config)]), \
                     mock.patch.object(zcode_cli, "health_on_port", return_value=health), \
                     self.assertRaises(SystemExit) as result:
                    exec(check, {})
                self.assertEqual(result.exception.code, 0 if mode == "healthy" else 1)
        for writer, argument in ((installer.write_windows_launcher, '"%AUTOSTART_CONFIG%"'),
                                 (installer.write_posix_launcher, '"$AUTOSTART_CONFIG"')):
            path = writer(self.package, None, [(18788, UP_A), (18789, UP_B)], config=config)
            probes = [line for line in path.read_text(encoding="utf-8").splitlines() if " -c " in line and "import reqslim.zcode" not in line]
            self.assertTrue(probes)
            self.assertTrue(all(argument in line for line in probes))

    def test_review_install_passes_previous_mapping_before_overwriting_config(self) -> None:
        self.configure([(18888, UP_A), (18889, UP_B)])
        previous = installer.configured_reverse_mapping(self.config)
        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
             mock.patch.object(installer, "rewrite_runtime_caches", return_value=[]) as rewrite:
            self.assertEqual(self.install("--no-tls", "--no-autostart"), 0)
        self.assertEqual(rewrite.call_args.kwargs.get("previous_mapping"), previous)

    def test_review_install_migrates_legacy_cache_without_sidecar(self) -> None:
        self.configure([(18888, UP_A), (18889, UP_B)])
        path = self.cache(entries=[
            entry("a", "http://127.0.0.1:18888"), entry("b", "https://127.0.0.1:18889"),
            entry("other", "http://localhost:19999"),
        ])
        self.assertFalse(path.with_name(path.name + wiring.CACHE_STATE_SUFFIX).exists())
        with mock.patch.object(installer, "stop_configured_proxy", return_value=True):
            self.assertEqual(self.install("--no-tls", "--no-autostart"), 0)
        self.assertEqual(urls(path), {"a": "http://127.0.0.1:18788", "b": "http://127.0.0.1:18789",
                                     "other": "http://localhost:19999"})
        self.assertTrue(path.with_name(path.name + wiring.CACHE_STATE_SUFFIX).is_file())
        self.set_environment.assert_not_called()

    def test_review_snapshot_restores_existing_orphan_sidecars_and_removes_new_ones(self) -> None:
        path = self.cache()
        sidecar = path.with_name(path.name + wiring.CACHE_STATE_SUFFIX)
        sidecar.write_bytes(b"original opaque sidecar bytes")
        orphan = self.cache("orphan")
        orphan.unlink()
        orphan_state = orphan.with_name(orphan.name + wiring.CACHE_STATE_SUFFIX)
        orphan_state.write_bytes(b"original orphan sidecar bytes")
        snapshot = installer._InstallSnapshot(self.config, self.package)
        sidecar.write_bytes(b"new sidecar bytes")
        orphan_state.write_bytes(b"new orphan sidecar bytes")
        new_path = self.cache("watcher-new")
        backup = new_path.with_name(new_path.name + wiring.CACHE_BACKUP_SUFFIX)
        wiring.rewrite_runtime_cache_file(new_path, {UP_A: LOCAL_A, UP_B: LOCAL_B})
        new_state = new_path.with_name(new_path.name + wiring.CACHE_STATE_SUFFIX)
        self.assertEqual(snapshot.restore({LOCAL_A: UP_A, LOCAL_B: UP_B}), [])
        self.assertEqual(sidecar.read_bytes(), b"original opaque sidecar bytes")
        self.assertEqual(orphan_state.read_bytes(), b"original orphan sidecar bytes")
        self.assertFalse(new_state.exists())
        self.assertFalse(backup.exists())
        self.assertEqual(urls(new_path), {"a": UP_A, "b": UP_B})

    def test_review_late_mixed_backup_rollback_uses_current_ownership(self) -> None:
        snapshot = installer._InstallSnapshot(self.config, self.package)
        path = self.cache("late-mixed", [entry("a", LOCAL_A), entry("b", UP_B)])
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B}, previous_mapping={LOCAL_A: UP_A})
        self.assertEqual(snapshot.restore({LOCAL_A: UP_A, LOCAL_B: UP_B}), [])
        self.assertEqual(urls(path), {"a": UP_A, "b": UP_B})
        self.assertFalse(path.with_name(path.name + wiring.CACHE_BACKUP_SUFFIX).exists())
        self.assertFalse(path.with_name(path.name + wiring.CACHE_STATE_SUFFIX).exists())

    def test_review_late_cache_rollback_preserves_new_rows_and_metadata(self) -> None:
        snapshot = installer._InstallSnapshot(self.config, self.package)
        path = self.cache("late-new-rows")
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B})
        write_table(path, [entry("a", LOCAL_A), entry("b", LOCAL_B), entry("later", UP_A)])
        data = json.loads(path.read_text(encoding="ascii"))
        data["revision"] = "later-than-backup"
        path.write_text(json.dumps(data), encoding="ascii")
        self.assertEqual(snapshot.restore({LOCAL_A: UP_A, LOCAL_B: UP_B}), [])
        self.assertEqual(urls(path), {"a": UP_A, "b": UP_B, "later": UP_A})
        self.assertEqual(json.loads(path.read_text(encoding="ascii"))["revision"], "later-than-backup")

    def test_review_late_cache_plan_refreshes_after_recovery_snapshot(self) -> None:
        snapshot = installer._InstallSnapshot(self.config, self.package)
        path = self.cache("late-refreshed")
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B})
        snapshot.save_recovery(self.config, reverse_mapping={LOCAL_A: UP_A, LOCAL_B: UP_B})
        write_table(path, [entry("a", LOCAL_A), entry("b", LOCAL_B), entry("later", UP_B)])
        self.assertEqual(snapshot.restore({LOCAL_A: UP_A, LOCAL_B: UP_B}), [])
        self.assertEqual(urls(path), {"a": UP_A, "b": UP_B, "later": UP_B})

    def test_review_late_cache_write_failure_keeps_current_materials(self) -> None:
        snapshot = installer._InstallSnapshot(self.config, self.package)
        self.owned_artifacts()
        path = self.cache("late-locked")
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B})
        before = file_states(self.root)
        original_write = installer._write_bytes

        def write(candidate, content):
            if candidate == path:
                raise OSError("late cache locked")
            return original_write(candidate, content)

        with mock.patch.object(installer, "_write_bytes", side_effect=write):
            errors = snapshot.restore({LOCAL_A: UP_A, LOCAL_B: UP_B})
        self.assertTrue(errors)
        self.assertEqual(file_states(self.root), before)

    def test_review_invalid_late_cache_state_blocks_all_cleanup(self) -> None:
        snapshot = installer._InstallSnapshot(self.config, self.package)
        self.owned_artifacts()
        path = self.cache("late-invalid")
        wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B})
        path.with_name(path.name + wiring.CACHE_STATE_SUFFIX).write_text("{", encoding="ascii")
        before = file_states(self.root)
        errors = snapshot.restore({LOCAL_A: UP_A, LOCAL_B: UP_B})
        self.assertTrue(errors)
        self.assertEqual(file_states(self.root), before)

    def test_review_failed_install_repairs_late_mixed_cache_before_cleanup(self) -> None:
        later = None
        local_a, local_b = "http://127.0.0.1:18788", "http://127.0.0.1:18789"

        def failed_start(*args, **kwargs):
            nonlocal later
            later = self.cache("late-main", [entry("a", local_a), entry("b", UP_B)])
            wiring.rewrite_runtime_cache_file(later, {UP_A: local_a, UP_B: local_b}, previous_mapping={local_a: UP_A})
            write_table(later, [entry("a", local_a), entry("b", local_b), entry("later", UP_A)])
            return False

        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
                mock.patch.object(installer, "start_proxy_now", side_effect=failed_start):
            self.assertEqual(self.install("--no-tls"), 1)
        self.assertEqual(urls(later), {"a": UP_A, "b": UP_B, "later": UP_A})
        self.assertFalse((self.config / "autostart.json").exists())

    def test_review_incomplete_late_cache_plan_prevents_environment_registration(self) -> None:
        initial = self.review_initial_environment()

        def started(*args, **kwargs):
            path = self.cache("late-unplannable")
            wiring.rewrite_runtime_cache_file(path, {UP_A: LOCAL_A, UP_B: LOCAL_B})
            path.with_name(path.name + wiring.CACHE_STATE_SUFFIX).write_text("{", encoding="ascii")
            return True

        with mock.patch.object(installer, "stop_configured_proxy", return_value=True), \
                mock.patch.object(installer, "start_proxy_now", side_effect=started), \
                mock.patch.object(installer, "install_autostart_windows", return_value=(self.startup / "fake.lnk", None, True, True)) as register:
            self.assertEqual(self.install("--no-tls"), 1)
        register.assert_not_called()
        self.set_environment.assert_not_called()
        self.assertEqual(self.environment, initial)
        self.assertTrue((self.config / "autostart.json").is_file())

    def test_review_unowned_late_cache_is_not_rewritten(self) -> None:
        snapshot = installer._InstallSnapshot(self.config, self.package)
        path = self.cache("late-other-proxy", [entry("other", "http://localhost:19999")])
        before = file_states(self.root)
        original_write = installer._write_bytes

        def write(candidate, content):
            if candidate == path:
                raise AssertionError("unowned cache must not be rewritten")
            return original_write(candidate, content)

        with mock.patch.object(installer, "_write_bytes", side_effect=write):
            self.assertEqual(snapshot.restore({LOCAL_A: UP_A}), [])
        self.assertEqual(file_states(self.root), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
