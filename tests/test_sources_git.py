import os
import shutil
import subprocess
import tempfile
import unittest

from agentree.sources import git as g

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live")


def fx(name: str, binary: bool = False):
    with open(os.path.join(FIX, name), "rb" if binary else "r", **({} if binary else {"encoding": "utf-8"})) as fh:
        return fh.read()


class WorktreePorcelainTest(unittest.TestCase):
    def test_live(self):
        recs = g.parse_worktree_porcelain_z(fx("git_worktree_list_porcelain_z.bin", True))
        self.assertGreaterEqual(len(recs), 2)
        self.assertTrue(recs[0]["worktree"].endswith("web-app"))
        self.assertTrue(recs[0]["branch"].startswith("refs/heads/"))
        self.assertEqual(len(recs[0]["HEAD"]), 40)
        det = [r for r in recs if r.get("detached")]
        self.assertTrue(det)
        for r in det:
            self.assertNotIn("branch", r)
        for r in recs:
            self.assertIn("worktree", r)

    def test_synthetic(self):
        data = (b"worktree /r\0HEAD " + b"a" * 40 + b"\0branch refs/heads/main\0\0"
                b"worktree /r/.worktrees/x\0HEAD " + b"b" * 40 + b"\0detached\0locked\0\0"
                b"worktree /gone\0HEAD " + b"c" * 40 + b"\0branch refs/heads/feat/x y\0locked being moved\0"
                b"prunable gitdir file points to non-existent location\0\0"
                b"worktree /bare\0bare\0\0")
        recs = g.parse_worktree_porcelain_z(data)
        self.assertEqual(len(recs), 4)
        self.assertEqual(recs[0], {"worktree": "/r", "HEAD": "a" * 40, "branch": "refs/heads/main"})
        self.assertTrue(recs[1]["detached"])
        self.assertEqual(recs[1]["locked"], "")
        self.assertEqual(recs[2]["branch"], "refs/heads/feat/x y")
        self.assertEqual(recs[2]["locked"], "being moved")
        self.assertTrue(recs[2]["prunable"].startswith("gitdir file"))
        self.assertTrue(recs[3]["bare"])

    def test_empty(self):
        self.assertEqual(g.parse_worktree_porcelain_z(b""), [])


class StatusV2Test(unittest.TestCase):
    def test_live_clean_detached(self):
        s = g.parse_status_v2(fx("git_status_v2_staging.txt"))
        self.assertEqual(s.dirty, 0)
        self.assertIsNone(s.upstream)
        self.assertIsNone(s.ahead)

    def test_live_with_upstream(self):
        s = g.parse_status_v2(fx("git_status_v2_sso-login.txt"))
        self.assertIsNotNone(s.upstream)
        self.assertIsNotNone(s.ahead)
        self.assertIsNotNone(s.behind)
        lines = fx("git_status_v2_sso-login.txt").splitlines()
        self.assertEqual(s.dirty, sum(1 for ln in lines if ln[:2] in ("1 ", "2 ", "u ", "? ")))

    def test_synthetic(self):
        text = (
            "# branch.oid abc\n# branch.head feat\n# branch.upstream origin/feat\n# branch.ab +3 -7\n"
            "1 .M N... 100644 100644 100644 a b src/a.py\n"
            "2 R. N... 100644 100644 100644 a b R100 new name.py\told.py\n"
            "u UU N... 100644 100644 100644 100644 a b c conflict.txt\n"
            "? untracked dir/\n"
            "! ignored.txt\n"
        )
        s = g.parse_status_v2(text)
        self.assertEqual(s, g.StatusV2(4, 3, 7, "origin/feat"))

    def test_no_upstream(self):
        s = g.parse_status_v2("# branch.oid abc\n# branch.head main\n")
        self.assertEqual(s, g.StatusV2(0, None, None, None))

    def test_nul_separated_rename(self):
        text = ("# branch.oid abc\0# branch.head (detached)\0"
                "2 R. N... 100644 100644 100644 a b R100 new\0old\0? x\0")
        self.assertEqual(g.parse_status_v2(text).dirty, 2)


class RefParsersTest(unittest.TestCase):
    def test_live_for_each_ref(self):
        refs = g.parse_for_each_ref(fx("git_for_each_ref.bin", True).decode())
        self.assertTrue(refs)
        for name, r in refs.items():
            self.assertTrue(name.startswith("refs/heads/"))
            self.assertEqual(len(r["objectname"]), 40)
            self.assertTrue(r["ct"].isdigit())
            self.assertTrue(r["subject"])
            self.assertIsNotNone(g.parse_ahead_behind_atom(r["ab_default"]))

    def test_track(self):
        self.assertEqual(g.parse_track(""), (0, 0))
        self.assertEqual(g.parse_track("ahead 2"), (2, 0))
        self.assertEqual(g.parse_track("behind 5"), (0, 5))
        self.assertEqual(g.parse_track("ahead 22, behind 95"), (22, 95))
        self.assertIsNone(g.parse_track("gone"))

    def test_ahead_behind_and_rev_list(self):
        self.assertEqual(g.parse_ahead_behind_atom("3 227"), (3, 227))
        self.assertIsNone(g.parse_ahead_behind_atom(""))
        # rev-list --left-right --count base...HEAD prints "behind ahead"
        self.assertEqual(g.parse_rev_list_count("66\t12\n"), (12, 66))
        self.assertIsNone(g.parse_rev_list_count("fatal"))

    def test_log_no_walk(self):
        logs = g.parse_log_no_walk(fx("git_log_no_walk.bin", True).decode())
        self.assertTrue(logs)
        for sha, (ct, subj) in logs.items():
            self.assertEqual(len(sha), 40)
            self.assertIsInstance(ct, int)
            self.assertTrue(subj)

    def test_subject_with_nul_safe(self):
        refs = g.parse_for_each_ref("refs/heads/a\0" + "f" * 40 + "\0\0\0\0" + "123\0subj\n")
        self.assertEqual(refs["refs/heads/a"]["subject"], "subj")
        self.assertEqual(refs["refs/heads/a"]["upstream"], "")


@unittest.skipUnless(shutil.which("git"), "git missing")
class ScratchRepoFetchTest(unittest.TestCase):
    """End-to-end against a throwaway repo in $TMPDIR (never touches real repos)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentree-git-")
        self.base = os.path.join(self.tmp, "projects")
        os.makedirs(self.base)
        self.repo = os.path.join(self.base, "demo")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")

        def sh(*args, cwd=None):
            subprocess.run(args, cwd=cwd or self.repo, env=env, check=True, capture_output=True)

        os.makedirs(self.repo)
        sh("git", "init", "-q", "-b", "main")
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:
            fh.write("a\n")
        sh("git", "add", "a.txt")
        sh("git", "commit", "-q", "-m", "first commit")
        # fake origin/main + origin/HEAD without a remote
        sh("git", "update-ref", "refs/remotes/origin/main", "HEAD")
        sh("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        sh("git", "worktree", "add", "-q", "-b", "agent/x", ".worktrees/x")
        wt = os.path.join(self.repo, ".worktrees", "x")
        with open(os.path.join(wt, "b.txt"), "w") as fh:
            fh.write("b\n")
        sh("git", "add", "b.txt", cwd=wt)
        sh("git", "commit", "-q", "-m", "second", cwd=wt)
        with open(os.path.join(wt, "untracked.txt"), "w") as fh:
            fh.write("u\n")
        sh("git", "worktree", "add", "-q", "--detach", ".worktrees/det")
        sh("git", "worktree", "add", "-q", "-b", "gone", os.path.join(self.tmp, "gone"))
        shutil.rmtree(os.path.join(self.tmp, "gone"))
        os.makedirs(os.path.join(self.base, "notarepo"))
        self.wt = wt

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fetch(self):
        self.assertEqual(g.discover_repos([os.path.join(self.base, "*")]), [os.path.realpath(self.repo)])
        repos, warnings = g.fetch([os.path.join(self.base, "*")], timeout=5, budget_s=10)
        self.assertEqual(warnings, [])
        self.assertEqual(len(repos), 1)
        r = repos[0]
        root = os.path.realpath(self.repo)
        self.assertEqual(r.root, root)  # /var -> /private/var
        self.assertEqual(r.name, "demo")
        self.assertEqual(r.default_ref, "origin/main")
        by = {w.path: w for w in r.worktrees}
        self.assertEqual(r.worktrees[0].path, root)
        main = by[root]
        self.assertTrue(main.is_root)
        self.assertEqual(main.branch, "main")
        self.assertEqual(main.dirty, 1)  # untracked .worktrees/ (not excluded in this scratch repo)
        self.assertEqual((main.ahead, main.behind, main.base_ref), (0, 0, "origin/main"))
        self.assertEqual(main.last_commit, "first commit")
        self.assertIsNotNone(main.last_commit_age_s)
        x = by[os.path.realpath(self.wt)]
        self.assertFalse(x.is_root)
        self.assertEqual(x.branch, "agent/x")
        self.assertEqual(x.repo_root, root)
        self.assertEqual(x.dirty, 1)
        self.assertEqual((x.ahead, x.behind, x.base_ref), (1, 0, "origin/main"))
        self.assertEqual(x.last_commit, "second")
        det = by[os.path.realpath(os.path.join(self.repo, ".worktrees", "det"))]
        self.assertTrue(det.detached)
        self.assertIsNone(det.branch)
        self.assertEqual(det.last_commit, "first commit")
        self.assertEqual((det.ahead, det.behind), (0, 0))
        self.assertTrue(det.label.startswith("("))
        gone = [w for w in r.worktrees if w.prunable]
        self.assertEqual(len(gone), 1)
        self.assertIsNone(gone[0].dirty)

    def test_budget_exhausted_keeps_partial(self):
        repos, warnings = g.fetch([os.path.join(self.base, "*")], timeout=5, budget_s=0.0)
        self.assertEqual(len(repos), 1)
        self.assertTrue(warnings)
        for w in warnings:
            self.assertEqual(w.source, "git")


if __name__ == "__main__":
    unittest.main()
