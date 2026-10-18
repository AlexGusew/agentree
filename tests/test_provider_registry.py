import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from agentree import config
from agentree.config import Config
from agentree.providers import command, registry
from agentree.providers.base import ProviderSpec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONCRETE = ("agentree.providers.opencode", "agentree.providers.claude")

FAKE = ProviderSpec(id="codex", label="Codex", module="tests._fake_codex_provider_mod",
                    command_key="codex_cmd", default_command="codex --full-auto",
                    extra_command_dirs=("~/.codex/bin",), internal_commands=("codex-hook",))


class FakeProvider:
    def __init__(self, spec):
        self.spec = spec
        self.id = spec.id
        self.quick_failure = "keep_provider"


def three_specs():
    return (*registry.SPECS, FAKE)


class RegistryTest(unittest.TestCase):
    def test_descriptors(self):
        self.assertEqual(registry.ids(), ("opencode", "claude"))           # order = PID precedence
        oc, cc = registry.specs()
        self.assertEqual((oc.command_key, oc.default_command, oc.module),
                         ("opencode_cmd", "opencode", "agentree.providers.opencode"))
        self.assertEqual((cc.command_key, cc.default_command, cc.label, cc.module),
                         ("claude_cmd", "claude", "Claude Code", "agentree.providers.claude"))
        self.assertEqual(cc.extra_command_dirs, ("~/.local/bin", "~/.claude/local"))
        self.assertEqual(cc.internal_commands, ("claude-hook",))
        self.assertIs(registry.spec("claude"), cc)
        with self.assertRaises(KeyError):
            registry.spec("nope")
        with self.assertRaises(KeyError):
            registry.get("nope")
        # descriptor defaults are the config defaults
        c = Config()
        self.assertEqual((c.opencode_cmd, c.claude_cmd), (oc.default_command, cc.default_command))

    def test_uniqueness(self):
        oc = registry.spec("opencode")
        dup = {
            "id": ProviderSpec("opencode", "x", "m", "x_cmd", "x"),
            "command key": ProviderSpec("x", "x", "m", "opencode_cmd", "x"),
            "internal command": ProviderSpec("x", "x", "m", "x_cmd", "x", internal_commands=("claude-hook",)),
        }
        for name, extra in dup.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    registry.validate((*registry.SPECS, extra))
                with self.assertRaises(ValueError), registry.override(specs=(*registry.SPECS, extra)):
                    pass
                self.assertEqual(registry.ids(), ("opencode", "claude"))     # untouched
        with self.assertRaises(ValueError):
            registry.validate((ProviderSpec("", "x", "m", "k", "x"),))
        with self.assertRaises(ValueError):
            registry.validate((oc, "opencode"))                              # type: ignore[arg-type]
        self.assertEqual(registry.validate(registry.SPECS), registry.SPECS)
        self.assertEqual(registry.validate(()), ())

    def test_import_is_lazy(self):
        code = ("import sys\n"
                "import agentree.providers, agentree.providers.registry as r, agentree.providers.base, agentree.providers.command\n"
                "import agentree.config, agentree.types, agentree.model.snapshot, agentree.actions.api\n"
                "c = agentree.config.load_config('/nonexistent/agentree-config.toml')\n"
                "r.specs(); r.ids(); r.spec('claude'); r.internal_owner('claude-hook'); r.default_id(c)\n"
                "r.missing_command_message('claude', c); agentree.config.example_toml()\n"
                f"bad = [m for m in {CONCRETE!r} if m in sys.modules]\n"
                "print(','.join(bad) or 'clean')\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "clean")

    def test_override_with_instances(self):
        fake = FakeProvider(FAKE)
        oc = FakeProvider(registry.spec("opencode"))
        before = registry.SPECS
        with registry.override(specs=three_specs(), providers={"codex": fake, "opencode": oc}):
            self.assertEqual(registry.ids(), ("opencode", "claude", "codex"))
            self.assertIs(registry.get("codex"), fake)
            self.assertIs(registry.get("opencode"), oc)
            self.assertIs(registry.spec("codex"), FAKE)
            self.assertIs(registry.internal_owner("codex-hook"), FAKE)
            with registry.override(specs=(FAKE,), providers={"codex": oc}):  # nests
                self.assertEqual(registry.ids(), ("codex",))
                self.assertIs(registry.get("codex"), oc)
                with self.assertRaises(KeyError):
                    registry.get("opencode")
            self.assertIs(registry.get("codex"), fake)
        self.assertIs(registry.SPECS, before)
        self.assertEqual(registry.ids(), ("opencode", "claude"))
        self.assertIsNone(registry.internal_owner("codex-hook"))
        with self.assertRaises(KeyError):
            registry.get("codex")

    def test_override_restores_on_error(self):
        with self.assertRaises(RuntimeError), registry.override(specs=(FAKE,)):
            raise RuntimeError("boom")
        self.assertEqual(registry.ids(), ("opencode", "claude"))

    def test_get_imports_lazily_and_caches(self):
        calls = []
        mod = types.ModuleType(FAKE.module)

        def create_provider(spec):
            calls.append(spec)
            return FakeProvider(spec)
        mod.create_provider = create_provider
        with mock.patch.dict(sys.modules, {FAKE.module: mod}), registry.override(specs=three_specs(), providers={}):
            self.assertEqual(calls, [])
            p = registry.get("codex")
            self.assertIs(registry.get("codex"), p)
            self.assertEqual(calls, [FAKE])
            self.assertEqual(p.id, "codex")
            with registry.override(providers={"opencode": FakeProvider(registry.spec("opencode")),
                                              "claude": FakeProvider(registry.spec("claude"))}):
                self.assertEqual([x.id for x in registry.all_providers()], ["opencode", "claude", "codex"])
            self.assertEqual(len(calls), 2)                  # inner override had its own cache

    def test_internal_owner(self):
        self.assertIs(registry.internal_owner("claude-hook"), registry.spec("claude"))
        for cmd in ("claude", "opencode", "", "doctor"):
            self.assertIsNone(registry.internal_owner(cmd))

    def test_default_id(self):
        self.assertEqual(registry.default_id(Config()), "opencode")
        self.assertEqual(registry.default_id(Config(default_agent="claude")), "claude")
        self.assertEqual(registry.default_id(Config(default_agent="gpt")), "opencode")
        self.assertEqual(registry.default_id(None), "opencode")
        self.assertEqual(registry.default_id(types.SimpleNamespace(default_agent=3)), "opencode")
        with registry.override(specs=three_specs()):
            self.assertEqual(registry.default_id(types.SimpleNamespace(default_agent="codex")), "codex")
        with registry.override(specs=(FAKE,)):
            self.assertEqual(registry.default_id(Config()), "codex")       # opencode not registered

    def test_missing_command_message(self):
        self.assertEqual(registry.missing_command_message("claude", Config(claude_cmd="nope --x")),
                         "claude command not found: 'nope --x' (set claude_cmd in ~/.config/agentree/config.toml)")
        self.assertEqual(registry.missing_command_message("opencode", Config()),
                         "opencode command not found: 'opencode' "
                         "(set opencode_cmd in ~/.config/agentree/config.toml)")
        self.assertEqual(registry.missing_command_message("opencode", types.SimpleNamespace(opencode_cmd="oc")),
                         "opencode command not found: 'oc' (set opencode_cmd in ~/.config/agentree/config.toml)")
        self.assertIn("unknown agent provider 'zzz'", registry.missing_command_message("zzz", Config()))
        with registry.override(specs=three_specs()):
            cfg = config.from_mapping({"codex_cmd": "codex -q"})
            self.assertEqual(registry.missing_command_message("codex", cfg),
                             "codex command not found: 'codex -q' (set codex_cmd in ~/.config/agentree/config.toml)")


class ResolveForTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.env = mock.patch.dict(os.environ, {"HOME": self.home, "PATH": "/nonexistent"})
        self.env.start()
        self.which = mock.patch("agentree.actions.exec.which", return_value=None)
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.env.stop()
        self.tmp.cleanup()

    def exe(self, *parts):
        d = os.path.join(self.home, *parts[:-1])
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, parts[-1])
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(p, 0o755)
        return p

    def test_claude_extra_dirs(self):
        cc = registry.spec("claude")
        self.assertIsNone(command.resolve_for(cc, Config()))
        exe = self.exe(".local", "bin", "claude")
        self.assertEqual(command.resolve_for(cc, Config()), (exe,))
        self.assertEqual(command.resolve_for(cc, Config(claude_cmd="claude --verbose")), (exe, "--verbose"))
        self.assertEqual(command.resolve_for(cc, Config(claude_cmd="  ")), (exe,))       # blank -> "claude"
        self.assertEqual(command.resolve_for(cc, types.SimpleNamespace()), (exe,))       # duck cfg
        os.chmod(exe, 0o644)
        self.assertIsNone(command.resolve_for(cc, Config()))
        exe2 = self.exe(".claude", "local", "claude")
        self.assertEqual(command.resolve_for(cc, Config()), (exe2,))
        self.assertIsNone(command.resolve_for(cc, Config(claude_cmd="'unterminated")))

    def test_opencode(self):
        oc = registry.spec("opencode")
        self.assertEqual(command.resolve_for(oc, Config(opencode_cmd="/bin/sh -x")), ("/bin/sh", "-x"))
        self.assertIsNone(command.resolve_for(oc, Config()))                # "opencode" not on PATH
        auto = self.exe("bin", "opencode-auto")
        self.assertEqual(command.resolve_for(oc, Config(opencode_cmd="~/bin/opencode-auto")), (auto,))
        self.assertIsNone(command.resolve_for(oc, Config(opencode_cmd="")))  # blank -> "opencode" (not found)
        self.exe(".local", "bin", "opencode")                               # opencode has no extra dirs
        self.assertIsNone(command.resolve_for(oc, Config(opencode_cmd="opencode")))
        with mock.patch("agentree.actions.exec.which", side_effect=lambda n: f"/usr/bin/{n}"):
            self.assertEqual(command.resolve_for(oc, Config(opencode_cmd="")), ("/usr/bin/opencode",))
            self.assertEqual(command.resolve_for(oc, Config(opencode_cmd=""), fallback="oc2"), ("/usr/bin/oc2",))

    def test_matches_resolve_command(self):
        cc = registry.spec("claude")
        self.exe(".local", "bin", "claude")
        for cmd in ("claude", "claude -p x", "~/.local/bin/claude", "nope"):
            with self.subTest(cmd):
                self.assertEqual(command.resolve_for(cc, Config(claude_cmd=cmd)),
                                 command.resolve_command(cmd, "claude", extra_dirs=cc.extra_command_dirs))

    def test_fake_provider(self):
        exe = self.exe(".codex", "bin", "codex")
        with registry.override(specs=three_specs()):
            self.assertEqual(command.resolve_for(FAKE, Config()), (exe, "--full-auto"))
            cfg = config.from_mapping({"codex_cmd": "codex --quiet"})
            self.assertEqual(command.resolve_for(FAKE, cfg), (exe, "--quiet"))


if __name__ == "__main__":
    unittest.main()
