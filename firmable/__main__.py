import argparse
import sys


def main():
    # Packets contain non-ASCII characters; a Windows console or a redirected stream may not be UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # One subcommand per pipeline stage. In running order:
    #   triage -> onboard -> review -> extract -> link -> profiles
    # The *-report, link-diff and audit commands measure a stage; they do not feed the next one.
    p = argparse.ArgumentParser(prog="python -m firmable")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("triage", help="Part 1: build pool and shortlist")
    t.add_argument("--no-cache", action="store_true")
    sub.add_parser("triage-report", help="Part 1: false-positive rate from the hand-check")

    o = sub.add_parser("onboard", help="Part 2: run the agent on a dataset (CKAN id or name)")
    o.add_argument("datasets", nargs="+")
    o.add_argument("--restart-from", help="re-run from this step onwards",
                   choices=["select_resource", "download", "inspect", "load", "profile", "propose",
                            "validate_revise", "finalize"])
    o.add_argument("--resource", help="pin a resource id instead of letting the agent choose")

    r = sub.add_parser("review", help="Part 2: human approval gate for an agent-written config")
    r.add_argument("slug")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    g.add_argument("--feedback")
    r.add_argument("--reviewer")
    r.add_argument("--note")

    e = sub.add_parser("extract", help="Part 2: run approved configs through the engine")
    e.add_argument("slugs", nargs="*")
    e.add_argument("--limit", type=int, default=1000)
    e.add_argument("--allow-unapproved", action="store_true")

    lk = sub.add_parser("link", help="Part 3: link businesses across the onboarded sources")
    lk.add_argument("--threshold", type=float)
    lk.add_argument("--allow-unapproved", action="store_true")
    lr = sub.add_parser("link-report", help="Part 3: precision from the filled-in link hand-check")
    lr.add_argument("--reviewer", help="who filled in the sheets; recorded with each verdict")
    ld = sub.add_parser("link-diff", help="Part 3: compare a previous matcher's links with the current ones")
    ld.add_argument("old", help="an earlier links file, e.g. outputs/links/history/links.rules-0.1.0.jsonl")
    ld.add_argument("new", nargs="?", help="defaults to outputs/links/links.jsonl")

    pr = sub.add_parser("profiles", help="Part 4: 50 merged company profiles + source-deletion test")
    pr.add_argument("-n", type=int, default=50)
    pr.add_argument("--allow-unapproved", action="store_true")
    pr.add_argument("--as-of", help="date a claim's valid_to is judged against (YYYY-MM-DD); default today")

    au = sub.add_parser("audit", help="Check every config over its whole file, with no model calls")
    au.add_argument("slugs", nargs="*")

    args = p.parse_args()
    # Each module is imported only when its command runs. That keeps startup fast, and it means
    # triage, link and profiles work without the anthropic package or an API key being set up.
    if args.cmd == "triage":
        from . import triage
        triage.run(use_cache=not args.no_cache)
    elif args.cmd == "triage-report":
        from . import triage
        triage.report()
    elif args.cmd == "onboard":
        from . import agent
        results = [agent.onboard(d, restart_from=args.restart_from, resource=args.resource) for d in args.datasets]
        # onboard() returns None for a run that failed, so a script calling this sees a nonzero exit code.
        if not all(results):
            sys.exit(1)
    elif args.cmd == "review":
        from . import review
        review.review(args.slug, approve=args.approve, reject=args.reject, feedback=args.feedback,
                      reviewer=args.reviewer, note=args.note)
    elif args.cmd == "extract":
        from . import review
        review.extract(args.slugs or None, limit=args.limit, allow_unapproved=args.allow_unapproved)
    elif args.cmd == "link":
        from . import link
        link.run(allow_unapproved=args.allow_unapproved, threshold=args.threshold or link.THRESHOLD)
    elif args.cmd == "link-report":
        from . import link
        link.report(reviewer=args.reviewer)
    elif args.cmd == "link-diff":
        from . import link
        link.diff_links(args.old, args.new)
    elif args.cmd == "profiles":
        from . import profile
        profile.run(n=args.n, allow_unapproved=args.allow_unapproved, as_of=args.as_of)
    elif args.cmd == "audit":
        from . import audit
        audit.run(args.slugs or None)


if __name__ == "__main__":
    main()
