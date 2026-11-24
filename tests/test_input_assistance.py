"""⌨️ What people type: real argv, shell completion, PTY input, typo hints and calculator input.

- Real argv, shell completion, PTY input and missing-runtime regressions.
- Real shell expansion, arithmetic compatibility, and numeric input boundaries.
"""
import importlib
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vendor.cli_args import run_cli


# ─── ⌨️ completion · typo hints · PTY · missing runtimes ───

class TestInputAssistance(unittest.TestCase):
    def setUp(self):
        # Let test_olnm finish its isolated environment setup during discovery.
        # Importing olnm at collection time would capture the caller's real HOME.
        global olnm
        olnm = importlib.import_module('olnm')
        self.tmp = tempfile.TemporaryDirectory(prefix="olnm assist ")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.env = dict(os.environ, OLNM_HOME=str(self.work / "state"),
                        DIAL_FILE=str(self.work / "dial"), GIT_CONFIG_GLOBAL=str(self.work / "gitconfig"))
        self.source = ROOT / "olnm.py"

    def cli(self, *args, input=None):
        return run_cli(self.source, *args, stdin=input, env=self.env, cwd=self.work, timeout=15)

    def shell(self, script, shell="bash", env=None):
        env = dict(env or self.env, OLNM_PY=str(self.source))
        return subprocess.run([shell, "-c", 'eval "$(python3 "$OLNM_PY" init ' + shell + ')"\n' + script],
                              text=True, capture_output=True, env=env, cwd=self.work, timeout=15)

    def test_offline_install_failure_is_not_hidden_by_sync(self):
        uv = self.work / 'uv'
        uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UV_TEST_LOG"\nexit 7\n')
        uv.chmod(0o755)
        (self.work / 'wheels').mkdir()
        log = self.work / 'uv.log'
        env = dict(self.env, PATH=str(self.work) + os.pathsep + os.environ['PATH'], UV_TEST_LOG=str(log))
        result = self.shell('uvoffline', env=env)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(log.read_text().splitlines(), ['pip install --no-index --find-links wheels -r requirements.txt'])

    def test_uvbundle_stops_if_export_fails(self):
        uv = self.work / 'uv'
        uv.write_text('#!/bin/sh\nexit 7\n')
        uv.chmod(0o755)
        env = dict(self.env, PATH=str(self.work) + os.pathsep + os.environ['PATH'])
        result = self.shell('uvbundle', env=env)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertFalse((self.work / 'wheels').exists())

    def test_jsonf_preserves_jq_failure(self):
        jq = self.work / 'jq'
        jq.write_text('#!/bin/sh\nexit 7\n')
        jq.chmod(0o755)
        env = dict(self.env, PATH=str(self.work) + os.pathsep + os.environ['PATH'])
        result = self.shell('jsonf', env=env)
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_uvbundle_reports_download_failure(self):
        uv = self.work / 'uv'
        uv.write_text('#!/bin/sh\nif [ "$1" = export ]; then : > requirements.txt; exit 0; fi\nexit 9\n')
        uv.chmod(0o755)
        env = dict(self.env, PATH=str(self.work) + os.pathsep + os.environ['PATH'])
        result = self.shell('uvbundle', env=env)
        self.assertEqual(result.returncode, 9, result.stderr)

    def test_uv_wrappers_do_not_repeat_sync_or_hide_install_failure(self):
        uv = self.work / 'uv'
        uv.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UV_TEST_LOG"\n'
                      'if [ "$1 $2" = "python install" ]; then exit 7; fi\n')
        uv.chmod(0o755)
        log = self.work / 'uv.log'
        env = dict(self.env, PATH=str(self.work) + os.pathsep + os.environ['PATH'], UV_TEST_LOG=str(log))
        result = self.shell('uvadd demo; uvpys 3.11 3.12; echo "exit=$?"; uvpys', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('exit=7', result.stdout)
        self.assertEqual(log.read_text().splitlines(), ['add demo', 'python install 3.11 3.12', 'python list --only-installed'])

    def test_completion_context_and_prefix(self):
        cases = [
            (("olnm", "ru"), ["run"]),
            (("olnm", "run", "pyc"), ["pyclean", "pycsv"]),
            (("olnm", "hub", "i"), ["init", "install", "issue"]),  # NagoyAction owns everything after SUB
            (("hubdial", "s"), ["status", "sync"]),
            (("olnm", "dial", "m"), ["mirror"]),
            (("olnm_dial", ""), ["0", "1", "2", "3", "github", "local", "mirror", "offline"]),
            (("olnm_offline", "py"), ["python"]),
            (("olnm_like", "pyc"), ["pyclean", "pycsv"]),
            (("olnm", "tools", "ru"), ["rust", "rustc", "rustup"]),
            (("olnm", "call", "read_"), ["read_dial"]),
        ]
        for words, expected in cases:
            with self.subTest(words=words):
                with mock.patch.object(olnm.subprocess, "run", side_effect=AssertionError("completion executed a command")):
                    self.assertEqual(olnm._completion(list(words)), expected)
                result = self.cli("complete", "--", *words)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)
        result = self.cli("complete", "--", "olnm", "ls", "--off")
        self.assertEqual(result.stdout.strip(), "--offline")
        self.assertEqual(self.cli("complete", "--", "olnm", "run", "calc", "x").stdout, "")

    def test_bash_completion_registration_and_replies(self):
        result = self.shell('complete -p olnm; COMP_WORDS=(olnm hub s); COMP_CWORD=2; _olnm_complete_bash; printf "%s\\n" "${COMPREPLY[@]}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-F _olnm_complete_bash", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-2:], ["status", "sync"])

    @unittest.skipUnless(shutil.which("zsh"), "zsh unavailable")
    def test_zsh_completion_registration_and_replies(self):
        result = self.shell('autoload -Uz compinit; compinit -u -D; eval "$(python3 "$OLNM_PY" init zsh)"; '
                            'print -r -- "${_comps[olnm]}"; words=(olnm hub s); CURRENT=3; '
                            'compadd() { printf "%s\\n" "$@"; }; _olnm_complete_zsh', shell="zsh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["_olnm_complete_zsh", "--", "status", "sync"])

    def test_hybrid_shell_preserves_every_section_and_supports_cli(self):
        for section in olnm._sections():
            result = self.shell('olnm ' + section)
            expected = self.cli("ls", section)
            self.assertEqual(result.stdout, expected.stdout, section)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.shell("olnm calc 6 + 7; olnm run calc 6 mul 7")
        self.assertEqual(result.stdout.splitlines(), ["13", "42"])
        self.assertIn('arithmetic', self.shell('olnm --help').stdout)

    def test_typos_suggest_without_executing_or_changing_input(self):
        for args, suggestion in [(('rn',), 'run'), (('hub', 'isue'), 'issue'),
                                 (('dial', 'mirorr'), 'mirror')]:
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(suggestion, result.stderr)
            self.assertIn("did you mean", result.stderr)
            self.assertFalse((self.work / "state").exists())
        result = self.shell('olnm rn')
        self.assertEqual(result.returncode, 2)
        self.assertIn('run', result.stderr)
        self.assertIn('did you mean', result.stderr)
        result = self.cli('call', 'read_dail')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('read_dial', result.stderr)
        self.assertIn('did you mean', result.stderr)

    def test_arithmetic_and_resource_limits(self):
        for expression, expected in [('6*7', 42), ('(6+7)*2', 26), ('sqrt(81)', 9), ('-2**3', -8),
                                     ('round(pi,2)', 3.14), ('7//2 + 7%2', 4)]:
            self.assertEqual(olnm.calc(expression), expected)
        for bad in ['__import__("os")', '(1).__class__', '[1]', 'True', '2**1001', '0.0/0', '"hello"',
                    '2**-1001', '1e999', 'sqrt(-1)', '(2**1000)**1000', '1+'*100+'1', '1'*1001]:
            with self.subTest(expression=bad), self.assertRaises((ValueError, SyntaxError, ArithmeticError)):
                olnm.calc(bad)

    def test_calculator_stdin_quotes_globs_and_errors(self):
        (self.work / "6x7").touch()
        self.assertEqual(self.cli('calc', input='6*7\n').stdout.strip(), '42')
        self.assertEqual(self.cli('calc', '--', '-2', '*', '3').stdout.strip(), '-6')
        error = self.cli('calc', input='6*\n')
        self.assertEqual(error.returncode, 2)
        self.assertIn('Try: olnm calc', error.stderr)
        # The no-argument calc shell function uses the same input route.
        result = subprocess.run(['bash', '-c', 'printf "%s\\n" "(6+7)*2" | python3 "$OLNM_PY" run calc'],
                                env=dict(self.env, OLNM_PY=str(self.source)), cwd=self.work,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.stdout.strip(), '26', result.stderr)

    def test_tty_prompt_does_not_require_shell_quoting(self):
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen([sys.executable, self.source, 'calc'], stdin=slave,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=self.work, env=self.env, text=True)
            os.close(slave)
            slave = None
            os.write(master, b'(6+7)*2\n')
            out, err = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(out.strip(), '26')
            self.assertIn('no surrounding quotes', err)
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)

    def test_real_terminal_tab_completes_calculator_input(self):
        try:
            import readline  # noqa: F401
        except ImportError:
            self.skipTest('optional readline unavailable')
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, self.source, 'calc'], stdin=slave,
                                stdout=slave, stderr=slave, cwd=self.work,
                                env=dict(self.env, INPUTRC='/dev/null'))
        os.close(slave)
        output = b''
        try:
            while b'no surrounding quotes' not in output:
                self.assertTrue(select.select([master], [], [], 10)[0], output)
                output += os.read(master, 8192)
            os.write(master, b'sq\t81)\n')
            proc.wait(timeout=10)
            while select.select([master], [], [], 1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            self.assertEqual(proc.returncode, 0, output)
            self.assertIn(b'sqrt(81)', output)
            self.assertIn(b'9.0', output)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)

    def test_invalid_json_has_actionable_message_and_no_traceback(self):
        for args in [('call', 'like', 'broken'), ('call', 'like', '{}'),
                     ('call', 'like', '[]', '--kwargs', '[]')]:
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('JSON', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_missing_runtimes_and_install_after_init(self):
        bindir = self.work / 'bin'; bindir.mkdir()
        (bindir / 'python3').symlink_to(sys.executable)
        bash = shutil.which('bash')
        (bindir / 'bash').symlink_to(bash)
        env = dict(self.env, PATH=str(bindir), OLNM_PY=str(self.source))
        for shell in ['bash'] + (['zsh'] if shutil.which('zsh') else []):
            executable = shutil.which(shell)
            for name, expected in [('nscripts', 'nodejs.org'), ('bi', 'bun.sh')]:
                result = subprocess.run([executable, '-c', 'eval "$(python3 "$OLNM_PY" init ' + shell + ')"; ' + name],
                                        cwd=self.work, env=env, text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 127, result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertIn('not found on PATH', result.stderr)
        # Missing npm must be detected before a cleanup removes project files.
        modules = self.work / 'node_modules'; modules.mkdir()
        result = subprocess.run([bash, '-c', 'eval "$(python3 "$OLNM_PY" init bash)"; nclean'],
                                env=env, cwd=self.work, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertTrue(modules.is_dir())
        # Runtime checks happen per call: installation after init requires no reload.
        result = subprocess.run([bash, '-c', 'eval "$(python3 "$OLNM_PY" init bash)"; '
                                 'nscripts </dev/null >/dev/null 2>&1; '
                                 'printf \'#!/bin/sh\\nprintf "42\\\\n"\\n\' > "$1/node"; '
                                 '/bin/chmod +x "$1/node"; nscripts </dev/null', 'test', str(bindir)],
                                cwd=self.work, env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '42')
        rust = self.cli('tools', 'rust')
        self.assertIn('rustup', rust.stdout)
        self.assertIn('rust-toolchain.toml', rust.stdout)

    def test_init_and_run_handle_shell_metacharacters_in_source_path(self):
        folder = self.work / 'literal $(touch INJECTED) " quote'; folder.mkdir()
        source = folder / 'olnm.py'; shutil.copyfile(self.source, source)
        result = run_cli(source, 'run', 'calc', '6*7', cwd=self.work, env=self.env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '42')
        self.assertFalse((self.work / 'INJECTED').exists())

    def test_update_without_source_explains_each_install_route(self):
        result = self.shell('unset OLNM_URL; olnm_update')
        self.assertEqual(result.returncode, 1)
        self.assertIn('File install', result.stderr)
        self.assertIn('uv tool upgrade', result.stderr)


# ─── 🧮 calculator through real shells ───

SOURCE = ROOT / 'olnm.py'


class TestCalculator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='olnm calculator ')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.env = dict(os.environ, OLNM_PY=str(SOURCE), PYTHONPATH=str(ROOT),
                        OLNM_HOME=str(self.work / 'state'), DIAL_FILE=str(self.work / 'dial'))
        # Wildcards must actually match: do not test only an empty directory.
        for name in ('alpha', 'two words', '2**10'):
            (self.work / name).touch()

    def execute(self, argv):
        return subprocess.run(argv, cwd=self.work, env=self.env, text=True,
                              capture_output=True, timeout=30)

    def shell(self, shell, command):
        if not shutil.which(shell):
            self.skipTest(f'{shell} unavailable')
        return self.execute([shell, '-f' if shell == 'zsh' else '--noprofile', '-c',
                             f'eval "$(python3 "$OLNM_PY" init {shell})"\n{command}'])

    def check(self, result, output, code=0):
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(result.stdout.strip(), output, result.stderr)

    def matrix(self, shell):
        cases = [('6 + 7', '13'), ('6 mul 7', '42'), ('6 div 4', None),
                 ('2 pow 10', '1024'), ('-6 mul 7', '-42'),
                 ('6 minus -7', '13'), ('lpar 6 plus 7 rpar mul 2', '26'),
                 ('1.5e2 div 3', None), ('7 mod 4', '3'), ('6mul7', None)]
        for engine in ('calc', 'calc --engine node'):
            if engine == 'calc --engine node' and not shutil.which('node'):
                continue
            for expression, want in cases:
                with self.subTest(shell=shell, engine=engine, expression=expression):
                    result = self.shell(shell, f'{engine} {expression}')
                    if expression == '6mul7':
                        self.assertEqual(result.returncode, 1)
                    elif want is None:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(float(result.stdout), 1.5 if expression == '6 div 4' else 50)
                    else:
                        self.check(result, want)

    def test_bash_word_matrix(self):
        self.matrix('bash')

    def test_zsh_word_matrix(self):
        self.matrix('zsh')

    def test_quoted_space_expression(self):
        for shell in ('bash', 'zsh'):
            self.check(self.shell(shell, "calc '6 mul 7'"), '42')

    def test_glob_failure_is_before_function_entry(self):
        for shell in ('bash', 'zsh'):
            for engine in ('calc', 'calc --engine node'):
                with self.subTest(shell=shell, engine=engine):
                    result = self.shell(shell, f'{engine} 6 * 7')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(result.stderr.startswith('calc:'), result.stderr)
            result = self.shell(shell, 'calc 8 *nomatch* 7')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('no matches found' if shell == 'zsh' else 'calc:', result.stderr)

    def test_python_results_and_exit_codes(self):
        for expression in ('6*7', '6 + 7', '2**10', '-6*7', '1/0', '1 +'):
            with self.subTest(expression=expression):
                old = self.execute([sys.executable, '-c', f'from math import *; print({expression})'])
                new = self.execute([sys.executable, str(SOURCE), 'calc', expression])
                self.assertEqual((new.returncode, new.stdout), (old.returncode, old.stdout))

    @unittest.skipUnless(shutil.which('node'), 'requires node')
    def test_node_results_and_exit_codes(self):
        for expression in ('6*7', '6 + 7', '2**10', '1/0', '1 +', '-6*7'):
            with self.subTest(expression=expression):
                old = self.execute(['node', '-p', '(' + expression + ')'])
                new = self.execute([sys.executable, str(SOURCE), 'calc', '--engine', 'node', expression])
                self.assertEqual((new.returncode, new.stdout), (old.returncode, old.stdout))

    def test_numeric_mode_rejects_code_and_non_arithmetic(self):
        for engine in ('python', 'node'):
            for expression in ('', '__import__("os").system("touch owned")',
                               'process.exit(0)', '1; 2', '1 # comment', 'True',
                               '"hello"', '[1]', '(1,2)', '1j', '2 ^ 3',
                               '2 << 3', '1 if 1 else 2', '1+' * 3000):
                with self.subTest(engine=engine, expression=expression):
                    result = self.execute([sys.executable, str(SOURCE), 'calc', '--engine', engine, expression])
                    self.check(result, '', 1)
                    self.assertTrue(result.stderr.startswith('calc:'))
        self.assertFalse((self.work / 'owned').exists())

    def test_received_shell_metacharacters_are_not_re_evaluated(self):
        for shell in ('bash', 'zsh'):
            result = self.shell(shell, "calc '$(touch owned)'")
            self.check(result, '', 1)
            self.assertFalse((self.work / 'owned').exists())

    def test_floor_division_and_runtime_semantics(self):
        self.check(self.shell('bash', 'calc -7 floordiv 2'), '-4')
        self.check(self.shell('bash', 'calc -7 mod 4'), '1')
        self.check(self.shell('bash', 'calc --engine node -7 mod 4'), '-3')
        self.check(self.shell('bash', 'calc --engine node 7 floordiv 2'), '', 1)
        self.check(self.shell('bash', 'calc 1 div 0'), '', 1)
        self.check(self.shell('bash', 'calc --engine node 1 div 0'), 'Infinity')
        self.check(self.shell('bash', 'calc -2 pow 2'), '-4')
        self.check(self.shell('bash', 'calc --engine node -2 pow 2'), '', 1)
        self.check(self.shell('bash', 'calc --engine node lpar -2 rpar pow 2'), '4')

    def test_python_one_liner_without_formula_quotes(self):
        self.check(self.execute([sys.executable, '-c',
            'import olnm,sys; sys.exit(olnm.calculate(*sys.argv[1:]))',
            '6', 'mul', '7']), '42')

    def test_run_arguments_are_not_globbed_again(self):
        self.check(self.execute([sys.executable, str(SOURCE), 'run', 'calc', '6', '*', '7']), '42')

    @unittest.skipUnless(shutil.which('node'), 'requires node')
    def test_node_caller_uses_argument_array(self):
        self.check(self.execute(['node', '-e',
            'const {execFileSync}=require("node:child_process"); process.stdout.write(execFileSync(process.argv[1],[process.env.OLNM_PY,"calc","6","*","7"],{encoding:"utf8"}))',
            sys.executable]), '42')

    def test_engine_options_help_and_missing_node(self):
        self.check(self.execute([sys.executable, str(SOURCE), 'calc', '--engine=python', '-6*7']), '-42')
        self.check(self.execute([sys.executable, str(SOURCE), 'calc', '--', '-6*7']), '-42')
        self.assertEqual(self.execute([sys.executable, str(SOURCE), 'calc', '--engine']).returncode, 2)
        self.assertEqual(self.execute([sys.executable, str(SOURCE), 'calc', '--engine', 'bad', '1']).returncode, 2)
        self.assertIn('usage:', self.execute([sys.executable, str(SOURCE), 'calc', '--help']).stdout)
        import olnm
        from unittest import mock
        with mock.patch.object(olnm.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertEqual(olnm.calculate('1', engine='node'), 127)
        self.assertEqual(olnm.calculate('1', engine='bad'), 1)

    def test_words_backend_needs_no_bash(self):
        self.check(self.execute([sys.executable, str(SOURCE), 'calc', '-6', 'mul', '7']), '-42')


if __name__ == "__main__":
    unittest.main()
