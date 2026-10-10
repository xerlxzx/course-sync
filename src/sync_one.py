#!/usr/bin/env python3
"""
Extract ONE Moodle module by cmid, using course_sync's own handlers.

Usage (from the repo root): python3 src/sync_one.py <cmid> [--dry-run] [--config PATH]

The module goes through course_sync.dispatch_module, the same router a full
sync uses, so a book becomes its Markdown file, a page its Markdown plus
images, a resource/folder its files, and a label or url its linked files.

Deliberately does NOT call write_links_file / write_section_content /
write_section_index, so _index.md, _content.md and _links.md are left alone.
Only the single module's own files are written. Module types whose only output
lives in those section-level files (quiz/assign descriptions, label text,
external links, unsupported types) therefore write nothing here; sync_one says
so and exits 3 instead of reporting a silent success.

Exit status:
  0  the module was handled: files fetched, already current, or previewed
  1  error: cmid not found in any configured course, a fetch or write failed,
     or the run crashed (the message is redacted; COURSE_SYNC_DEBUG=1 adds more)
  2  bad command line
  3  the module was found but nothing can be written for it (see above)
"""
import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

# course_sync.py sits beside this file in src/.
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

import course_sync as cs  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_NOTHING_TO_WRITE = 0, 1, 3

# Why a module type that produced no file output has nothing for sync_one to write.
SECTION_ONLY_REASON = {
    "quiz": "its description is captured in the section's _content.md",
    "assign": "its description is captured in the section's _content.md",
    "label": "its text goes to _content.md and its links to _links.md",
    "url": "an external link is recorded in the section's _links.md",
}


def prime_name_claims(base_url, token, sections, course_dir, output_dir, cmid,
                      misc_patterns, skip_patterns):
    """
    Rebuild the filename claims a full sync would hold when it reaches ``cmid``.

    Names that clash within a folder are numbered in module order (claim_name),
    so a lone module must replay the modules before it to get the same name.
    This is a silent dry run: no network, no writes, throwaway state.
    """
    claims = {}
    for section_index, section in enumerate(sections):
        sec_name = cs.clean_section_name(section.get("name", ""))
        section_dir = cs.section_dir_for(course_dir, sec_name, section_index)
        for mod in section.get("modules", []):
            if mod.get("id") == cmid:
                return claims
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    cs.dispatch_module(
                        base_url, token, mod, section_dir, True, set(), output_dir,
                        {"files": {}}, cs.new_section_state(claims),
                        misc_patterns, skip_patterns,
                    )
            except Exception:
                pass  # the real run reports problems; here only the claims matter
    return claims


def report(mod, state, files):
    """Print the outcome and return the exit status."""
    modname = mod.get("modname", "")
    if state["skipped"]:
        print("\nSkipped:")
        for s in state["skipped"]:
            print(f"  - {s}")
    print(f"\nFiles written/updated: {files}")
    print("Index/content/links files: untouched.")

    if state["failures"]:
        print(f"\n{len(state['failures'])} failure(s): see the [fail] lines above.")
        return EXIT_ERROR

    if not state["rows"]:
        why = SECTION_ONLY_REASON.get(modname)
        if why:
            print(f"\nNothing was written: this {modname} module has no file of its own; {why}, "
                  "which sync_one never rewrites. Run the full sync to refresh it.")
        else:
            print(f"\nNothing was written: course_sync has no file output for this module "
                  f"(type '{modname}'). See the Skipped list above for the reason.")
        return EXIT_NOTHING_TO_WRITE

    if state["content_blocks"] or state["external_links"]:
        print("\nNot written (section-level files untouched): "
              f"{len(state['content_blocks'])} text block(s), "
              f"{len(state['external_links'])} external link(s). "
              "Run the full sync to refresh them.")
    return EXIT_OK


def run(args):
    config_path = Path(args.config).expanduser().resolve() if args.config else cs.DEFAULT_CONFIG_PATH
    cfg = cs.load_config(config_path)
    base_url = cfg["moodle"]["base_url"].rstrip("/")
    token = cs.load_token(None, cfg, config_path)
    output_dir = cs.resolve_path(cfg["output_dir"], config_path.parent)
    wanted = cs.parse_courses(cfg)
    misc_patterns = cs.parse_pattern_list(cfg, "misc_patterns")
    skip_patterns = cs.parse_pattern_list(cfg, "skip_patterns")
    cmid = args.cmid

    user_id, fullname, _site_info = cs.get_user_id(base_url, token)
    print(f"Logged in as: {fullname}")
    all_courses = cs.get_courses(base_url, token, user_id)

    for want in wanted:
        match = None
        for c in all_courses:
            if (c.get("shortname", "") or "").startswith(want["code"]):
                match = c
                break
        if not match:
            continue

        course_dir = output_dir / want["folder"]
        sections = cs.get_contents(base_url, token, match["id"])
        for section_index, section in enumerate(sections):
            for mod in section.get("modules", []):
                if mod.get("id") != cmid:
                    continue

                sec_name = cs.clean_section_name(section.get("name", ""))
                section_dir = cs.section_dir_for(course_dir, sec_name, section_index)

                print(f"Course:  {match['shortname']}")
                print(f"Section: {sec_name}")
                print(f"Module:  {mod.get('name')}  (modname={mod.get('modname')})")
                print(f"Target:  {section_dir}")
                print()

                claims = prime_name_claims(
                    base_url, token, sections, course_dir, output_dir, cmid,
                    misc_patterns, skip_patterns,
                )
                state = cs.new_section_state(claims)
                hash_index = cs.load_hash_index(output_dir, move_aside=not args.dry_run)
                index_before = json.dumps(hash_index, sort_keys=True)
                files = 0
                try:
                    files = cs.dispatch_module(
                        base_url, token, mod, section_dir, args.dry_run, set(),
                        output_dir, hash_index, state, misc_patterns, skip_patterns,
                    )
                except Exception as e:
                    cs.record_failure(state, f"{mod.get('name')} ({mod.get('modname')})",
                                      cs.describe_error(e))
                if not args.dry_run and json.dumps(hash_index, sort_keys=True) != index_before:
                    cs.save_hash_index(output_dir, hash_index)
                return report(mod, state, files)

    print(f"cmid {cmid} not found in any configured course.")
    return EXIT_ERROR


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Re-fetch one Moodle module by its cmid (section index files are not rewritten).",
        epilog="Exit status: 0 handled, 1 error or cmid not found, 2 bad usage, "
               "3 module has no file output so nothing was written.")
    parser.add_argument("cmid", type=int, help="course module id (cmid) to fetch")
    parser.add_argument("--dry-run", action="store_true", help="Preview without downloading")
    parser.add_argument("--config", default=None,
                        help="Path to config YAML (default: config.yaml in the repo root)")
    args = parser.parse_args(argv)
    return cs.run_guarded(run, args)


if __name__ == "__main__":
    sys.exit(main())
