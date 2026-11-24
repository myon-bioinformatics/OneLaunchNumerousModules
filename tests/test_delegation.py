"""🤝 Commands olnm hands to another tool, observed through fake executables at real process boundaries.

- 🏠 The local forge and workflow lint live in NagoyAction: olnm hands `hub`, `actionlint` (and a dial move with a forge) to `nagoyaction`.
- mise adapter contracts; a fake executable observes real process boundaries.
- Default stdlib commands delegate to pinned uv tools without registration.
- Default yourself command works from a standalone copy, without installation.
"""
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import vendor.fleet_check as fleet_check
from vendor.cli_args import Command, run_cli, run_command, run_node


# ─── 🏠 NagoyAction: hub · actionlint · dial with a forge ───

FAKE_NAGOYACTION = "#!{py}\nimport json,sys\nfrom pathlib import Path\nPath({log!r}).write_text(json.dumps(sys.argv[1:]))\nsys.exit(7)\n"


class ForgeDelegationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.log = self.root / "arguments.json"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "nagoyaction"
        fake.write_text(FAKE_NAGOYACTION.format(py=sys.executable, log=str(self.log)))
        fake.chmod(0o755)
        self.home = self.root / "state"
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        OLNM_HOME=str(self.home), DIAL_FILE=str(self.home / "dial"))
        self.env.pop("NAGOYACTION", None)

    def olnm(self, *args, **env):
        # run_cli decodes UTF-8 and does not follow the locale the way text=True did.
        child = dict(self.env, **env)
        child["PYTHONIOENCODING"] = "utf-8"
        return run_cli(ROOT / "olnm.py", *args, env=child)

    def forwarded(self):
        return json.loads(self.log.read_text())

    def with_forge(self, port=3123):
        (self.home / "forge").mkdir(parents=True)
        (self.home / "forge" / "state.json").write_text(json.dumps({"port": port}))

    def test_hub_forwards_exact_arguments_and_exit_code(self):
        result = self.olnm("hub", "issue", "new", "title with spaces", "--body", "line one")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.forwarded(), ["hub", "issue", "new", "title with spaces", "--body", "line one"])
        self.assertEqual(self.olnm("hub", "--help").returncode, 7)
        self.assertEqual(self.forwarded(), ["hub", "--help"])

    def test_dial_with_a_forge_is_moved_by_nagoyaction(self):
        self.with_forge()
        result = self.olnm("dial", "mirror")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.forwarded(), ["hub", "dial", "2"])
        self.assertFalse((self.home / "dial").exists())  # NagoyAction writes it after moving the origins

    def test_dial_without_a_forge_stays_in_olnm(self):
        result = self.olnm("dial", "local")
        self.assertEqual(result.stdout, "🎛️  → 🏠 local (1)  🌍 one-liners stubbed\n")
        self.assertEqual((self.home / "dial").read_text(), "1\n")
        self.assertFalse(self.log.exists())

    def test_nagoyaction_py_checkout_can_be_named(self):
        script = self.root / "nagoyaction.py"
        script.write_text(FAKE_NAGOYACTION.format(py=sys.executable, log=str(self.log)))
        result = self.olnm("hub", "status", NAGOYACTION=str(script), PATH="/usr/bin:/bin")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.forwarded(), ["hub", "status"])

    def test_carried_nagoyaction_py_is_the_whole_tool(self):
        # 🎒 `hub bundle` carries nagoyaction.py itself (NagoyAction #58), so hub and actionlint go through as from a checkout.
        script = self.root / "carry" / "nagoyaction.py"
        script.parent.mkdir()
        script.write_text(FAKE_NAGOYACTION.format(py=sys.executable, log=str(self.log)))
        result = self.olnm("hub", "repo", "ls", NAGOYACTION=str(script), PATH="/usr/bin:/bin")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.forwarded(), ["hub", "repo", "ls"])
        self.assertEqual(self.olnm("actionlint", NAGOYACTION=str(script), PATH="/usr/bin:/bin").returncode, 7)
        self.assertEqual(self.forwarded(), ["actionlint"])

    def test_actionlint_forwards_exact_arguments_and_exit_code(self):
        result = self.olnm("actionlint", "--strict", "--root", "dir with space")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.forwarded(), ["actionlint", "--strict", "--root", "dir with space"])
        self.assertEqual(self.olnm("actionlint", "--help").returncode, 7)  # NagoyAction's own help
        self.assertEqual(self.forwarded(), ["actionlint", "--help"])
        script = f'eval "$(python3 {ROOT / "olnm.py"} init bash)"\nghlint --all --repeat 3; echo "rc=$?"'
        result = subprocess.run(["bash", "-c", script], env=self.env, text=True, capture_output=True)
        self.assertIn("rc=7", result.stdout, result.stderr)
        self.assertEqual(self.forwarded(), ["actionlint", "--all", "--repeat", "3"])

    def test_actionlint_without_nagoyaction(self):
        result = self.olnm("actionlint", PATH="/usr/bin:/bin")
        self.assertEqual(result.returncode, 127)
        self.assertIn("nagoyaction actionlint", result.stderr)
        self.assertIn("uv tool install", result.stderr)
        helped = self.olnm("actionlint", "--help", PATH="/usr/bin:/bin")
        self.assertEqual(helped.returncode, 0)
        self.assertIn("usage: olnm actionlint", helped.stdout)

    def test_missing_nagoyaction_explains_how_to_install(self):
        result = self.olnm("hub", "status", PATH="/usr/bin:/bin")
        self.assertEqual(result.returncode, 127)
        self.assertIn("nagoyaction hub", result.stderr)
        self.assertIn("uv tool install", result.stderr)
        self.with_forge()
        (self.home / "dial").write_text("3\n")
        result = self.olnm("dial", "offline", PATH="/usr/bin:/bin")
        self.assertEqual(result.returncode, 127)
        self.assertIn("dial unchanged", result.stderr)
        self.assertEqual((self.home / "dial").read_text(), "3\n")

    def test_shell_routes_gh_one_liners_to_the_forge_at_mirror_and_below(self):
        self.with_forge()
        (self.home / "dial").write_text("2\n")
        (self.bin / "xdg-open").write_text("#!/bin/sh\nexit 1\n")  # no browser here: hubopen prints the URL
        (self.bin / "xdg-open").chmod(0o755)
        script = f'eval "$(python3 {ROOT / "olnm.py"} init bash)"\nghisn crash bug; echo "rc=$?"; hubopen'
        result = subprocess.run(["bash", "-c", script], env=self.env, text=True, capture_output=True)
        self.assertIn("rc=7", result.stdout, result.stderr)
        self.assertEqual(self.forwarded(), ["hub", "issue", "new", "crash", "--label", "bug"])
        self.assertIn("http://localhost:3123/", result.stdout)
        (self.home / "dial").write_text("3\n")
        result = subprocess.run(["bash", "-c", f'eval "$(python3 {ROOT / "olnm.py"} init bash)"\n_olnm_hub && echo HUB || echo GH'],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.stdout.strip(), "GH")


# ─── 🧰 mise adapter ───

class TestMise(unittest.TestCase):
    def setUp(self):
        global olnm
        olnm = importlib.import_module('olnm')
        self.tmp = tempfile.TemporaryDirectory(prefix='olnm mise ')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.bin = self.work / 'bin'; self.bin.mkdir()
        self.fake = self.bin / 'mise'
        self.fake.write_text('#!' + sys.executable + '\n' + '''import json,os,sys
if os.environ.get("FAKE_MISE_STDERR"):
    print("mise diagnostic", file=sys.stderr)
print(json.dumps({"argv":sys.argv[1:], "cwd":os.getcwd(), "stdin":sys.stdin.read(),
                  "auto":os.environ.get("MISE_AUTO_INSTALL"),
                  "exec_auto":os.environ.get("MISE_EXEC_AUTO_INSTALL")}))
sys.exit(int(os.environ.get("FAKE_MISE_EXIT", "0")))
''')
        self.fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        PYTHONPATH=str(ROOT), OLNM_PY=str(ROOT/'olnm.py'),
                        OLNM_HOME=str(self.work/'state'), DIAL_FILE=str(self.work/'dial'),
                        GIT_CONFIG_GLOBAL=str(self.work/'gitconfig'))

    def cli(self, *args, input='payload\n', env=None):
        return run_cli(ROOT / 'olnm.py', *args, cwd=self.work, stdin=input, env=env or self.env, timeout=15)

    def test_direct_cli_preserves_argv_streams_cwd_and_exit(self):
        args = ('exec', 'node@26', '--', 'node', '-e', 'console.log("hello world")', '日本語', '$(touch INJECTED)', "a'b")
        result = self.cli('mise', *args, env=dict(self.env, FAKE_MISE_EXIT='7', FAKE_MISE_STDERR='1'))
        self.assertEqual(result.returncode, 7)
        observed = result.json()
        self.assertEqual(observed['argv'], list(args))
        self.assertEqual(observed['cwd'], str(self.work))
        self.assertEqual(observed['stdin'], 'payload\n')
        self.assertIn('mise diagnostic', result.stderr)
        self.assertFalse((self.work/'INJECTED').exists())
        self.assertEqual(self.cli('mise', '--', '--version').json()['argv'], ['--version'])

    def test_python_api_works_without_bash(self):
        # No bash or other runtime is on PATH; both Python executable paths are absolute.
        proc = run_command(Command((sys.executable, '-c',
            'import olnm,sys;sys.exit(olnm.mise("exec","--","uv","run","python","-V"))')),
            cwd=self.work, env=dict(self.env, PATH=str(self.bin)), stdin='', timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['argv'], ['exec','--','uv','run','python','-V'])

    def test_shell_wrappers_from_bash_zsh_and_run(self):
        cases = {'miseuse': ['use'], 'miseglobal':['use','--global'], 'miseinstall':['install'],
                 'miseexec':['exec'], 'miseoffline':['exec'], 'misels':['ls'],
                 'miselock':['lock'], 'misedoctor':['doctor']}
        for name, prefix in cases.items():
            with self.subTest(name=name):
                result = self.cli('run', name, 'node@26', '--', 'node', 'arg with spaces')
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = result.json()
                self.assertEqual(observed['argv'], prefix+['node@26','--','node','arg with spaces'])
                if name == 'miseoffline':
                    self.assertEqual((observed['auto'], observed['exec_auto']), ('false','false'))
                self.assertEqual(observed['stdin'], 'payload\n')
        for shell in ['bash'] + (['zsh'] if shutil.which('zsh') else []):
            result = subprocess.run([shell,'-c', 'eval "$(python3 "$OLNM_PY" init '+shell+')"; miseexec node@26 -- node -v'],
                                    cwd=self.work, env=self.env, input='', capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['argv'], ['exec','node@26','--','node','-v'])

    def test_missing_mise_is_actionable_and_installs_nothing(self):
        self.fake.unlink()
        # Isolate PATH rather than depending on whether the host has mise installed.
        (self.bin/'python3').symlink_to(sys.executable)
        (self.bin/'bash').symlink_to(shutil.which('bash'))
        env = dict(self.env, PATH=str(self.bin))
        for args in [('mise','exec','--','node','-v'), ('run','miseexec','--','node','-v')]:
            result = self.cli(*args, env=env)
            self.assertEqual(result.returncode, 127, result.stderr)
            self.assertIn('mise.jdx.dev', result.stderr)
            self.assertEqual(result.stdout, '')
        self.assertFalse((self.work/'mise.toml').exists())
        self.assertFalse((self.work/'state').exists())

    def test_dial_gates_known_network_wrappers_and_keeps_local_route(self):
        (self.work/'dial').write_text('0\n')
        result = self.cli('run','miseexec','--','node','-v')
        self.assertEqual(result.returncode, 1)
        self.assertIn('needs dial', result.stderr)
        local = self.cli('run','miseoffline','--','node','-v')
        self.assertEqual(local.returncode, 0, local.stderr)
        self.assertEqual(local.json()['auto'], 'false')
        for name in ['misels','misedoctor']:
            self.assertEqual(self.cli('run',name).returncode, 0)

    def test_completion_and_public_api_catalog(self):
        self.assertEqual(olnm._completion(['olnm','mise','ex']), ['exec'])
        self.assertEqual(olnm._completion(['miseglobal','no']), ['node@26'])
        self.assertEqual(olnm._completion(['miseexec','node@26','--','node','x']), [])
        self.assertIn('mise',olnm.public())

    def test_readme_python_invocation_as_written(self):
        commands = [line for block in re.findall(r'```sh\n(.*?)\n```', (ROOT/'README.md').read_text(), re.S)
                    for line in block.splitlines() if line.startswith('python3 -c ') and 'olnm.mise(' in line]
        self.assertTrue(commands)
        for command in commands:
            result = subprocess.run(['bash','-c',command],cwd=self.work,env=self.env,
                                    input='',capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['argv'],['exec','node@26','--','node','--version'])

    @unittest.skipUnless(fleet_check.node(), 'Node 26+ unavailable')
    def test_node_execfile_invokes_python_adapter(self):
        js = next(block for block in re.findall(r'```js\n(.*?)\n```', (ROOT/'README.md').read_text(), re.S)
                  if block.startswith("const { spawnSync }"))
        result = run_node('-e', js, node=fleet_check.node(), cwd=self.work,
                          env=self.env, stdin='node stdin', timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads(result.stdout)
        self.assertEqual(observed['argv'],['exec','node@26','--','node','--version'])
        self.assertEqual(observed['stdin'],'node stdin')


# ─── 📦 stdlib tools via uv's pinned tool cache ───

class StdlibCommands(unittest.TestCase):
    def setUp(self):
        global olnm
        olnm=importlib.import_module('olnm')
        self.tmp=tempfile.TemporaryDirectory(prefix='olnm stdlib ');self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name);self.bin=self.work/'bin';self.bin.mkdir()
        self.uv=self.bin/'uv';self.uv.write_text('#!'+sys.executable+'\nimport os,json,sys\nprint(json.dumps({"argv":sys.argv[1:],"stdin":sys.stdin.read(),"cwd":os.getcwd()}))\nprint("backend diagnostic",file=sys.stderr)\nsys.exit(int(os.environ.get("BACKEND_EXIT","0")))\n');self.uv.chmod(0o755)
        self.env=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],PYTHONPATH=str(ROOT),OLNM_PY=str(ROOT/'olnm.py'),DIAL_FILE=str(self.work/'dial'),OLNM_HOME=str(self.work/'state'))

    def cli(self,*args,env=None):
        return run_cli(ROOT/'olnm.py',*args,cwd=self.work,env=env or self.env,stdin='stdin text',timeout=15)

    def test_default_cli_commands_preserve_process_contract(self):
        for name,tool in olnm._STDLIB_TOOLS.items():
            r=self.cli(name,'--help','arg with spaces','$(touch INJECTED)',env=dict(self.env,BACKEND_EXIT='7'))
            self.assertEqual(r.returncode,7,r.stderr)
            d=r.json();self.assertEqual(d['stdin'],'stdin text');self.assertEqual(d['cwd'],str(self.work))
            self.assertEqual(d['argv'][:3],['tool','run','--from'])
            self.assertTrue(d['argv'][3].endswith('@'+tool['commit']))
            self.assertEqual(d['argv'][4:],[tool['command'],'--help','arg with spaces','$(touch INJECTED)'])
            self.assertIn('backend diagnostic',r.stderr);self.assertFalse((self.work/'INJECTED').exists())

    def test_default_python_api(self):
        for name,tool in olnm._STDLIB_TOOLS.items():
            r=run_command(Command((sys.executable,'-c',f'import olnm,sys;sys.exit(olnm.{name}("--help"))')),cwd=self.work,env=self.env,stdin='',timeout=15)
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(r.stdout)['argv'][-2:],[tool['command'],'--help'])

    def test_bash_zsh_and_run_entrypoints(self):
        for name in olnm._STDLIB_TOOLS:
            self.assertEqual(self.cli('run',name,'--help').returncode,0)
        for shell in ['bash']+(['zsh'] if shutil.which('zsh') else []):
            r=subprocess.run([shell,'-c','eval "$(python3 "$OLNM_PY" init '+shell+')"; xprobe --help'],cwd=self.work,env=self.env,input='',text=True,capture_output=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(r.stdout)['argv'][-2:],['xprobe','--help'])

    def test_ontautology_exchange_adds_its_rdf_extra(self):
        pin=f"git+https://github.com/myon-bioinformatics/OnTautoLogy.git@{olnm._STDLIB_TOOLS['ontautology']['commit']}"
        for args,source in ((['--help'],pin),(['exchange','--help'],'ontautology[exchange] @ '+pin)):
            r=self.cli('ontautology',*args)
            self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.json()['argv'][3:5],[source,'ontautology'])

    def test_onto_uses_distribution_extra_and_propagates_offline(self):
        self.uv.write_text(self.uv.read_text().replace(
            '"cwd":os.getcwd()', '"cwd":os.getcwd(),"offline":os.environ.get("UV_OFFLINE")'))
        self.env.pop("UV_OFFLINE", None)
        ref = "git+https://github.com/myon-bioinformatics/ONTO.git@89e23ddadb47f04bb10318a2232f2be554c6651f"
        cases = [
            ("3", ["exchange", "export", "neo4j", "space scope", "--format", "json"], "onto-shared[exchange] @ " + ref),
            ("0", ["otl", "exchange", "--help"], ref),
            ("1", ["otl", "check", "sample.json"], ref),
        ]
        for dial, args, source in cases:
            with self.subTest(dial=dial, args=args):
                (self.work / "dial").write_text(dial)
                result = self.cli("onto", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = result.json()
                offline = dial in {"0", "1"}
                expected = ["--offline"] if offline else []
                self.assertEqual(observed["argv"], [*expected, "tool", "run", "--from", source, "onto", *args])
                self.assertEqual(observed["offline"], "1" if offline else None)

    def test_dial_zero_uses_uv_offline_cache_mode(self):
        (self.work/'dial').write_text('0\n');r=self.cli('xprobe','--help')
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.json()['argv'][0],'--offline')

    def test_missing_uv_gives_next_step_and_no_registration(self):
        self.uv.unlink();env=dict(self.env,PATH=str(self.bin));r=self.cli('xprobe','--help',env=env)
        self.assertEqual(r.returncode,127);self.assertIn('docs.astral.sh/uv',r.stderr);self.assertEqual(r.stdout,'')
        self.assertFalse((self.work/'state').exists())

    def test_catalog_has_only_fixed_user_repository_sources(self):
        self.assertEqual(set(olnm._STDLIB_TOOLS),{'xprobe','cli_args','ghi','markdown','ascii_artist','ontautology','onto'})
        for name,t in olnm._STDLIB_TOOLS.items():
            self.assertRegex(t['commit'],r'^[a-f0-9]{40}$');self.assertIn(name,olnm.names());self.assertIn(name,olnm.public())


# ─── 🪞 bundled yourself ───

class TestBundledYourself(unittest.TestCase):
    def setUp(self):
        global olnm
        olnm = importlib.import_module('olnm')
        self.tmp = tempfile.TemporaryDirectory(prefix='olnm yourself ')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        # Only olnm.py is carried to a different directory. No source clone on sys.path.
        self.source = self.work / 'olnm.py'
        shutil.copyfile(ROOT / 'olnm.py', self.source)
        self.env = dict(os.environ, OLNM_PY=str(self.source),
                        OLNM_HOME=str(self.work/'state'), DIAL_FILE=str(self.work/'dial'),
                        GIT_CONFIG_GLOBAL=str(self.work/'gitconfig'))
        self.env.pop('PYTHONPATH',None)

    def cli(self,*args):
        return run_cli(self.source,*args,cwd=self.work,env=self.env,timeout=20)

    def test_snapshot_integrity_license_and_inert_import(self):
        self.assertEqual(hashlib.sha256(olnm._YOURSELF_SOURCE.encode()).hexdigest(),olnm._YOURSELF_SHA256)
        self.assertEqual(len(olnm._YOURSELF_COMMIT),40)
        self.assertIn('MIT License',olnm._YOURSELF_LICENSE)
        self.assertIn('Copyright (c) 2026 myon',olnm._YOURSELF_LICENSE)
        # Loading the pinned definitions performs no process execution or socket access.
        import socket
        with mock.patch.object(olnm.subprocess,'run',side_effect=AssertionError('unexpected subprocess')), \
             mock.patch.object(socket,'socket',side_effect=AssertionError('unexpected socket')):
            namespace=olnm._yourself_namespace()
        self.assertTrue(callable(namespace['main']))

    def test_cli_equals_pinned_upstream_on_minimal_report(self):
        result=self.cli('yourself','--minimal','--format','json')
        self.assertEqual(result.returncode,0,result.stderr)
        upstream=self.work/'reference.py';upstream.write_text(olnm._YOURSELF_SOURCE)
        expected=run_cli(upstream,'--minimal','--format','json',cwd=self.work,env=self.env,timeout=20)
        self.assertEqual(result.json(), expected.json())
        self.assertEqual(result.json()['schema_version'],1)
        self.assertFalse((self.work/'state').exists())

    def test_api_without_args_is_default_upstream_main_and_returns_exit_code(self):
        entry=mock.Mock(return_value=0)
        with mock.patch.object(olnm,'_yourself_namespace',return_value={'main':entry}):
            self.assertEqual(olnm.yourself(),0)
        entry.assert_called_once_with([])
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(olnm.yourself('--minimal','--format','json'),0)
        self.assertEqual(json.loads(out.getvalue())['schema_version'],1)

    def test_api_help_and_argument_error_return_codes(self):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            self.assertEqual(olnm.yourself('--help'),0)
            self.assertEqual(olnm.yourself('--format','invalid'),2)
        self.assertIn('--minimal',out.getvalue())
        self.assertIn('invalid',err.getvalue())
        self.assertEqual(self.cli('yourself','--format','invalid').returncode,2)
        self.assertEqual(self.cli('yourself','--runner-env-json').returncode,2)

    def test_shell_run_and_uv_file_entrypoints(self):
        for args in [('run','yourself','--minimal','--format','json'),
                     ('yourself','--','--minimal','--format','json')]:
            result=self.cli(*args)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.json()['schema_version'],1)
        for shell in ['bash']+(['zsh'] if shutil.which('zsh') else []):
            result=subprocess.run([shell,'-c','eval "$(python3 "$OLNM_PY" init '+shell+')"; yourself --minimal --format json'],
                                  cwd=self.work,env=self.env,text=True,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['schema_version'],1)
        if shutil.which('uv'):
            result=subprocess.run(['uv','run','--offline','--no-project',self.source,'yourself','--minimal','--format','json'],
                                  cwd=self.work,env=self.env,text=True,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['schema_version'],1)

    def test_default_completion_uses_upstream_declarations(self):
        for words in [['yourself','--fo'],['olnm','yourself','--fo'],['olnm','run','yourself','--fo']]:
            self.assertEqual(olnm._completion(words),['--format'])
        self.assertEqual(olnm._completion(['yourself','--format','']),['json','markdown','text'])
        self.assertIn('yourself',olnm.names())
        self.assertIn('yourself',olnm.public())

    def test_report_stdout_has_no_wrapper_messages_even_when_mise_is_missing(self):
        bindir=self.work/'bin';bindir.mkdir();(bindir/'python3').symlink_to(sys.executable)
        env=dict(self.env,PATH=str(bindir))
        result=run_cli(self.source,'yourself','--minimal','--format','json',cwd=self.work,env=env,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stderr,'')
        result.json()

    def test_snapshot_refresh_is_idempotent_and_rejects_uncommitted_source(self):
        upstream=self.work/'upstream';upstream.mkdir()
        (upstream/'yourself.py').write_text('def main(argv=None):\n    return 0\n')
        (upstream/'LICENSE').write_text('MIT License\n')
        def git(*args):
            return subprocess.run(['git',*args],cwd=upstream,env=self.env,text=True,capture_output=True,check=True)
        git('init','-q');git('add','yourself.py','LICENSE')
        git('-c','user.name=test','-c','user.email=test@example.invalid','commit','-qm','snapshot')
        target=self.work/'target.py';target.write_text('if __name__ == "__main__":\n    pass\n')
        result=run_cli(ROOT/'scripts/update_yourself.py',upstream,'--target',target,env=self.env,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        expected=target.read_bytes()
        self.assertEqual(run_cli(ROOT/'scripts/update_yourself.py',upstream,'--target',target,env=self.env).returncode,0)
        self.assertEqual(target.read_bytes(),expected)
        (upstream/'yourself.py').write_text('def main(argv=None):\n    return 1\n')
        result=run_cli(ROOT/'scripts/update_yourself.py',upstream,'--target',target,env=self.env,timeout=15)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('differs from the pinned',result.stderr)
        self.assertEqual(target.read_bytes(),expected)


if __name__ == "__main__":
    unittest.main()
