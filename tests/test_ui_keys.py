"""Key decoder: CSI/SS3, kitty CSI-u, paste, lone Esc, Alt combos, mouse."""
import unittest

from agentree.ui.term import Key, KeyDecoder


def names(*chunks, flush=True):
    d = KeyDecoder()
    out = []
    for c in chunks:
        out += d.feed(c)
    if flush and d.pending:
        out += d.flush()
    return [k.name for k in out]


class Decoder(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(names(b"jk W/q"), ["j", "k", " ", "W", "/", "q"])
        self.assertEqual(names("é→".encode()), ["é", "→"])

    def test_agent_here_keys(self):
        # o opens "agent here"; the chooser uses ↑/↓/j/k, ↵ and esc/q
        self.assertEqual(names(b"o\x1b[A\x1b[Bjk\rq"), ["o", "up", "down", "j", "k", "enter", "q"])
        self.assertEqual(names(b"\x1b"), ["esc"])

    def test_controls(self):
        self.assertEqual(names(b"\r\n\t\x7f\x08\x03\x0e\x15"),
                         ["enter", "enter", "tab", "backspace", "backspace", "ctrl+c", "ctrl+n", "ctrl+u"])

    def test_arrows_csi_ss3(self):
        self.assertEqual(names(b"\x1b[A\x1b[B\x1b[C\x1b[D"), ["up", "down", "right", "left"])
        self.assertEqual(names(b"\x1bOA\x1bOB\x1bOH\x1bOF"), ["up", "down", "home", "end"])
        self.assertEqual(names(b"\x1b[1;5A"), ["ctrl+up"])

    def test_home_end_pages(self):
        self.assertEqual(names(b"\x1b[H\x1b[F\x1b[1~\x1b[4~\x1b[7~\x1b[8~"),
                         ["home", "end", "home", "end", "home", "end"])
        self.assertEqual(names(b"\x1b[5~\x1b[6~\x1b[3~"), ["pgup", "pgdn", "delete"])
        self.assertEqual(names(b"\x1b[Z"), ["backtab"])

    def test_lone_escape_needs_timeout(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b"), [])
        self.assertTrue(d.pending)
        self.assertEqual(d.flush(), [Key("esc")])
        self.assertFalse(d.pending)

    def test_split_sequence_across_reads(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b["), [])
        self.assertEqual([k.name for k in d.feed(b"B")], ["down"])
        self.assertEqual(d.feed(b"\xe2\x86"), [])          # partial utf-8
        self.assertEqual([k.name for k in d.feed(b"\x92")], ["→"])

    def test_double_escape(self):
        self.assertEqual(names(b"\x1b\x1b"), ["esc", "esc"])

    def test_alt_ignored(self):
        self.assertEqual(names(b"\x1bj"), [])
        self.assertEqual(names(b"\x1b[1;3A"), [])          # alt+up
        self.assertEqual(names(b"\x1b[106;3u"), [])        # kitty alt+j

    def test_kitty_csi_u(self):
        self.assertEqual(names(b"\x1b[13;5u"), ["ctrl+enter"])
        self.assertEqual(names(b"\x1b[27u"), ["esc"])
        self.assertEqual(names(b"\x1b[110;5u"), ["ctrl+n"])
        self.assertEqual(names(b"\x1b[99;5u"), ["ctrl+c"])
        self.assertEqual(names(b"\x1b[13u\x1b[9;2u"), ["enter", "backtab"])
        self.assertEqual(names(b"\x1b[106;1:3u"), [])       # key release
        self.assertEqual(names(b"\x1b[27;5;110~"), ["ctrl+n"])  # xterm modifyOtherKeys

    def test_bracketed_paste(self):
        d = KeyDecoder()
        out = d.feed(b"\x1b[200~feat/x\x1b[")
        self.assertEqual(out, [])
        out = d.feed(b"201~j")
        self.assertEqual(out, [Key("paste", "feat/x"), Key("j", "j")])
        self.assertEqual(names(b"\x1b[200~a\x1bb\x1b[201~"), ["paste"])

    def test_terminal_responses_dropped(self):
        self.assertEqual(names(b"\x1b[?1u\x1b[?62;c"), [])

    def test_mouse(self):
        d = KeyDecoder()
        ks = d.feed(b"\x1b[<0;5;7M\x1b[<0;5;7m\x1b[<64;1;1M\x1b[<65;1;1M")
        self.assertEqual([(k.name, k.x, k.y) for k in ks], [("click", 4, 6), ("wheelup", 0, 0), ("wheeldown", 0, 0)])


if __name__ == "__main__":
    unittest.main()
