# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import re
import subprocess

from mozphab.conduit import conduit
from mozphab.config import config
from mozphab.exceptions import Error, NonLinearException, NotFoundError
from mozphab.helpers import prepare_body, prompt, short_node
from mozphab.logger import logger
from mozphab.mercurial import Mercurial
from mozphab.patch import apply_patch
from mozphab.spinner import wait_message


def get_base_ref(diff):
    for ref in diff["fields"].get("refs", []):
        if ref["type"] == "base":
            return ref["identifier"]


def patch(repo, args):
    """Patch repository from Phabricator's revisions.

    By default:
    * perform sanity checks
    * find the base commit
    * create a new branch/bookmark
    * apply the patches and commit the changes

    args.no_commit is True - no commit will be created after applying diffs
    args.apply_to - <head|tip|branch> (default: branch)
        branch - find base commit and apply on top of it
        head/tip - apply changes to current commit
    args.raw is True - only print out the diffs (--force doesn't change anything)

    Raises:
    * Error if uncommitted changes are present in the working tree
    * Error if Phabricator revision is not found
    * Error if `--apply-to base` and no base commit found in the first diff
    * Error if base commit not found in repository
    """
    # Check if raw Conduit API can be used
    with wait_message("Checking connection to Phabricator."):
        # Check if raw Conduit API can be used
        if not conduit.check():
            raise Error("Failed to use Conduit API")

    if not args.raw:
        # Check if local and remote VCS matches
        with wait_message("Checking VCS"):
            repo.check_vcs()

        # Look for any uncommitted changes
        with wait_message("Checking repository.."):
            clean = repo.is_worktree_clean()

        if not clean:
            raise Error(
                "Uncommitted changes present. Please %s them or commit before patching."
                % ("shelve" if isinstance(repo, Mercurial) else "stash")
            )

    # --no-dependencies is an alias for --no-parents + --no-children
    if args.no_dependencies:
        args.no_parents = True
        args.no_children = True

    # Get the target revision
    with wait_message("Fetching D%s.." % args.revision_id):
        revisions = conduit.get_revisions(ids=[args.revision_id])
    if not revisions:
        raise Error("Revision not found")
    revision = revisions[0]

    parents = []
    if not args.no_parents:
        with wait_message("Fetching D%s's parents.." % args.revision_id):
            try:
                parents = conduit.get_ancestor_phids(revision["phid"])
            except NonLinearException:
                raise Error(
                    "Revision D%s has non-linear dependency relationships.\n"
                    "Unable to patch the stack." % args.revision_id
                )

    children = []
    if not args.no_children:
        with wait_message("Fetching D%s's children.." % args.revision_id):
            try:
                children = conduit.get_successor_phids(
                    revision["phid"], include_abandoned=args.include_abandoned
                )
            except NonLinearException:
                logger.warning(
                    "Revision D%s has non-linear dependency relationships.\n"
                    "Unable to apply child revisions.",
                    args.revision_id,
                )

    if not args.yes:
        if children and not config.always_full_stack:
            children_msg = "a child commit" if len(children) == 1 else "child commits"
            res = prompt(
                "Revision D%s has %s.  Would you like to patch all "
                "children?" % (args.revision_id, children_msg),
                ["Yes", "No", "Always"],
            )
            if res == "Always":
                config.always_full_stack = True
                config.write()
            elif res == "No":
                children = []

    # Fetch revision data
    if parents:
        with wait_message("Fetching related revisions.."):
            revisions.extend(conduit.get_revisions(phids=parents))
        revisions.reverse()

    if children:
        with wait_message("Fetching related revisions.."):
            revisions.extend(conduit.get_revisions(phids=children))

    if not args.raw:
        logger.info(
            "Patching revision%s: %s",
            "s" if len(revisions) > 1 else "",
            " ".join(["D%s" % r["id"] for r in revisions]),
        )

    # Pull diffs
    with wait_message("Downloading patch information.."):
        diffs = conduit.get_diffs([r["fields"]["diffPHID"] for r in revisions])

    if not args.no_commit and not args.raw:
        for rev in revisions:
            diff = diffs[rev["fields"]["diffPHID"]]
            if not diff["attachments"]["commits"]["commits"]:
                raise Error(
                    "A diff without commit information detected in revision D%s.\n"
                    "Use `--no-commit` to patch the working tree." % rev["id"]
                )

    # Set the target id
    target_revision_id = revisions[-1]["id"]

    base_node = None
    if not args.raw:
        args.apply_to = args.apply_to or config.apply_patch_to

        if args.apply_to == "base":
            base_node = get_base_ref(diffs[revisions[0]["fields"]["diffPHID"]])

            if not base_node:
                raise Error(
                    "Base commit not found in diff. "
                    "Use `--apply-to here` to patch current commit."
                )
        elif args.apply_to != "here":
            base_node = args.apply_to

        if args.apply_to != "here":
            try:
                with wait_message("Checking %s.." % short_node(base_node)):
                    base_node = repo.check_node(base_node)
            except NotFoundError as e:
                msg = "Unknown revision: %s" % short_node(base_node)
                if str(e):
                    msg += "\n%s" % str(e)

                if args.apply_to == "base":
                    msg += "\nUse --apply-to to set the base commit."

                raise Error(msg)

        branch_name = None if args.no_commit else "phab-D%s" % target_revision_id
        repo.before_patch(base_node, branch_name)

    parent = None
    for rev in revisions:
        # Prepare the body using just the data from Phabricator
        body = prepare_body(
            rev["fields"]["title"],
            rev["fields"]["summary"],
            rev["id"],
            repo.phab_url,
            depends_on=parent,
        )
        parent = rev["id"]
        diff = diffs[rev["fields"]["diffPHID"]]
        with wait_message("Downloading D%s.." % rev["id"]):
            raw = conduit.call("differential.getrawdiff", {"diffID": diff["id"]})

        if args.no_commit:
            with wait_message("Applying D%s.." % rev["id"]):
                apply_patch(raw, repo.path)

        elif args.raw:
            logger.info(raw)

        else:
            diff_commits = diff["attachments"]["commits"]["commits"]
            author = "%s <%s>" % (
                diff_commits[0]["author"]["name"],
                diff_commits[0]["author"]["email"],
            )

            try:
                with wait_message("Applying D%s.." % rev["id"]):
                    repo.apply_patch(raw, body, author, diff["fields"]["dateCreated"])
            except subprocess.CalledProcessError:
                raise Error("Patch failed to apply")

        if not args.raw and rev["id"] != revisions[-1]["id"]:
            logger.info("D%s applied", rev["id"])

    if not args.raw:
        logger.warning("D%s applied", target_revision_id)


def check_revision_id(value):
    # D123 or 123
    m = re.search(r"^D?(\d+)$", value)
    if m:
        return int(m.group(1))

    # Full URL
    m = re.search(r"^https?://[^/]+/D(\d+)", value)
    if m:
        return int(m.group(1))

    # Invalid
    raise argparse.ArgumentTypeError(
        "Invalid Revision ID (expected number or URL): %s\n" % value
    )


def add_parser(parser):
    patch_parser = parser.add_parser("patch", help="Patch from Phabricator revision")
    patch_parser.add_argument(
        "revision_id",
        type=check_revision_id,
        help="Revision number",
    )
    patch_group = patch_parser.add_mutually_exclusive_group()
    patch_group.add_argument(
        "--apply-to",
        "--applyto",
        "-a",
        metavar="TARGET",
        dest="apply_to",
        help="Where to apply the patch? <{NODE}|here|base> (default: %s)"
        % config.apply_patch_to,
    )
    patch_group.add_argument(
        "--raw", action="store_true", help="Prints out the raw diff to the STDOUT"
    )
    patch_parser.add_argument(
        "--no-commit",
        "--nocommit",
        action="store_true",
        dest="no_commit",
        help="Do not commit. Applies the changes with the `patch` command",
    )
    patch_parser.add_argument(
        "--no-bookmark",
        "--nobookmark",
        action="store_true",
        dest="no_bookmark",
        help="(Mercurial only) Do not create the bookmark",
    )
    patch_parser.add_argument(
        "--no-branch",
        "--nobranch",
        action="store_true",
        dest="no_branch",
        help="(Git only) Do not create the branch",
    )
    patch_parser.add_argument(
        "--no-parents",
        action="store_true",
        help="Do not apply parents/ancestors of this patch",
    )
    patch_parser.add_argument(
        "--no-children",
        action="store_true",
        help="Do not apply children/descendants of this patch",
    )
    patch_parser.add_argument(
        "--no-dependencies",
        "--skip-dependencies",
        action="store_true",
        help="Do not apply dependencies of this patch "
        "(same as --no-parents and --no-children)",
    )
    patch_parser.add_argument(
        "--include-abandoned",
        action="store_true",
        help="Apply abandoned revisions",
    )
    patch_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Patch without confirmation (default: False)",
    )
    patch_parser.add_argument(
        "--safe-mode",
        dest="safe_mode",
        action="store_true",
        help="Run VCS with only necessary extensions",
    )
    patch_parser.add_argument(
        "--force-vcs",
        action="store_true",
        help="EXPERIMENTAL: Override VCS compatibility check",
    )
    patch_parser.set_defaults(func=patch, needs_repo=True, no_arc=True)
