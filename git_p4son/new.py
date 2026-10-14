"""New command: creates a Perforce changelist from the git change."""

import argparse
from .changelist_store import check_alias_usable, save_changelist_alias
from .lib import (
    check_git_workspace_clean,
    create_changelist,
    open_changes_and_revert_unchanged,
)
from .perforce import p4_shelve_changelist
from .log import log
from .jj import Workspace, revision_options


def new_command(args: argparse.Namespace) -> int:
    """Execute the new command."""
    workspace_dir = args.workspace_dir
    options = revision_options(args)

    if not args.no_edit:
        workspace = getattr(args, 'jj_workspace', None)
        clean = (workspace.check_untracked_files() if isinstance(workspace, Workspace)
                 else check_git_workspace_clean(workspace_dir))
        if not clean:
            return 1

    if args.alias and not check_alias_usable(args.alias, workspace_dir,
                                             args.force):
        return 1

    log.heading('Creating changelist')
    changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run,
        commit_list=not args.no_commit_list, review=args.review, **options)

    if not args.dry_run:
        if args.alias:
            if not save_changelist_alias(args.alias, changelist,
                                         workspace_dir, force=args.force):
                return 1
            log.success(f'Created CL {changelist} (alias={args.alias})')
        else:
            log.success(f'Created CL {changelist}')

    if not args.no_edit:
        open_changes_and_revert_unchanged(changelist, args.base_branch,
                                          workspace_dir, args.dry_run, **options)

    if args.shelve or args.review:
        log.heading('Shelving')
        p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    return 0
