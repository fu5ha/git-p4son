"""Colocated jj workspace selection and operation boundaries."""

import argparse
import contextlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .common import CommandError, run
from .git import get_head_commit, get_untracked_files
from .jj_lock import working_copy_lock
from .log import log


_LOCK_ENV = '_GIT_P4SON_JJ_LOCK'
_REVISION_TEMPLATE = ('json(self) ++ "\\t" ++ empty ++ "\\t" ++ conflict'
                      ' ++ "\\t" ++ divergent ++ "\\n"')


def is_workspace(workspace_dir: str) -> bool:
    """Whether a Git workspace also contains jj metadata."""
    return (Path(workspace_dir) / '.jj').exists()


def _run(workspace_dir: str, *command: str, operation: str = '@'):
    return run(['jj', '--no-pager', '--color', 'never',
                '--ignore-working-copy', '--at-op', operation, *command],
               cwd=workspace_dir)


def _operation_id(workspace_dir: str) -> str:
    return _run(workspace_dir, 'op', 'log', '-n', '1',
                '--no-graph', '-T', 'id').stdout[0]


def _validate_workspace(workspace_dir: str) -> None:
    root = Path(workspace_dir).resolve()
    repo = root / '.jj' / 'repo'
    if not repo.is_dir():
        raise CommandError('Only colocated jj workspaces are supported.')
    target = repo / 'store' / 'git_target'
    if not target.is_file():
        raise CommandError(
            'jj workspace is not backed by the colocated Git repository.')
    git_dir = run(['git', 'rev-parse', '--absolute-git-dir'],
                  cwd=workspace_dir).stdout[0]
    if (target.parent / target.read_text()).resolve() != Path(git_dir).resolve():
        raise CommandError(
            'jj and Git must share the same colocated repository.')
    if _run(workspace_dir, 'sparse', 'list').stdout != ['.']:
        raise CommandError('Sparse jj workspaces are not supported.')
    for state in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD'):
        if (Path(git_dir) / state).exists():
            raise CommandError(
                'Finish the Git operation first, or use --ignore-jj.')


def _validate_version(workspace_dir: str) -> None:
    version = run(['jj', '--version'], cwd=workspace_dir).stdout[0]
    match = re.search(r'jj (\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (0, 41):
        raise CommandError(
            'jj support requires jj 0.41 or newer for compatible working-copy locking.')
    kind = Path(workspace_dir) / '.jj' / 'working_copy' / 'type'
    if not kind.is_file() or kind.read_text() != 'local':
        raise CommandError('Only jj local working copies are supported.')


@dataclass
class Revision:
    commit: str
    parents: list[str]
    description: str
    empty: bool
    conflict: bool
    divergent: bool

    @classmethod
    def parse(cls, line: str):
        data, empty, conflict, divergent = line.split('\t')
        data = json.loads(data)
        return cls(data['commit_id'], data['parents'], data['description'],
                   empty == 'true', conflict == 'true', divergent == 'true')


def _resolve(directory: str, operation: str, revision: str) -> Revision:
    lines = _run(directory, 'log', '-r', revision, '--no-graph',
                 '-T', _REVISION_TEMPLATE, operation=operation).stdout
    if len(lines) != 1:
        raise CommandError(
            f'Expected one jj revision for "{revision}", found {len(lines)}.')
    resolved = Revision.parse(lines[0])
    if resolved.conflict:
        raise CommandError(
            f'jj revision "{revision}" has conflicts; resolve them first.')
    if resolved.divergent:
        raise CommandError(
            f'jj revision "{revision}" is divergent; resolve it first.')
    if not resolved.commit.strip('0'):
        raise CommandError(
            'The jj root revision has no Git commit; select a different base.')
    return resolved


@dataclass
class Workspace:
    directory: str
    operation: str
    working: Revision
    tip: Revision
    bookmarks: dict[str, list[str | None]]
    eol_conversion: str
    sync_bookmark: str | None = None

    @classmethod
    def load(cls, directory: str, operation: str):
        if operation == '@':
            operation = _operation_id(directory)
        working = _resolve(directory, operation, '@')
        bookmarks = {}
        result = _run(directory, 'bookmark', 'list', '-T', 'json(self) ++ "\\n"',
                      operation=operation)
        for line in result.stdout:
            bookmark = json.loads(line)
            if 'remote' not in bookmark:
                bookmarks[bookmark['name']] = bookmark['target']
        eol = _run(directory, 'config', 'get', 'working-copy.eol-conversion',
                   operation=operation).stdout[0]
        if eol not in ('none', 'input', 'input-output'):
            raise CommandError(f'Unsupported jj EOL conversion: {eol}')
        workspace = cls(directory, operation, working, working, bookmarks, eol)
        if (working.empty and not working.description
                and len(working.parents) == 1
                and not workspace.bookmarks_at(working.commit)):
            workspace.tip = workspace.resolve(working.parents[0])
        return workspace

    def run(self, *command: str):
        return _run(self.directory, *command, operation=self.operation)

    def resolve(self, revision: str) -> Revision:
        """Resolve exactly one conflict-free jj revision."""
        return _resolve(self.directory, self.operation, revision)

    def bookmarks_at(self, commit: str) -> list[str]:
        return [name for name, targets in self.bookmarks.items()
                if commit in targets]

    def current_bookmark(self) -> str:
        """Require one unconflicted local bookmark at the selected tip."""
        names = self.bookmarks_at(self.tip.commit)
        if len(names) != 1 or len(self.bookmarks[names[0]]) != 1:
            raise CommandError('No unambiguous jj bookmark at the selected change. '
                               'Name the alias explicitly, or set a bookmark there.')
        return names[0]

    def resolve_base(self, base: str) -> str:
        """Resolve the base and reject conflicted changes in the selected range."""
        if base == 'HEAD~1':
            if len(self.tip.parents) != 1:
                raise CommandError(
                    'The selected jj change needs an explicit base revision.')
            base = self.tip.parents[0]
        resolved = self.resolve(base)
        conflicts = self.run('log', '-r',
                             f'({resolved.commit}..{self.tip.commit}) & (conflicts() | divergent())',
                             '--no-graph', '-T', 'commit_id ++ "\\n"').stdout
        if conflicts:
            raise CommandError('The selected jj changes contain conflicts or divergence; '
                               'resolve them first.')
        return resolved.commit

    def check_untracked_files(self) -> bool:
        """Reject non-ignored files jj left out of the initial snapshot."""
        log.heading('Checking jj workspace files')
        files = get_untracked_files(self.working.commit, self.directory)
        if files:
            for path in files:
                log.file_change(path, 'untracked')
            log.error('Workspace has files jj did not track')
            return False
        log.success('snapshotted')
        return True

    def prepare_sync(self, bookmark: str | None) -> None:
        """Require an empty working change above the sync bookmark."""
        working = self.working
        if (not working.empty or working.description or len(working.parents) != 1
                or self.bookmarks_at(working.commit)):
            raise CommandError('jj sync requires an empty, undescribed, bookmark-free '
                               'working change with one parent. Use jj new <bookmark> first.')
        bookmark = bookmark or self.current_bookmark()
        if self.bookmarks.get(bookmark) != [self.tip.commit]:
            raise CommandError(
                f'jj bookmark "{bookmark}" must point to the working change parent.')
        if get_head_commit(self.directory) != self.tip.commit:
            raise CommandError('Git HEAD does not match the jj sync parent.')
        self.sync_bookmark = bookmark

    def detach_for_sync(self) -> None:
        """Keep Git commits from advancing any bookmark other than the selected one."""
        run(['git', 'update-ref', '--no-deref', 'HEAD',
             self.tip.commit, self.tip.commit], cwd=self.directory)

    def advance_sync(self) -> None:
        """Advance the sync bookmark after a completed Git commit."""
        head = get_head_commit(self.directory)
        ref = f'refs/heads/{self.sync_bookmark}'
        run(['git', 'update-ref', ref, head, self.tip.commit],
            cwd=self.directory)
        self.tip.commit = head


def revision_options(args: argparse.Namespace) -> dict[str, str]:
    """Explicit Git revision options for a jj-aware command."""
    workspace = getattr(args, 'jj_workspace', None)
    return {'revision': workspace.tip.commit} if isinstance(workspace, Workspace) else {}


@contextlib.contextmanager
def operation(workspace_dir: str, *, ignore: bool = False, snapshot: bool = True):
    """Select jj state and exclude snapshots until the command finishes."""
    if not is_workspace(workspace_dir):
        yield None
        return
    root = str(Path(workspace_dir).resolve())
    inherited = os.environ.get(_LOCK_ENV)
    if inherited == root:
        if not ignore:
            raise CommandError(
                'Nested commands under the jj lock must use --ignore-jj.')
        yield None
        return
    _validate_version(workspace_dir)
    if not ignore:
        _validate_workspace(workspace_dir)
    # Check before snapshotting, so a busy workspace fails rather than waits.
    with working_copy_lock(workspace_dir):
        pass
    if snapshot and not ignore:
        snapshot_commit = run(['jj', '--no-pager', '--color', 'never', 'log', '-r', '@',
                               '--no-graph', '-T', 'commit_id'], cwd=workspace_dir).stdout[0]
    before = None if ignore else _operation_id(workspace_dir)
    with working_copy_lock(workspace_dir):
        if not ignore and before != _operation_id(workspace_dir):
            raise CommandError(
                'jj changed while acquiring the lock; retry the command.')
        workspace = None if ignore else Workspace.load(workspace_dir, before)
        if snapshot and not ignore and workspace.working.commit != snapshot_commit:
            raise CommandError(
                'jj working change changed while acquiring the lock; retry the command.')
        os.environ[_LOCK_ENV] = root
        try:
            yield workspace
        finally:
            if inherited is None:
                os.environ.pop(_LOCK_ENV, None)
            else:
                os.environ[_LOCK_ENV] = inherited
    if snapshot and not ignore:
        log.heading('Reconciling jj workspace')
        run(['jj', '--no-pager', '--color', 'never', 'status'], cwd=workspace_dir)
