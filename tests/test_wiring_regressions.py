from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reqslim.zcode import wiring
from reqslim.zcode.__main__ import CacheWatcher


UP_A = "https://a.example/anthropic"
UP_B = "https://b.example/anthropic"
LOCAL_A = "https://127.0.0.1:18788"
LOCAL_B = "https://127.0.0.1:18789"
OTHER_PROXY = "http://localhost:29999/user-proxy"
TEMP_ROOT = (
    Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "Temp" / "opencode"
    if os.name == "nt" else Path(tempfile.gettempdir()).resolve() / "opencode"
)
TEMP_ROOT = TEMP_ROOT if TEMP_ROOT.is_dir() else None


def entry(ident: str, url: str, template: bool = False, protocol: str = "anthropic-messages") -> dict:
    return {
        "templateId" if template else "providerId": ident,
        "config": {"api": {"type": protocol, "baseUrl": url}},
    }


def write_table(path: Path, providers: list[dict], templates: list[dict] | None = None, **metadata) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    path.write_text(json.dumps({
        **metadata,
        "config": {"providerConfigRules": {
            "providerRules": providers, "templateRules": templates or [],
        }},
    }, indent=2) + "\n", encoding="ascii")


def urls(path: Path) -> dict[str, str]:
    rules = json.loads(path.read_text(encoding="ascii"))["config"]["providerConfigRules"]
    return {
        item.get("providerId", item.get("templateId")): item["config"]["api"]["baseUrl"]
        for section in ("providerRules", "templateRules") for item in rules[section]
    }


def file_states(root: Path) -> dict[Path, tuple[bytes, int, int]]:
    return {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


class WiringRegressions(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="reqslim-wiring-tests-", dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        home = mock.patch.object(Path, "home", side_effect=AssertionError("real home access"))
        home.start()
        self.addCleanup(home.stop)
        self.path = self.root / "windows" / "1" / "endpoint-test" / "zcode-builtin.json"
        self.backup = self.path.with_name(self.path.name + wiring.CACHE_BACKUP_SUFFIX)
        self.state = self.path.with_name(self.path.name + wiring.CACHE_STATE_SUFFIX)
        self.mapping = {UP_A: LOCAL_A, UP_B: LOCAL_B}

    def mixed_backup(self) -> None:
        write_table(self.path, [entry("a", LOCAL_A), entry("b", UP_B)])
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, self.mapping), "rewritten")
        self.assertEqual(urls(self.backup), {"a": LOCAL_A, "b": UP_B})

    def test_mixed_backup_does_not_leave_a_known_local_after_restore(self) -> None:
        self.mixed_backup()
        errors = []
        restored = wiring.restore_runtime_caches(
            root=self.root, reverse_mapping={LOCAL_A: UP_A, LOCAL_B: UP_B}, errors=errors,
        )
        self.assertEqual(errors, [])
        self.assertEqual(restored, [self.path])
        self.assertEqual(urls(self.path), {"a": UP_A, "b": UP_B})
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.state.exists())

    def test_mixed_backup_restore_dry_run_is_read_only(self) -> None:
        self.mixed_backup()
        before = file_states(self.root)
        with mock.patch.object(wiring.os, "chmod", side_effect=AssertionError("dry-run chmod")), \
             mock.patch.object(wiring.os, "replace", side_effect=AssertionError("dry-run replace")), \
             mock.patch.object(Path, "unlink", side_effect=AssertionError("dry-run unlink")):
            self.assertEqual(wiring.restore_runtime_caches(
                root=self.root, reverse_mapping={LOCAL_A: UP_A, LOCAL_B: UP_B},
                dry_run=True, strict=True,
            ), [self.path])
        self.assertEqual(file_states(self.root), before)

    def test_legacy_mixed_backup_without_sidecar_is_repaired(self) -> None:
        self.mixed_backup()
        self.state.unlink()
        self.assertEqual(wiring.restore_runtime_caches(
            root=self.root, reverse_mapping={LOCAL_A: UP_A, LOCAL_B: UP_B}, strict=True,
        ), [self.path])
        self.assertEqual(urls(self.path), {"a": UP_A, "b": UP_B})

    def test_missing_table_mixed_backup_is_repaired_before_replacement(self) -> None:
        write_table(self.backup, [entry("a", LOCAL_A), entry("b", UP_B)])
        before = file_states(self.root)
        self.assertEqual(wiring.restore_runtime_caches(
            root=self.root, reverse_mapping={LOCAL_A: UP_A}, dry_run=True, strict=True,
        ), [self.path])
        self.assertEqual(file_states(self.root), before)
        self.assertEqual(wiring.restore_runtime_caches(
            root=self.root, reverse_mapping={LOCAL_A: UP_A}, strict=True,
        ), [self.path])
        self.assertEqual(urls(self.path), {"a": UP_A, "b": UP_B})
        self.assertFalse(self.backup.exists())

    def test_mixed_backup_repair_failure_is_not_reported_as_success(self) -> None:
        self.mixed_backup()
        before = file_states(self.root)
        errors = []
        with mock.patch.object(wiring, "_atomic_write", side_effect=PermissionError("repair locked")):
            self.assertEqual(wiring.restore_runtime_caches(
                root=self.root, reverse_mapping={LOCAL_A: UP_A, LOCAL_B: UP_B}, errors=errors,
            ), [])
            self.assertIn("repair locked", errors[0])
            with self.assertRaisesRegex(OSError, "restoration failed"):
                wiring.restore_runtime_caches(
                    root=self.root, reverse_mapping={LOCAL_A: UP_A, LOCAL_B: UP_B}, strict=True,
                )
        self.assertEqual(file_states(self.root), before)

    def test_new_provider_and_template_survive_port_swap_and_uninstall(self) -> None:
        write_table(self.path, [entry("a", UP_A)])
        wiring.rewrite_runtime_cache_file(self.path, self.mapping)
        pristine = self.backup.read_bytes()
        write_table(self.path, [entry("b", UP_B), entry("a", LOCAL_A)], [entry("t", UP_B, True)], revision=2)
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, self.mapping), "rewritten")
        swapped = {UP_A: LOCAL_B, UP_B: LOCAL_A}
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, swapped), "rewritten")
        self.assertEqual(urls(self.path), {"a": LOCAL_B, "b": LOCAL_A, "t": LOCAL_A})
        before = file_states(self.root)
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, swapped), "untouched")
        self.assertEqual(file_states(self.root), before)
        self.assertEqual(self.backup.read_bytes(), pristine)
        self.assertEqual(wiring.restore_runtime_caches(root=self.root, strict=True), [self.path])
        self.assertEqual(urls(self.path), {"a": UP_A, "b": UP_B, "t": UP_B})
        self.assertEqual(json.loads(self.path.read_text(encoding="ascii"))["revision"], 2)

    def test_user_changed_proxy_and_added_metadata_are_not_reverted(self) -> None:
        write_table(self.path, [entry("a", UP_A), entry("b", UP_B)])
        wiring.rewrite_runtime_cache_file(self.path, self.mapping)
        write_table(self.path, [entry("a", OTHER_PROXY), entry("b", LOCAL_B), entry("new", UP_A)], revision=3)
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, self.mapping), "rewritten")
        self.assertEqual(urls(self.path), {"a": OTHER_PROXY, "b": LOCAL_B, "new": LOCAL_A})
        wiring.restore_runtime_caches(root=self.root, strict=True)
        self.assertEqual(urls(self.path), {"a": OTHER_PROXY, "b": UP_B, "new": UP_A})
        self.assertEqual(json.loads(self.path.read_text(encoding="ascii"))["revision"], 3)

    def test_cache_watcher_learns_new_entries_and_restarts_with_swapped_ports(self) -> None:
        write_table(self.path, [entry("a", UP_A)])
        watcher = CacheWatcher(self.mapping, root=self.root)
        self.assertEqual(watcher.tick(), [self.path])
        write_table(self.path, [entry("b", UP_B), entry("a", LOCAL_A)], [entry("t", UP_B, True)])
        self.assertEqual(watcher.tick(), [self.path])
        self.assertEqual(watcher.tick(), [])
        replacement = CacheWatcher({UP_A: LOCAL_B, UP_B: LOCAL_A}, root=self.root)
        self.assertEqual(replacement.tick(), [self.path])
        self.assertEqual(urls(self.path), {"a": LOCAL_B, "b": LOCAL_A, "t": LOCAL_A})
        before = file_states(self.root)
        self.assertEqual(replacement.tick(), [])
        self.assertEqual(file_states(self.root), before)

    def test_loopback_with_path_is_a_valid_original_upstream(self) -> None:
        upstream = "http://localhost:24444/gateway/anthropic/"
        write_table(self.path, [entry("local-upstream", upstream)])
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, {upstream.rstrip("/"): LOCAL_A}), "rewritten")
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, {upstream.rstrip("/"): LOCAL_B}), "rewritten")
        self.assertEqual(urls(self.path), {"local-upstream": LOCAL_B})
        self.backup.unlink()
        wiring.restore_runtime_caches(root=self.root, strict=True)
        self.assertEqual(urls(self.path), {"local-upstream": upstream})

    def test_unknown_loopback_is_not_claimed_from_backup_identity_alone(self) -> None:
        write_table(self.backup, [entry("a", UP_A)])
        write_table(self.path, [entry("a", OTHER_PROXY)])
        before = self.path.read_bytes()
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, self.mapping), "unrelated")
        self.assertEqual(self.path.read_bytes(), before)
        wiring.restore_runtime_caches(root=self.root, strict=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_previous_mapping_migrates_a_provider_missing_from_legacy_backup(self) -> None:
        write_table(self.backup, [entry("a", UP_A)])
        write_table(self.path, [entry("b", LOCAL_B), entry("a", LOCAL_A), entry("other", OTHER_PROXY)])
        swapped = {UP_A: LOCAL_B, UP_B: LOCAL_A}
        before = file_states(self.root)
        self.assertEqual(wiring.rewrite_runtime_caches(
            swapped, root=self.root, dry_run=True,
            previous_mapping={LOCAL_A: UP_A, LOCAL_B: UP_B},
        ), [(self.path, "rewritten")])
        self.assertEqual(file_states(self.root), before)
        self.assertEqual(wiring.rewrite_runtime_caches(
            swapped, root=self.root, previous_mapping={LOCAL_A: UP_A, LOCAL_B: UP_B},
        ), [(self.path, "rewritten")])
        self.assertEqual(urls(self.path), {"a": LOCAL_B, "b": LOCAL_A, "other": OTHER_PROXY})
        self.assertTrue(self.state.is_file())
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, swapped), "untouched")
        wiring.restore_runtime_caches(root=self.root, strict=True)
        self.assertEqual(urls(self.path), {"a": UP_A, "b": UP_B, "other": OTHER_PROXY})

    def test_legacy_port_swap_without_ownership_evidence_is_not_guessed(self) -> None:
        write_table(self.backup, [entry("a", UP_A), entry("b", UP_B)])
        write_table(self.path, [entry("a", LOCAL_A), entry("b", LOCAL_B)])
        before = file_states(self.root)
        with self.assertRaisesRegex((OSError, ValueError), "ownership|previous_mapping"):
            wiring.rewrite_runtime_cache_file(self.path, {UP_A: LOCAL_B, UP_B: LOCAL_A})
        self.assertEqual(file_states(self.root), before)

    def test_sidecar_is_written_before_table_and_retains_old_port_on_failure(self) -> None:
        write_table(self.path, [entry("a", UP_A)])
        wiring.rewrite_runtime_cache_file(self.path, {UP_A: LOCAL_A})
        before = self.path.read_bytes()
        real_write = wiring._atomic_write
        writes = []

        def fail_table(path, text, **options):
            writes.append(path)
            if path == self.path:
                raise PermissionError("table locked")
            return real_write(path, text, **options)

        with mock.patch.object(wiring, "_atomic_write", side_effect=fail_table):
            with self.assertRaisesRegex(OSError, "table locked"):
                wiring.rewrite_runtime_cache_file(self.path, {UP_A: LOCAL_B})
        self.assertEqual(writes, [self.state, self.path])
        self.assertEqual(self.path.read_bytes(), before)
        state_text = self.state.read_text(encoding="ascii")
        self.assertIn(LOCAL_A, state_text)
        self.assertIn(LOCAL_B, state_text)
        self.backup.unlink()
        self.assertEqual(wiring.restore_runtime_caches(root=self.root, strict=True), [self.path])
        self.assertEqual(urls(self.path), {"a": UP_A})

    def test_sidecar_write_failure_never_rewrites_table(self) -> None:
        write_table(self.path, [entry("a", UP_A)])
        before = self.path.read_bytes()
        real_write = wiring._atomic_write

        def fail_state(path, text, **options):
            if path == self.state:
                raise PermissionError("state locked")
            return real_write(path, text, **options)

        with mock.patch.object(wiring, "_atomic_write", side_effect=fail_state):
            with self.assertRaisesRegex(OSError, "state locked"):
                wiring.rewrite_runtime_cache_file(self.path, {UP_A: LOCAL_A})
        self.assertEqual(self.path.read_bytes(), before)

    def test_sidecar_corruption_blocks_remap_and_strict_restore(self) -> None:
        write_table(self.path, [entry("a", UP_A)])
        wiring.rewrite_runtime_cache_file(self.path, {UP_A: LOCAL_A})
        self.state.write_text('{"version": 999, "entries": []}', encoding="ascii")
        before = file_states(self.root)
        with self.assertRaises((OSError, ValueError)):
            wiring.rewrite_runtime_cache_file(self.path, {UP_A: LOCAL_B})
        errors = []
        self.assertEqual(wiring.restore_runtime_caches(root=self.root, errors=errors), [])
        self.assertTrue(errors)
        with self.assertRaisesRegex(OSError, "restoration failed"):
            wiring.restore_runtime_caches(root=self.root, strict=True)
        self.assertEqual(file_states(self.root), before)

    def test_state_history_overrides_global_reverse_mapping_after_port_swaps(self) -> None:
        write_table(self.path, [entry("a", UP_A), entry("b", UP_B)])
        wiring.rewrite_runtime_cache_file(self.path, self.mapping)
        swapped = {UP_A: LOCAL_B, UP_B: LOCAL_A}
        wiring.rewrite_runtime_cache_file(self.path, swapped)
        write_table(self.path, [entry("b", LOCAL_B), entry("a", LOCAL_A)])
        self.backup.unlink()
        self.assertEqual(wiring.restore_runtime_caches(
            root=self.root, reverse_mapping={LOCAL_B: UP_A, LOCAL_A: UP_B}, strict=True,
        ), [self.path])
        self.assertEqual(urls(self.path), {"a": UP_A, "b": UP_B})

    def test_changed_cloud_upstream_is_latest_even_when_its_local_port_is_reused(self) -> None:
        write_table(self.path, [entry("a", UP_A)])
        wiring.rewrite_runtime_cache_file(self.path, {UP_A: LOCAL_A})
        write_table(self.path, [entry("a", UP_B)])
        self.assertEqual(wiring.rewrite_runtime_cache_file(self.path, {UP_B: LOCAL_A}), "rewritten")
        self.backup.unlink()
        self.assertEqual(wiring.restore_runtime_caches(root=self.root, strict=True), [self.path])
        self.assertEqual(urls(self.path), {"a": UP_B})

    def test_ambiguous_historical_backup_blocks_cleanup(self) -> None:
        write_table(self.path, [entry("a", UP_A)])
        wiring.rewrite_runtime_cache_file(self.path, {UP_A: LOCAL_A})
        write_table(self.path, [entry("a", UP_B)])
        wiring.rewrite_runtime_cache_file(self.path, {UP_B: LOCAL_A})
        write_table(self.backup, [entry("a", LOCAL_A)])
        self.path.chmod(self.path.stat().st_mode | stat.S_IWUSR)
        self.path.unlink()
        before = file_states(self.root)
        errors = []
        self.assertEqual(wiring.restore_runtime_caches(root=self.root, errors=errors), [])
        self.assertIn("ambiguous cache ownership", errors[0])
        with self.assertRaisesRegex(OSError, "restoration failed"):
            wiring.restore_runtime_caches(root=self.root, strict=True)
        self.assertEqual(file_states(self.root), before)

    def test_removed_provider_and_manual_remote_changes_survive_uninstall(self) -> None:
        write_table(self.path, [entry("a", UP_A), entry("b", UP_B)])
        wiring.rewrite_runtime_cache_file(self.path, self.mapping)
        changed = "https://user.example/custom-gateway"
        write_table(self.path, [entry("a", changed), entry("new", UP_B)], revision=4)
        self.assertEqual(wiring.restore_runtime_caches(root=self.root, strict=True), [self.path])
        self.assertEqual(urls(self.path), {"a": changed, "new": UP_B})
        self.assertEqual(json.loads(self.path.read_text(encoding="ascii"))["revision"], 4)

    def test_unmodified_legacy_backup_keeps_exact_bytes_and_writable_mode(self) -> None:
        write_table(self.backup, [entry("a", UP_A)])
        original = self.backup.read_bytes()
        write_table(self.path, [entry("a", LOCAL_A)])
        self.path.chmod(self.path.stat().st_mode & ~stat.S_IWUSR)
        self.assertEqual(wiring.restore_runtime_caches(
            root=self.root, reverse_mapping={LOCAL_A: UP_A}, strict=True,
        ), [self.path])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.path.stat().st_mode & stat.S_IWUSR)
        self.assertFalse(self.backup.exists())

    def test_rewrite_dry_run_never_creates_sidecar_or_backup(self) -> None:
        write_table(self.path, [entry("a", UP_A)])
        before = file_states(self.root)
        self.assertEqual(wiring.rewrite_runtime_caches(self.mapping, root=self.root, dry_run=True), [
            (self.path, "rewritten"),
        ])
        self.assertEqual(file_states(self.root), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
