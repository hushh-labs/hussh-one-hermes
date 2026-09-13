# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0
"""Exercise updater control flow against local Git remotes and fake installers."""
from pathlib import Path
import os
import shutil
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def git(cwd, *args):
    return subprocess.check_output(['git', '-C', str(cwd), *args], text=True).strip()


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def fixture(tmp_path):
    seed = tmp_path / 'seed'
    seed.mkdir()
    git(seed, 'init', '-b', 'main')
    git(seed, 'config', 'user.email', 'test@example.invalid')
    git(seed, 'config', 'user.name', 'Test')
    (seed / 'scripts').mkdir()
    shutil.copy2(ROOT / 'scripts/hussh-one-upstream-update.sh', seed / 'scripts/hussh-one-upstream-update.sh')
    (seed / '.gitignore').write_text('.venv/\n')
    (seed / 'shared.txt').write_text('base\n')
    (seed / 'LICENSES').mkdir()
    (seed / 'LICENSES/attribution.toml').write_text('upstream_base_commit = "' + '0' * 40 + '"\n')
    executable(seed / 'scripts/hussh-one-guard.sh', '#!/bin/sh\necho guard >> "$TRACE"\nexit "${FAIL_GUARD:-0}"\n')
    executable(seed / 'scripts/hussh-one-supervisor.sh', '#!/bin/sh\necho restart >> "$TRACE"\n')
    git(seed, 'add', '.')
    git(seed, 'commit', '-qm', 'base')
    for name in ['origin', 'upstream']:
        subprocess.run(['git', 'clone', '--bare', str(seed), str(tmp_path / (name + '.git'))], check=True, capture_output=True)
    live = tmp_path / 'live'
    subprocess.run(['git', 'clone', str(tmp_path / 'origin.git'), str(live)], check=True, capture_output=True)
    for key, val in [('user.email', 'test@example.invalid'), ('user.name', 'Test')]:
        git(live, 'config', key, val)
    for name, url in [('origin', 'https://github.com/hushh-labs/hussh-one-hermes.git'), ('upstream', 'https://github.com/NousResearch/hermes-agent.git')]:
        if name == 'origin':
            git(live, 'remote', 'set-url', name, url)
        else:
            git(live, 'remote', 'add', name, url)
        git(live, 'config', 'url.' + str(tmp_path / (name + '.git')) + '.insteadOf', url)
    git(live, 'remote', 'set-url', '--push', 'upstream', 'DISABLED')
    (live / '.venv/bin').mkdir(parents=True)
    (live / '.venv/bin/python').symlink_to(sys.executable)
    bin_dir = tmp_path / 'bin'
    # Keep the production remote identity gate while routing every actual Git
    # transfer to the two throwaway bare repositories via insteadOf.
    real_git = shlex.quote(shutil.which('git'))
    executable(bin_dir / 'git', '''#!/bin/sh
case "$*" in
  'remote get-url origin') echo https://github.com/hushh-labs/hussh-one-hermes.git; exit 0;;
  'remote get-url upstream') echo https://github.com/NousResearch/hermes-agent.git; exit 0;;
esac
exec ''' + real_git + ''' "$@"
''')
    executable(bin_dir / 'uv', '#!/bin/sh\necho "deps $*" >> "$TRACE"\nexit "${FAIL_DEPS:-0}"\n')
    executable(bin_dir / 'corepack', '#!/bin/sh\necho "node $*" >> "$TRACE"\n')
    env = dict(os.environ, HOME=str(tmp_path), HERMES_HOME=str(tmp_path / 'profile'),
               PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], TRACE=str(tmp_path / 'trace'))
    env.pop('HUSSH_ONE_DRY_RUN', None)
    return seed, live, tmp_path, env


def advance(seed, root, remote, text):
    (seed / 'shared.txt').write_text(text)
    git(seed, 'add', 'shared.txt')
    git(seed, 'commit', '-qm', 'fixture revision')
    git(seed, 'push', str(root / (remote + '.git')), 'main')


def run(live, env, *args):
    return subprocess.run(['bash', 'scripts/hussh-one-upstream-update.sh', *args], cwd=live, env=env,
                          text=True, capture_output=True, timeout=30)


def test_detect_apply_validate_restart_then_noop(fixture):
    seed, live, root, env = fixture
    advance(seed, root, 'origin', 'fork update\n')
    check = run(live, env, '--check')
    assert check.returncode == 0, check.stderr
    assert 'origin/main: 1 commit(s) available' in check.stdout
    applied = run(live, env, '--apply', '--restart')
    assert applied.returncode == 0, applied.stderr
    trace = (root / 'trace').read_text().splitlines()
    assert trace[0].startswith('deps pip install --python ')
    assert trace[-2:] == ['guard', 'restart']
    assert git(live, 'rev-parse', 'HEAD') == git(seed, 'rev-parse', 'HEAD')
    assert git(live, 'branch', '--show-current') == 'main'
    assert not git(live, 'status', '--porcelain')
    again = run(live, env, '--apply', '--restart')
    assert again.returncode == 0
    assert (root / 'trace').read_text().splitlines() == trace


@pytest.mark.parametrize('failure', ['FAIL_DEPS', 'FAIL_GUARD'])
def test_failed_validation_never_restarts_and_retries_next_run(fixture, failure):
    seed, live, root, env = fixture
    advance(seed, root, 'origin', 'fork update\n')
    result = run(live, dict(env, **{failure: '1'}), '--apply', '--restart')
    assert result.returncode != 0
    assert 'restart' not in (root / 'trace').read_text().splitlines()
    assert (root / 'profile/cache/hussh-one-update-pending').exists()
    retry = run(live, env, '--apply', '--restart')
    assert retry.returncode == 0, retry.stderr
    assert (root / 'trace').read_text().splitlines()[-2:] == ['guard', 'restart']
    assert not (root / 'profile/cache/hussh-one-update-pending').exists()


def test_native_conflict_is_detected_without_switching_live_main(fixture):
    seed, live, root, env = fixture
    base = git(seed, 'rev-parse', 'HEAD')
    advance(seed, root, 'origin', 'fork side\n')
    git(seed, 'reset', '--hard', base)
    advance(seed, root, 'upstream', 'native side\n')
    result = run(live, env, '--apply')
    assert result.returncode == 0, result.stderr
    assert 'Deferred:' in result.stdout
    assert git(live, 'branch', '--show-current') == 'main'
    assert not git(live, 'status', '--porcelain')
    assert git(live, 'branch', '--list', 'sync/*') == ''
    assert git(live, 'tag', '--list', 'safety/*') == ''
    assert (live / 'shared.txt').read_text() == 'fork side\n'


def test_apply_dry_run_cannot_advance_or_install(fixture):
    seed, live, root, env = fixture
    before = git(live, 'rev-parse', 'HEAD')
    advance(seed, root, 'origin', 'fork update\n')
    result = run(live, env, '--apply', '--restart', '--dry-run')
    assert result.returncode == 0, result.stderr
    assert git(live, 'rev-parse', 'HEAD') == before
    assert not (root / 'trace').exists()


@pytest.mark.parametrize('fail', [False, True])
def test_clean_native_merge_is_guarded_and_always_returns_to_main(fixture, fail):
    seed, live, root, env = fixture
    advance(seed, root, 'upstream', 'native revision\n')
    before = git(live, 'rev-parse', 'main')
    result = run(live, dict(env, FAIL_DEPS='1' if fail else '0'), '--apply')
    assert git(live, 'branch', '--show-current') == 'main'
    assert not git(live, 'status', '--porcelain')
    if fail:
        assert result.returncode != 0
        assert git(live, 'rev-parse', 'main') == before
    else:
        assert result.returncode == 0, result.stderr
        assert git(live, 'rev-parse', 'main') == git(root / 'origin.git', 'rev-parse', 'main')
        assert (live / 'shared.txt').read_text() == 'native revision\n'
        assert 'guard' in (root / 'trace').read_text().splitlines()
