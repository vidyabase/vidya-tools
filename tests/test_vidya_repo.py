"""Tests for vidya_repo.py / vidya_lib.repo.

    python3 -m unittest discover tests -v

Each test builds a small sample repo in a temporary folder.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vidya_lib import repo as vr  # noqa: E402


def make_vidya(path: Path, records: dict[int, str], *, extra_meta=None, extra_sql: str = "") -> Path:
    """A minimal .vidya-like file: db_meta + records."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE db_meta (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, value TEXT);"
        "CREATE TABLE records (id INTEGER PRIMARY KEY, content TEXT);"
    )
    meta = {"library_title": "Sample Library", "vidya_format_version": "2", "update-url": '"@vidyabase:sample:en"'}
    meta.update(extra_meta or {})
    conn.executemany("INSERT INTO db_meta (key, value) VALUES (?, ?)", meta.items())
    conn.executemany("INSERT INTO records (id, content) VALUES (?, ?)", records.items())
    if extra_sql:
        conn.executescript(extra_sql)
    conn.commit()
    conn.close()
    return path


def meta_of(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT key, value FROM db_meta"))
    finally:
        conn.close()


def unpack(zp: Path, dst: Path) -> Path:
    with zipfile.ZipFile(zp) as zf:
        (entry,) = zf.infolist()
        with zf.open(entry) as fi, open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo)
    return dst


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.public = self.repo / "public"
        self.v1 = make_vidya(self.tmp / "in1.vidya", {1: "one", 2: "two", 3: "three"})
        self.v2 = make_vidya(self.tmp / "in2.vidya", {1: "one (fixed)", 3: "three", 4: "four"})
        self.v3 = make_vidya(
            self.tmp / "in3.vidya", {1: "one (fixed)", 4: "four", 5: "five"},
            extra_sql="CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO authors VALUES (1, 'A');",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def init(self, **kw):
        return vr.init_repo(self.repo, self.v1, slug="sample-en", base_url="https://example.org/vidya/sample-en/", **kw)

    def manifest(self) -> dict:
        return vr.load_manifest(self.repo)

    def full_copy(self, version: int, name: str = "installed.vidya") -> Path:
        e = vr.version_entry(self.manifest(), version)
        return unpack(self.public / e["full"]["file"], self.tmp / name)


class TestInitAndAdd(RepoTestCase):
    def test_init_creates_repo_and_stamps_copy(self):
        before = self.v1.read_bytes()
        m = self.init()
        self.assertEqual(self.v1.read_bytes(), before, "input file must not change")
        self.assertEqual(m["latest"], 1)
        self.assertEqual(m["update_url"], "https://example.org/vidya/sample-en/repo.json")
        self.assertEqual(m["install_file_name"], "sample-en.vidya")
        self.assertEqual(m["title"], "Sample Library")
        self.assertTrue((self.public / "sample-en-000001.vidya.zip").is_file())
        self.assertIsNone(m["versions"][0]["diff"])
        meta = meta_of(self.full_copy(1))
        self.assertEqual(meta["repo_version"], "1")
        self.assertEqual(meta["update_url"], m["update_url"])
        self.assertNotIn("update-url", meta)
        self.assertFalse((self.repo / vr.WORK_DIR).exists())
        self.assertFalse((self.repo / vr.LOCK_NAME).exists())

    def test_init_twice_fails(self):
        self.init()
        with self.assertRaises(vr.RepoError):
            self.init()

    def test_bad_slug_and_url(self):
        with self.assertRaises(vr.RepoError):
            vr.init_repo(self.repo, self.v1, slug="Bad Slug", base_url="https://example.org/")
        with self.assertRaises(vr.RepoError):
            vr.init_repo(self.repo, self.v1, slug="ok", base_url="example.org/x")

    def test_add_versions_and_diff_names(self):
        self.init()
        vr.add_version(self.repo, self.v2, vr.VersionOptions(label="fixes"))
        vr.add_version(self.repo, self.v3)
        m = self.manifest()
        self.assertEqual(m["latest"], 3)
        e3 = vr.version_entry(m, 3)
        self.assertEqual(e3["diff"]["from"], 2)
        self.assertEqual(e3["diff"]["file"], "sample-en-000002-000003.sql.zip")
        self.assertEqual(e3["full"]["file"], "sample-en-000003.vidya.zip")
        self.assertIn("authors", e3["diff"]["stats"]["tables"])
        self.assertEqual(vr.version_entry(m, 2)["label"], "fixes")
        self.assertEqual(meta_of(self.full_copy(3))["repo_version"], "3")

    def test_zip_layout_and_reproducible(self):
        self.init()
        vr.add_version(self.repo, self.v2)
        e = vr.version_entry(self.manifest(), 2)
        with zipfile.ZipFile(self.public / e["full"]["file"]) as zf:
            self.assertEqual(zf.namelist(), ["sample-en.vidya"])
            self.assertEqual(zf.infolist()[0].compress_type, zipfile.ZIP_DEFLATED)
        self.assertEqual(e["full"]["entry"], "sample-en.vidya")
        with zipfile.ZipFile(self.public / e["diff"]["file"]) as zf:
            self.assertEqual(zf.namelist(), ["sample-en-000001-000002.sql"])
        src = self.tmp / "x.bin"
        src.write_bytes(b"abc" * 1000)
        a = vr.zip_file(src, self.tmp / "a.zip", "x.bin", 6)
        b = vr.zip_file(src, self.tmp / "b.zip", "x.bin", 6)
        self.assertEqual(a["sha256"], b["sha256"])

    def test_unchanged_content_is_refused_unless_forced(self):
        self.init()
        same = make_vidya(self.tmp / "same.vidya", {1: "one", 2: "two", 3: "three"},
                          extra_meta={"update-url": '"something else"'})
        with self.assertRaises(vr.RepoError):
            vr.add_version(self.repo, same)
        self.assertEqual(self.manifest()["latest"], 1)
        vr.add_version(self.repo, same, vr.VersionOptions(force=True))
        self.assertEqual(self.manifest()["latest"], 2)

    def test_dry_run_writes_nothing(self):
        self.init()
        before = (self.public / "repo.json").read_text()
        vr.add_version(self.repo, self.v2, vr.VersionOptions(dry_run=True))
        self.assertEqual((self.public / "repo.json").read_text(), before)
        self.assertEqual(sorted(p.name for p in self.public.iterdir()), ["repo.json", "sample-en-000001.vidya.zip"])

    def test_lock_blocks_second_run(self):
        self.init()
        (self.repo / vr.LOCK_NAME).write_text("123\n")
        with self.assertRaises(vr.RepoError):
            vr.add_version(self.repo, self.v2)


class TestReaderVersions(RepoTestCase):
    def test_set_inherit_clear(self):
        self.init(opts=vr.VersionOptions(recommended="2.0.14"))
        vr.add_version(self.repo, self.v2, vr.VersionOptions(required="2.1.0"))
        vr.add_version(self.repo, self.v3, vr.VersionOptions(clear_recommended=True))
        m = self.manifest()
        e1, e2, e3 = (vr.version_entry(m, v) for v in (1, 2, 3))
        self.assertEqual((e1[vr.KEY_RECOMMENDED], e1[vr.KEY_REQUIRED]), ("2.0.14", None))
        self.assertEqual((e2[vr.KEY_RECOMMENDED], e2[vr.KEY_REQUIRED]), ("2.0.14", "2.1.0"))
        self.assertEqual((e3[vr.KEY_RECOMMENDED], e3[vr.KEY_REQUIRED]), (None, "2.1.0"))
        meta = meta_of(self.full_copy(3))
        self.assertNotIn(vr.KEY_RECOMMENDED, meta)
        self.assertEqual(meta[vr.KEY_REQUIRED], "2.1.0")

    def test_value_from_input_file(self):
        self.init()
        v2 = make_vidya(self.tmp / "in2b.vidya", {1: "x"}, extra_meta={vr.KEY_RECOMMENDED: '"3.0"'})
        vr.add_version(self.repo, v2)
        self.assertEqual(vr.version_entry(self.manifest(), 2)[vr.KEY_RECOMMENDED], "3.0")

    def test_invalid_version_rejected(self):
        with self.assertRaises(vr.RepoError):
            self.init(opts=vr.VersionOptions(required="two"))

    def test_compare(self):
        self.assertTrue(vr.reader_older("2.0.9", "2.0.14"))
        self.assertFalse(vr.reader_older("2.1", "2.0.14"))
        self.assertFalse(vr.reader_older("2.0", "2.0.0"))
        self.assertTrue(vr.reader_older("v1.9.9", "2"))


class TestPlanUpdate(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.init()
        vr.add_version(self.repo, self.v2, vr.VersionOptions(recommended="2.0.14"))
        vr.add_version(self.repo, self.v3, vr.VersionOptions(required="2.1.0"))
        self.m = self.manifest()

    def test_up_to_date(self):
        self.assertEqual(vr.plan_update(self.m, 3).status, "up-to-date")

    def test_full_chain_without_reader_version(self):
        plan = vr.plan_update(self.m, 1)
        self.assertEqual(plan.status, "update")
        self.assertEqual([e["version"] for e in plan.steps], [2, 3])

    def test_recommended_is_reported_before_update(self):
        plan = vr.plan_update(self.m, 1, "2.1.0")
        self.assertEqual(plan.status, "update")
        self.assertIsNone(plan.recommended_reader)
        plan = vr.plan_update(self.m, 1, "2.0.0")
        self.assertEqual(plan.recommended_reader, "2.0.14")

    def test_required_stops_chain(self):
        plan = vr.plan_update(self.m, 1, "2.0.14")
        self.assertEqual(plan.status, "partial")
        self.assertEqual([e["version"] for e in plan.steps], [2])
        self.assertEqual((plan.stop_reason, plan.stop_version, plan.required_reader), ("required-reader", 3, "2.1.0"))
        plan = vr.plan_update(self.m, 2, "2.0.14")
        self.assertEqual(plan.status, "blocked")

    def test_no_repo_version(self):
        self.assertEqual(vr.plan_update(self.m, None).status, "manual")

    def test_missing_diff(self):
        vr.prune(self.repo, diffs={2})
        plan = vr.plan_update(self.manifest(), 1)
        self.assertEqual((plan.status, plan.stop_reason, plan.stop_version), ("manual", "missing-diff", 2))
        self.assertEqual(vr.plan_update(self.manifest(), 2).status, "update")


class TestApplyAndGuards(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.init()
        vr.add_version(self.repo, self.v2)
        vr.add_version(self.repo, self.v3)

    def diff_sql(self, version: int) -> str:
        e = vr.version_entry(self.manifest(), version)
        with zipfile.ZipFile(self.public / e["diff"]["file"]) as zf:
            return zf.read(e["diff"]["entry"]).decode("utf-8")

    def test_client_updates_from_v1_to_latest(self):
        installed = self.full_copy(1)
        plan = vr.client_update(installed, repo_url=str(self.public / "repo.json"))
        self.assertEqual(plan.target, 3)
        ok, rest = vr.same_content(installed, self.full_copy(3, "latest.vidya"))
        self.assertTrue(ok, rest)

    def test_wrong_start_version_is_rejected_and_rolled_back(self):
        installed = self.full_copy(1)
        before = meta_of(installed), sqlite3.connect(installed).execute("SELECT * FROM records").fetchall()
        with self.assertRaises(vr.RepoError) as cm:
            vr.apply_patch(installed, self.diff_sql(3), 3)  # diff 2->3 on a v1 file
        self.assertIn("not at the version", str(cm.exception))
        after = meta_of(installed), sqlite3.connect(installed).execute("SELECT * FROM records").fetchall()
        self.assertEqual(before, after)

    def test_applying_twice_is_rejected(self):
        installed = self.full_copy(1)
        vr.apply_patch(installed, self.diff_sql(2), 2)
        with self.assertRaises(vr.RepoError):
            vr.apply_patch(installed, self.diff_sql(2), 2)
        self.assertEqual(meta_of(installed)["repo_version"], "2")

    def test_decode_download_checks_zip(self):
        e = vr.version_entry(self.manifest(), 2)["diff"]
        data = (self.public / e["file"]).read_bytes()
        raw = vr.decode_download(data, e)
        self.assertTrue(raw.startswith(b"-- SQL patch"))
        with self.assertRaises(vr.RepoError):
            vr.decode_download(data + b"x", e)
        with self.assertRaises(vr.RepoError):
            vr.decode_download(raw, e)  # unpacked bytes are not accepted

    def test_client_respects_recommended_and_required(self):
        repo2 = self.tmp / "repo2"
        vr.init_repo(repo2, self.v1, slug="s", base_url="https://example.org/s/")
        vr.add_version(repo2, self.v2, vr.VersionOptions(recommended="2.0.14"))
        vr.add_version(repo2, self.v3, vr.VersionOptions(required="2.1"))
        url = str(repo2 / "public" / "repo.json")
        installed = unpack(repo2 / "public" / "s-000001.vidya.zip", self.tmp / "s.vidya")
        vr.client_update(installed, repo_url=url, reader_version="2.0.0")  # recommended: asks first
        self.assertEqual(meta_of(installed)["repo_version"], "1")
        vr.client_update(installed, repo_url=url, reader_version="2.0.0", yes=True)  # stops before 3
        self.assertEqual(meta_of(installed)["repo_version"], "2")
        vr.client_update(installed, repo_url=url, reader_version="2.1")
        self.assertEqual(meta_of(installed)["repo_version"], "3")


class TestPruneAndVerify(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.init()
        vr.add_version(self.repo, self.v2)
        vr.add_version(self.repo, self.v3)

    def test_verify_deep_ok(self):
        res = vr.verify_repo(self.repo, deep=True)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(res.warnings, [])

    def test_keep_first_and_last_full(self):
        lost = vr.prune(self.repo, full={2})
        self.assertEqual(lost, [])
        self.assertFalse((self.public / "sample-en-000002.vidya.zip").exists())
        self.assertIsNone(vr.version_entry(self.manifest(), 2)["full"])
        res = vr.verify_repo(self.repo, deep=True)
        self.assertTrue(res.ok, res.errors)
        vr.add_version(self.repo, make_vidya(self.tmp / "in4.vidya", {9: "nine"}))
        self.assertTrue(vr.verify_repo(self.repo, deep=True).ok)

    def test_prune_diff_reports_lost_versions(self):
        lost = vr.prune(self.repo, diffs={2})
        self.assertEqual(lost, [1])
        res = vr.verify_repo(self.repo, deep=True)
        self.assertTrue(res.ok, res.errors)
        self.assertTrue(any("cannot update automatically" in w for w in res.warnings))

    def test_latest_full_cannot_be_pruned(self):
        with self.assertRaises(vr.RepoError):
            vr.prune(self.repo, full={3})

    def test_prune_dry_run(self):
        vr.prune(self.repo, full={1, 2}, dry_run=True)
        self.assertTrue((self.public / "sample-en-000001.vidya.zip").exists())

    def test_verify_detects_tampering(self):
        p = self.public / "sample-en-000002-000003.sql.zip"
        data = bytearray(p.read_bytes())
        data[len(data) // 2] ^= 0xFF
        p.write_bytes(bytes(data))
        res = vr.verify_repo(self.repo)
        self.assertFalse(res.ok)

    def test_verify_warns_about_unlisted_files(self):
        (self.public / "stray.txt").write_text("x")
        res = vr.verify_repo(self.repo)
        self.assertTrue(res.ok)
        self.assertTrue(any("stray.txt" in w for w in res.warnings))

    def test_manifest_is_valid_json_with_expected_keys(self):
        m = json.loads((self.public / "repo.json").read_text())
        self.assertEqual(
            set(m), {"format", "slug", "title", "update_url", "install_file_name", "latest", "updated_at", "versions"}
        )
        self.assertEqual(
            set(m["versions"][1]),
            {"version", "created_at", "label", "notes", "format_version", vr.KEY_RECOMMENDED, vr.KEY_REQUIRED,
             "full", "diff"},
        )
        self.assertEqual(m["versions"][1]["format_version"], "2")


class TestCli(RepoTestCase):
    def run_cli(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "vidya_repo.py"), *map(str, args)],
                              capture_output=True, text=True)

    def test_workflow(self):
        r = self.run_cli("init", self.repo, self.v1, "--slug", "sample-en", "--base-url", "https://example.org/x/")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("add", self.repo, self.v2, "--recommended-reader", "2.0.14", "--label", "fixes")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("add", self.repo, self.v2)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no content changes", r.stderr)
        r = self.run_cli("log", self.repo)
        self.assertIn("fixes", r.stdout)
        r = self.run_cli("verify", self.repo, "--deep")
        self.assertEqual(r.returncode, 0, r.stdout)
        installed = self.full_copy(1)
        r = self.run_cli("update", installed, "--repo-url", self.public / "repo.json", "--reader-version", "2.0.0")
        self.assertEqual(r.returncode, 3, r.stdout)
        r = self.run_cli("update", installed, "--repo-url", self.public / "repo.json", "--reader-version", "2.0.14")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(meta_of(installed)["repo_version"], "2")
        r = self.run_cli("prune", self.repo, "--full", "1", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
