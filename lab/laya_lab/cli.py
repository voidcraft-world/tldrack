"""`laya-lab` — export labels, fine-tune, and manage the checkpoints the laya bridge plugin serves.

    laya-lab status                    # AGENT RACK › LAYA's state, in the terminal (no torch)
    laya-lab export-coordinator        # merge the latest Claude-ranked audit into the dataset
    laya-lab train coordinator         # reads <data>/laya/data/coordinator.jsonl (see README)
    laya-lab models                    # what is registered, and how it scored
    laya-lab golden agent-open-work    # score the served model on its golden cases
    laya-lab settle agent-open-work --apply   # signed-off agent-written cases lose `provisional` (golden.py settle)
    laya-lab export-open-work          # turn ends from this machine's agent transcripts, and their requests (open_work_data.py)
    laya-lab question-challenger agent_open_work --version 3   # the served rows refitted at another question version (question.py)
    laya-lab label-open-work           # Claude labels them on the subscription, then builds the rows
    laya-lab review-open-work          # the served model's p on every reviewable text (score.py)
    laya-lab sweep agent-open-work     # where the ⚠️ cut should sit, on held-out labels (heldout.py)
    laya-lab promote agent-open-work --flips   # operator flips → the private golden suite (promote.py)
    laya-lab compare agent-open-work --voter other=http://127.0.0.1:8010  # voters side by side (compare.py)
    laya-lab gate agent-open-work      # the promotion gate: newest unserved checkpoint vs the served one (gate.py)
    laya-lab bench                     # every voter this machine can ask, one protocol → bench/results/<date>.json (bench.py)
    laya-lab fleet                     # the rack's live use as counts; --write folds them into today's results (fleet.py)
    laya-lab card                      # the verifier card's preview; --publish --digest writes it into docs/ (card.py)
    laya-lab unregister coordinator    # back to the base checkpoint for that surface
    laya-lab verdict golden agent-open-work    # the verdict line of the report THIS scheduled run wrote
    laya-lab nightly agent-open-work   # the nightly pack: golden, SCORE, compare, shadow, readiness (nightly.py)
    laya-lab shadow agent-open-work    # each @challenger on the ledger's live states, beside the served p (shadow.py)
    laya-lab latency agent-open-work   # the ledger's decision times by length, load and cold start; token overflow (latency.py)
    laya-lab stability agent-open-work # STABLE κ: consecutive checkpoints' verdicts on the fixed stop set (stability.py)
    laya-lab registry show             # versions, aliases (@champion · @challenger · @previous), cuts, retention
    laya-lab registry migrate          # a v1 models.json → v2, nothing served changes (every write runs it too)
    laya-lab registry register agent_open_work <checkpoint folder> --as challenger   # never touches what is served
    laya-lab teacher-kappa             # gpt-oss:20b labels the Sonnet + operator keys; κ vs both (teacher_local.py)
    laya-lab claude-vote               # a Claude model's p per bench state, on the subscription (claude_voter.py)

AGENT RACK › LAYA's lab buttons start `sweep`, `review-open-work`, `promote`, `golden` and `compare`
(`plugins/laya/laya_bridge/lab_jobs.py` owns how, and why `train` is never one); their names and exit
codes are that contract.
Every command first tightens the operator's files (`laya_bridge.files.tighten_params`) and says what it changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from laya_lab.data import DEFAULT_EPOCHS, SURFACES, export_coordinator, tighten
from laya_lab.golden import SUITES as GOLDEN_SUITES
from laya_lab.heldout import LABEL_SOURCES
from laya_lab.latency import DEFAULT_DAYS as DEFAULT_LATENCY_DAYS
from laya_lab.metrics import fmt_kappa, fmt_num
from laya_lab.registry import AS_CHOICES, CHALLENGER, DATA_DIRNAME, base_dir, read_registry, unregister

DEVICE_HELP = "mps (Apple GPU), cpu, cuda or auto"
#: A v1 models.json asks nothing of the operator: the next write migrates it (`registry.read_registry`).
LEGACY_NOTE = " (v1: the next write or the nightly pack migrates it)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="laya-lab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="what AGENT RACK › LAYA shows: served model, cut, review, golden balance")
    commands.add_parser("export-coordinator", help="merge the latest Claude-ranked coordinator audit into the dataset")
    train = commands.add_parser("train", help="fine-tune, calibrate and evaluate one surface; register it if it beat the base")
    train.add_argument("surface", choices=SURFACES)
    train.add_argument("--data", help="JSONL to train on (default: <data>/laya/data/<surface>.jsonl)")
    train.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    train.add_argument("--device", default="mps", help=DEVICE_HELP)
    train.add_argument("--force", action="store_true", help="register even when the promotion gate refuses it")
    train.add_argument(
        "--as",
        dest="as_",
        choices=AS_CHOICES,
        default=CHALLENGER,
        help="challenger (default: shadow-scored, never served until promoted) or champion (served now)",
    )
    commands.add_parser("models", help="list registered fine-tunes and swept cuts")
    golden = commands.add_parser("golden", help="score a surface's question on its golden cases (public + private)")
    golden.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    golden.add_argument("--model", default="served", help="served (default), challenger, english, or a checkpoint folder name")
    targets = golden.add_mutually_exclusive_group()
    targets.add_argument("--all-checkpoints", action="store_true", help="the base and every fine-tune on disk, one report each")
    targets.add_argument("--registered", action="store_true", help="the base, the champion and every registered version")
    golden.add_argument("--device", default="mps", help=DEVICE_HELP)
    exp = commands.add_parser("export-open-work", help="merge turn ends for the agent rack's ✅⚠️ into the export")
    exp.add_argument("--limit", type=int, default=600, help="at most this many NEW turn ends (existing ones are kept)")
    exp.add_argument(
        "--root",
        action="append",
        default=[],
        help="another folder holding a `projects/` of transcripts, live or archived (*.jsonl.zst); repeatable",
    )
    qc = commands.add_parser(
        "question-challenger", help="fine-tune the served rows at another question version, to measure it (question.py)"
    )
    qc.add_argument("surface", choices=("agent_open_work",))
    qc.add_argument("--version", type=int, required=True, help="the question version to rebuild every state at")
    qc.add_argument("--epochs", type=int, default=None, help="default: train's")
    qc.add_argument("--device", default="mps", choices=("mps", "cuda", "cpu"))
    qc.add_argument("--dry-run", action="store_true", help="print the rows and how many carry a request; train nothing")
    qc.add_argument("--like", default="served", help="whose training record to rebuild: served (default) or a checkpoint")
    lab = commands.add_parser("label-open-work", help="label the exported turn ends with Claude (subscription only)")
    lab.add_argument("--model", default="sonnet")
    lab.add_argument("--batch", type=int, default=25)
    lab.add_argument("--max-calls", type=int, default=40)
    lab.add_argument("--profile", default=None, help="launch profile id (default: the bridge default)")
    lab.add_argument("--teacher", choices=("sonnet", "auto"), default="sonnet")
    lab.add_argument("--weekly", action="store_true", help="hold the run to the week's subscription-label cap")
    vote = commands.add_parser(
        "claude-vote", help="ask a Claude model p(open) of every state the bench scores (subscription only); bench reads the file"
    )
    vote.add_argument("suite", nargs="?", default="agent-open-work", choices=sorted(GOLDEN_SUITES))
    vote.add_argument("--model", default="claude-opus-5-5")
    vote.add_argument("--arm", action="append", default=[], choices=("rubric", "question"), help="repeatable (default: both)")
    vote.add_argument("--batch", type=int, default=25)
    vote.add_argument("--max-calls", type=int, default=60)
    vote.add_argument("--profile", default=None, help="launch profile id (default: the bridge default)")
    vote.add_argument("--dry-run", action="store_true", help="count what would be asked and score what is voted; call nothing")
    vote.add_argument("--ask-stdin", action="store_true", help=argparse.SUPPRESS)  # claude_voter.py § the ask
    retrain = commands.add_parser("retrain", help="resume the durable retrain into a shadow challenger")
    retrain.add_argument("suite", choices=("agent-open-work",))
    retrain.add_argument("--device", default="mps", help=DEVICE_HELP)
    retrain.add_argument("--owed", action="store_true", help="read-only fresh-json check")
    retrain.add_argument("--now", action="store_true", help="open off schedule; readiness and GPU-window gates still apply")
    review = commands.add_parser(
        "review-open-work", help="score every reviewable text with the served model (the deck's disagree stratum)"
    )
    review.add_argument("--model", default="served", help="served (default), english, or a checkpoint folder name")
    review.add_argument(
        "--full", action="store_true", help="score every key again (default: only keys with no p under this digest and device)"
    )
    review.add_argument("--device", default="mps", help=DEVICE_HELP)
    needs_you = commands.add_parser("needs-you", help="gated three-way head; shadow only")
    needs_you.add_argument("--device", choices=("mps", "cuda", "cpu"), default="mps")
    needs_you.add_argument("--train", action="store_true", help="train if the grade, reliability and attention gates pass")
    card = commands.add_parser(
        "card", help="build the count-only verifier card's preview; --publish writes the reviewed one into docs/"
    )
    card.add_argument(
        "--publish", action="store_true", help="write docs/VERIFIER.md, REPRODUCE.md and MODEL_CARD.md (exit 3: held)"
    )
    card.add_argument("--digest", default="", help="the digest of the preview that was reviewed")
    bench = commands.add_parser(
        "bench",
        help="every available voter on the public golden set and the private held-out "
        "split; aggregates only, into bench/results/<date>.json",
    )
    bench.add_argument("suite", nargs="?", default="agent-open-work", choices=sorted(GOLDEN_SUITES))
    bench.add_argument(
        "--voter",
        action="append",
        default=[],
        help="<name>=<loopback url | votes:<model>[/<arm>]>[,kind=,runtime=,licence=,params_m=,labels=,label=]; repeatable",
    )
    bench.add_argument("--labels", choices=LABEL_SOURCES, default="auto", help="the held-out set's labels (as sweep)")
    bench.add_argument("--dry-run", action="store_true", help="print the plan (voters, sets, n); load and write nothing")
    bench.add_argument("--device", choices=("mps", "cuda", "cpu"), default="mps")
    bench.add_argument(
        "--fleet",
        action="store_true",
        help="also measure the rack's own use (fleet.py) into the file, which is published; off by default",
    )
    bench.add_argument(
        "--challenger",
        action="append",
        default=[],
        help="an unregistered fine-tune (checkpoint folder) to score at its own question version; repeatable",
    )
    fleet = commands.add_parser(
        "fleet",
        help="the rack's live use (verdicts, ⚠️ raised, live latency, next-step taken) "
        "as counts; --write folds them into today's bench results file",
    )
    fleet.add_argument("suite", nargs="?", default="agent-open-work", choices=sorted(GOLDEN_SUITES))
    fleet.add_argument("--days", type=int, default=7, help="the window, ending now (default 7)")
    fleet.add_argument("--write", action="store_true", help="fold into bench/results/<today>.json, which a bench run wrote")
    challenger = commands.add_parser("challenger", help="armed challenger decision; requires current evidence token")
    challenger.add_argument("action", choices=("promote", "reject"))
    challenger.add_argument("--surface", choices=("agent_open_work",), required=True)
    challenger.add_argument("--token", required=True)
    sweep = commands.add_parser("sweep", help="sweep the cut on held-out labelled texts; --apply moves it")
    sweep.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    sweep.add_argument(
        "--labels",
        choices=LABEL_SOURCES,
        default="auto",
        help="auto (operator once ready, else merged), operator, model or merged",
    )
    sweep.add_argument(
        "--no-heldout",
        dest="heldout",
        action="store_false",
        help="every labelled row, trained ones too (IN-SAMPLE; never applied)",
    )
    sweep.add_argument(
        "--model",
        default="served",
        help="served (default), challenger, english, or a checkpoint folder name (--apply: a registered version)",
    )
    moves = sweep.add_mutually_exclusive_group()
    sweep.add_argument("--reserve", action="store_true", help="use only the operator reserve; required for challenger promotion")
    moves.add_argument("--apply", action="store_true", help="write the recommended cut to models.json (operator labels only)")
    moves.add_argument("--reset", action="store_true", help="drop the swept cut: the code's cut applies again")
    sweep.add_argument("--device", default="mps", help=DEVICE_HELP)
    settle = commands.add_parser("settle", help="drop provisional from signed-off public golden cases (dry run without --apply)")
    settle.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    settle.add_argument("--apply", action="store_true", help="rewrite the public suite's changed lines")
    promote = commands.add_parser("promote", help="operator-labelled texts → the PRIVATE golden suite (dry run without --apply)")
    promote.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    promote.add_argument("--flips", action="store_true", help="every operator flip waiting (what the rack counts)")
    promote.add_argument("--key", action="append", default=[], help="one operator-labelled text key; repeatable")
    promote.add_argument("--apply", action="store_true", help="write them, then rebuild the training set")
    compare = commands.add_parser("compare", help="score several voters side by side: Laya in process, any /v1/systemone server")
    compare.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    compare.add_argument(
        "--voter",
        action="append",
        default=[],
        help="laya:<served|english|checkpoint>, <name>=<loopback url> or <name>=votes:<model>[/<arm>]; repeatable "
        "(default: laya:served, laya:english)",
    )
    compare.add_argument("--labels", choices=LABEL_SOURCES, default="auto", help="the held-out set's labels (as sweep)")
    compare.add_argument("--golden-only", dest="heldout", action="store_false", help="skip the held-out set")
    compare.add_argument("--device", default="mps", help=DEVICE_HELP)
    gate = commands.add_parser("gate", help="the promotion gate train runs: a candidate checkpoint vs the served model, paired")
    gate.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    gate.add_argument("--candidate", default=None, help="a checkpoint folder name (default: the newest one not served)")
    gate.add_argument("--served", default="served", help="the champion: served (default), english, or a checkpoint folder name")
    gate.add_argument("--labels", choices=LABEL_SOURCES, default="merged", help="the held-out rows' labels (default merged)")
    gate.add_argument(
        "--prospective",
        action="store_true",
        help="after a registration: the served fine-tune vs the champion it replaced, on operator labels since",
    )
    gate.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="also write the verdict's evidence, trimmed to numbers, as a test fixture at this path",
    )
    gate.add_argument("--device", default="mps", help=DEVICE_HELP)
    drop = commands.add_parser("unregister", help="stop serving a surface's fine-tune (and its swept cut)")
    drop.add_argument("surface", choices=SURFACES)
    verdict = commands.add_parser(
        "verdict", help="print the verdict of the report THIS scheduled run wrote (its run id is in the environment)"
    )
    verdict.add_argument("kind", choices=("golden", "compare", "nightly", "latency", "retrain"))
    verdict.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    add_nightly_pack(commands)
    add_teacher_kappa(commands)
    return parser


# ── blocks: each its own parsers and commands ─────────────────────────────────


def add_nightly_pack(commands: Any) -> None:
    """The nightly Laya pack, the shadow pass and the champion/challenger registry (nightly.py, shadow.py,
    registry.py own them); the latency read and the stability meter (latency.py, stability.py)."""
    nightly = commands.add_parser(
        "nightly",
        help="the nightly pack: golden on every registered checkpoint, incremental "
        "SCORE, compare with the floor, the challenger shadow pass, readiness",
    )
    nightly.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    nightly.add_argument("--device", default="mps", help=DEVICE_HELP)
    shadow = commands.add_parser("shadow", help="each @challenger's p on the ledger's live states, beside the served p")
    shadow.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    shadow.add_argument(
        "--since", default=None, help="an ISO stamp (default: the challenger's registration, at most 14 days back)"
    )
    shadow.add_argument("--device", default="mps", help=DEVICE_HELP)
    latency = commands.add_parser(
        "latency",
        help="the ledger's decision times by state length, machine load and cold start, "
        "and the token overflow of the states Laya is asked (a read: no model runs)",
    )
    latency.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    latency.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LATENCY_DAYS,
        help=f"the decisions of the last N days (default {DEFAULT_LATENCY_DAYS}); 0: everything the ledger keeps",
    )
    stable = commands.add_parser(
        "stability",
        help="STABLE κ: score every registered checkpoint on the fixed stop set, "
        "then κ between consecutive checkpoints' verdicts",
    )
    stable.add_argument("suite", choices=sorted(GOLDEN_SUITES))
    stable.add_argument("--device", default="mps", help=DEVICE_HELP)
    reg = commands.add_parser("registry", help="models.json: show it, migrate a v1 file, register a challenger")
    verbs = reg.add_subparsers(dest="registry_verb", required=True)
    verbs.add_parser("show", help="versions, aliases, cuts and what retention would prune (reads only)")
    verbs.add_parser("migrate", help="a v1 models.json → v2 (the v1 file is kept beside it); nothing served changes")
    add = verbs.add_parser("register", help="register a checkpoint folder under checkpoints/ as a version, with an alias")
    add.add_argument("surface", choices=SURFACES)
    add.add_argument("checkpoint", help="its folder name under <data>/laya/checkpoints/")
    add.add_argument(
        "--as",
        dest="as_",
        choices=(CHALLENGER,),
        default=CHALLENGER,
        help="challenger: never served (a champion moves only by promotion, AGENT RACK › LAYA)",
    )


def cmd_nightly(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.nightly import run

    return run(args.suite, base, device=args.device)


def cmd_shadow(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.shadow import cmd

    return cmd(args.suite, base, device=args.device, since=args.since)


def cmd_latency(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.latency import run

    return run(args.suite, base, days=max(args.days, 0))


def cmd_stability(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.stability import cmd

    return cmd(args.suite, base, device=args.device)


def cmd_registry(args: argparse.Namespace, base: Path | None) -> int:
    from laya_lab import registry

    if args.registry_verb == "show":
        for line in registry_lines(base):
            print(line)
        return 0
    assert base is not None
    if args.registry_verb == "migrate":
        migrated, notes = registry.migrate_registry(base)
        print(f"[laya-lab] registry: {'migrated to v2' if migrated else 'already v2 (or absent): nothing to do'}")
        for note in notes:
            print(f"  {note}")
        return 0
    folder = base / registry.CHECKPOINTS_DIRNAME / args.checkpoint
    if not (folder / registry.WEIGHTS_FILE).is_file():
        print(f"[laya-lab] registry: {folder} holds no {registry.WEIGHTS_FILE}")
        return 3
    report_path = folder / "laya-lab-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    from laya_lab.gate import newest_verdict_for, suite_for_surface
    from laya_lab.train import compact_report
    from laya_bridge import lab_files

    suite = suite_for_surface(args.surface)
    verdict = newest_verdict_for(base, suite.name, args.checkpoint) if suite is not None else None
    tags = (
        None
        if verdict is None
        else lab_files.gate_tags(
            "PASS" if verdict.get("promote") else "FORCED",
            verdict.get("run_id"),
            None,
            {check: block.get("state") for check, block in (verdict.get("checks") or {}).items()},
        )
    )
    name = registry.register(base, args.surface, folder, compact_report(report), as_=args.as_, tags=tags)
    print(
        f"[laya-lab] registered {args.checkpoint} as @{args.as_} of {args.surface} ({name}); nothing served changed"
        + (f" · gate {tags['gate']} (run {tags['gate_run']})" if tags else " · no gate verdict names it: run `laya-lab gate`")
    )
    return 0


def registry_lines(base: Path | None) -> list[str]:
    """`registry show`: names, aliases, cuts and sizes only."""
    from laya_lab import registry

    view = registry.registry_versions(base)
    lines = [f"[laya-lab] registry · v{view['version'] or '-'}" + (LEGACY_NOTE if view["legacy"] else "")]
    for surface, aliases in sorted(view["aliases"].items()):
        lines.append(f"  {surface}")
        for alias in registry.ALIASES:
            version = view["versions"].get(aliases.get(alias) or "")
            if version is None:
                continue
            cut = version["cut"]
            lines.append(
                f"    @{alias:<10} {version['name']}  cut {cut['at']} swept {cut['swept_at']}"
                if cut
                else f"    @{alias:<10} {version['name']}  cut code"
            )
        if base is not None:
            kept = registry.retention(base, surface)
            gb = sum(item["bytes"] for item in kept["prunable"]) / 1e9
            lines.append(f"    retention  {len(kept['keep'])} kept · {len(kept['prunable'])} prunable ({gb:.1f} GB, on REJECT)")
    rejected = sorted(name for name, version in view["versions"].items() if version["rejected_at"])
    if rejected:
        lines.append(f"  rejected  {', '.join(rejected)}")
    return lines


def add_teacher_kappa(commands: Any) -> None:
    """The local teacher's κ against Sonnet and the operator (teacher_local.py owns it and the bar it reads)."""
    from laya_lab.teacher_local import TEACHER_MODEL

    teacher = commands.add_parser(
        "teacher-kappa", help="a local teacher (gpt-oss:20b) labels the Sonnet and operator keys; κ vs both"
    )
    teacher.add_argument("--model", default=TEACHER_MODEL, help=f"the Ollama tag of the teacher (default {TEACHER_MODEL})")
    teacher.add_argument(
        "--max-calls", type=int, default=None, help="stop after this many calls (default: every key not yet answered)"
    )
    reads = teacher.add_mutually_exclusive_group()
    reads.add_argument(
        "--check", action="store_true", help="fresh-json: fresh while the last report covers today's keys, rubric and weights"
    )
    reads.add_argument(
        "--verdict-json", action="store_true", help="the last report's verdict as one JSON line (a scheduler's probe)"
    )


def cmd_teacher_kappa(args: argparse.Namespace, base: Path | None) -> int:
    from laya_lab.teacher_local import cmd

    return cmd(args, base)


def reads_only(args: argparse.Namespace) -> bool:
    """A command that only reads the operator's tree: it must never create or tighten it."""
    return (
        (args.command == "retrain" and args.owed)
        or args.command in ("status", "verdict", "bench")
        or (args.command == "registry" and args.registry_verb == "show")
        or (args.command == "teacher-kappa" and (args.check or args.verdict_json))
    )


# ── commands: each takes (args, base) and returns the exit code ───────────────


def cmd_status(args: argparse.Namespace, base: Path | None) -> int:
    from laya_bridge.ops import ops_state

    for line in status_lines(ops_state(base, None, [])):
        print(line)
    return 0


def cmd_export_coordinator(args: argparse.Namespace, base: Path) -> int:
    added, total = export_coordinator(base / DATA_DIRNAME / "coordinator.jsonl")
    print(f"[laya-lab] coordinator: +{added} labelled findings, {total} in the dataset")
    return 0


def cmd_train(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.train import run

    return run(
        args.surface,
        base,
        Path(args.data) if args.data else None,
        epochs=args.epochs,
        device=args.device,
        force=args.force,
        as_=args.as_,
    )


def cmd_retrain(args: argparse.Namespace, base: Path | None) -> int:
    from laya_lab import retrain

    if args.owed:
        fresh, reason = retrain.check(args.suite, base)
        print(json.dumps({"fresh": fresh, "reason": reason}))
        return 0 if fresh else 1
    assert base is not None
    return retrain.run(args.suite, base, device=args.device, forced=args.now)


def cmd_models(args: argparse.Namespace, base: Path) -> int:
    payload = read_registry(base, migrate=False)
    print(json.dumps({key: payload.get(key) for key in ("versions", "aliases")}, indent=2, sort_keys=True))
    return 0


def cmd_golden(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.golden import run

    return run(
        args.suite, base, model=args.model, all_checkpoints=args.all_checkpoints, registered=args.registered, device=args.device
    )


def cmd_settle(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.golden import settle

    return settle(args.suite, base, apply=args.apply)


def cmd_export_open_work(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.open_work_data import export_turn_ends, transcript_roots

    roots = [*transcript_roots(), *(Path(root).expanduser() for root in args.root)]
    added, total, seen = export_turn_ends(base, roots, limit=args.limit)
    print(f"[laya-lab] open-work: +{added} new turn ends, {total} in the export ({seen} seen)")
    return 0


def cmd_question_challenger(args: argparse.Namespace, base: Path) -> int:
    from laya_lab import question

    return question.run(
        base, args.version, epochs=args.epochs or DEFAULT_EPOCHS, device=args.device, dry_run=args.dry_run, like=args.like
    )


def cmd_label_open_work(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.open_work_data import in_bridge_venv, label, reexec_in_bridge_venv, teacher_bar, label_local

    if args.teacher == "auto":
        bar = teacher_bar(base)
        if bar.clear:
            return label_local(base, bar, max_calls=args.max_calls)
    if args.weekly and args.model != "sonnet":
        raise SystemExit("the weekly subscription teacher is sonnet")
    if not in_bridge_venv():
        return reexec_in_bridge_venv(args.raw_argv)
    from laya_lab.infer import hold_slot

    # One unattended Claude job at a time on the subscription (the lease's subscription_claude slot).
    hold_slot("subscription_claude", "claude", 1, [args.model])
    return label(
        base, model=args.model, batch_size=args.batch, max_calls=args.max_calls, profile=args.profile, weekly=args.weekly
    )


def cmd_claude_vote(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.claude_voter import ARMS, ask, vote

    arms = tuple(dict.fromkeys(args.arm)) or ARMS
    if not args.ask_stdin:
        return vote(base, args.suite, model=args.model, arms=arms, dry_run=args.dry_run, argv=args.raw_argv)
    from laya_lab.infer import hold_slot

    # One unattended Claude job at a time on the subscription, as for label-open-work.
    hold_slot("subscription_claude", "claude", 1, [args.model])
    todo = json.load(sys.stdin)
    return ask(base, args.suite, todo, model=args.model, batch_size=args.batch, max_calls=args.max_calls, profile=args.profile)


def cmd_review_open_work(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.score import run

    return run(base, model=args.model, device=args.device, full=args.full)


def cmd_sweep(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.heldout import run_sweep

    return run_sweep(
        args.suite,
        base,
        model=args.model,
        labels=args.labels,
        heldout=args.heldout,
        apply=args.apply,
        reset=args.reset,
        device=args.device,
        reserve=args.reserve,
    )


def cmd_promote(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.promote import promote

    return promote(base, args.suite, keys=args.key, flips=args.flips, apply=args.apply)


def cmd_compare(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.compare import run

    return run(args.suite, base, voters=args.voter or None, labels=args.labels, heldout=args.heldout, device=args.device)


def cmd_gate(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.gate import run

    return run(
        args.suite,
        base,
        candidate=args.candidate,
        served=args.served,
        labels=args.labels,
        prospective=args.prospective,
        device=args.device,
        fixture=args.fixture,
    )


def cmd_verdict(args: argparse.Namespace, base: Path | None) -> int:
    """A scheduler's verdict probe: one JSON line, exit 0, or the reason on stderr and exit 1 (the run is then
    incomplete). `lab_files.run_verdict` owns the rule, and which report is THIS run's."""
    from laya_bridge import lab_files

    verdict, why = lab_files.run_verdict(base, args.kind, args.suite)
    if verdict is None:
        print(f"[laya-lab] verdict: {why}", file=sys.stderr)
        return 1
    print(json.dumps(verdict, allow_nan=False))
    return 0


def cmd_unregister(args: argparse.Namespace, base: Path) -> int:
    print("[laya-lab] removed" if unregister(base, args.surface) else "[laya-lab] nothing registered for that surface")
    return 0


def cmd_needs_you(args, base):
    from laya_lab.needs_you import cmd

    return cmd(args, base)


def cmd_card(args, base):
    from laya_lab.card import cmd

    return cmd(args, base)


def cmd_bench(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.bench import run

    return run(
        args.suite,
        base,
        voters=args.voter,
        labels=args.labels,
        device=args.device,
        dry_run=args.dry_run,
        challengers=args.challenger,
        with_fleet=args.fleet,
    )


def cmd_fleet(args: argparse.Namespace, base: Path) -> int:
    from laya_lab.fleet import cmd

    return cmd(args.suite, base, days=args.days, write=args.write)


def cmd_challenger(args, base):
    from laya_lab.challenger import cmd

    return cmd(args, base)


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "status": cmd_status,
    "export-coordinator": cmd_export_coordinator,
    "train": cmd_train,
    "models": cmd_models,
    "golden": cmd_golden,
    "settle": cmd_settle,
    "export-open-work": cmd_export_open_work,
    "question-challenger": cmd_question_challenger,
    "label-open-work": cmd_label_open_work,
    "claude-vote": cmd_claude_vote,
    "review-open-work": cmd_review_open_work,
    "sweep": cmd_sweep,
    "promote": cmd_promote,
    "compare": cmd_compare,
    "gate": cmd_gate,
    "unregister": cmd_unregister,
    "verdict": cmd_verdict,
    "nightly": cmd_nightly,
    "shadow": cmd_shadow,
    "latency": cmd_latency,
    "stability": cmd_stability,
    "registry": cmd_registry,
    "retrain": cmd_retrain,
    "challenger": cmd_challenger,
    "card": cmd_card,
    "bench": cmd_bench,
    "fleet": cmd_fleet,
    "needs-you": cmd_needs_you,
    "teacher-kappa": cmd_teacher_kappa,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.raw_argv = argv if argv is not None else sys.argv[1:]
    base = base_dir(create=not reads_only(args))  # a read never creates the tree
    tighten(base)
    return COMMANDS[args.command](args, base)


# ── status ────────────────────────────────────────────────────────────────────


def status_lines(state: dict[str, Any]) -> list[str]:
    """`ops_state` as a readout: one block per surface, numbers first. Never text from the operator's files."""
    lines = [f"[laya-lab] status · {state['message']}"]
    review = {row["surface"]: row for row in state["review"]}
    sweeps = {row["surface"]: row for row in state["sweeps"]}
    golden = {row["surface"]: row for row in state["golden"]}
    for served in state["served"]:
        surface = served["surface"]
        cut = served["threshold"]
        lines.append(f"  {surface}")
        lines.append(
            f"    served   {served['name']}"
            + (f" · {served['checkpoint']}" if served["checkpoint"] else " (built-in)")
            + f" · cut {cut['at']} {cut['source']}"
            + (f" {cut['swept_at']}" if cut["swept_at"] else "")
        )
        if served["eval"]:
            before, after = served["eval"]["before"], served["eval"]["after"]
            lines.append(
                f"    eval     {served['eval']['items']} held-out · loss {before['loss']:.3f} → {after['loss']:.3f} · "
                f"accuracy {before['accuracy']:.3f} → {after['accuracy']:.3f}"
            )
        if served["golden"]:
            g = served["golden"]
            lines.append(
                f"    golden   {g['at']} · {g['cases']} cases · accuracy {g['accuracy']} · κ {fmt_num(g['kappa'])} · brier {g['brier']}"
            )
        if served["scores"]:
            s = served["scores"]
            lines.append(
                f"    scores   {s['at']} · {s['rows']} rows · {'current' if s['current'] else 'STALE: another checkpoint — run review-open-work'}"
            )
        if surface in review:
            r = review[surface]
            ready = r["sweep_ready"]
            lines.append(
                f"    review   {r['to_review']} to review · {r['backlog']} backlog · {r['reviewed']} answered "
                f"({r['agree']} agree / {r['flip']} flip / {r['skip']} skip) · κ sonnet "
                f"{fmt_kappa(r['operator_vs_sonnet']['kappa'], r['operator_vs_sonnet'].get('kappa_ci'))} "
                f"(n {r['operator_vs_sonnet']['n']}) · κ laya "
                f"{fmt_kappa(r['operator_vs_laya']['kappa'], r['operator_vs_laya'].get('kappa_ci'))} (n {r['operator_vs_laya']['n']})"
            )
            lines.append(
                f"    sweep    {'ready' if ready['ready'] else 'not ready'} · {ready['rows']} clean operator labels "
                f"({ready['open']} open / {ready['done']} done, {ready['reserve']['rows']} in reserve)"
                + (f" · {ready['reason']}" if ready["reason"] else "")
            )
        if surface in sweeps and sweeps[surface]["at"]:
            w = sweeps[surface]
            fit = w.get("calibration")
            lines.append(
                f"    heldout  {w['at']} · {w['rows']} rows {w['labels']} · best {w['best']} · live {w['live']} {w['source']} · κ {fmt_num(w['kappa'])}"
                + (
                    f" · ECE operator {fmt_num(fit['ece'], 3)} / teacher {fmt_num(fit['teacher_ece'], 3)} · "
                    f"T {fmt_num(fit['temperature'])} → {fmt_num(fit['ece_refit'], 3)}"
                    if fit
                    else ""
                )
            )
        if surface in golden:
            b = golden[surface]
            lines.append(
                f"    suites   {b['suite']}: public {b['public']['open']} open/{b['public']['done']} done · "
                f"private {b['private']['open']} open/{b['private']['done']} done · {b['candidates']} to promote"
                + (f" · {b['reserved']} held in reserve" if b["reserved"] else "")
            )
    for reg in state.get("registry") or []:
        champion, challenger = reg["champion"], reg["challenger"]
        head = f"  registry {reg['surface']} v{reg['version'] or '-'}" + (LEGACY_NOTE if reg["legacy"] else "")
        head += f" · @champion {champion['name'] if champion else 'english'}"
        if challenger:
            shadow = reg["shadow"] or {}
            head += (
                f" · @challenger {challenger['name']} gate {challenger['gate'] or '-'}"
                f" cut {challenger['cut']['at'] if challenger['cut'] else 'code'}"
                f" · shadow {shadow.get('rows', 0)} rows / {shadow.get('days', 0)} d · agree {fmt_num(shadow.get('agree'), 3)}"
            )
        if reg["nightly"]:
            head += f" · nightly {reg['nightly']['at']} {reg['nightly']['verdict']}"
        lines.append(head)
    for row in state.get("latency") or []:
        cut = row["truncation"]
        lines.append(
            f"  latency  {row['surface']} · {row['at']} · {row['decisions']} decisions · p50 {fmt_num(row['p50_ms'], 0)} ms · "
            f"p90 {fmt_num(row['p90_ms'], 0)} ms · cold p50 {fmt_num(row['cold']['p50_ms'], 0)} ({row['cold']['n']}) / "
            f"warm {fmt_num(row['warm']['p50_ms'], 0)} ({row['warm']['n']})"
            + (f" · {cut['overflowed']} of {cut['states']} states overflow" if cut else " · window not measured")
        )
    for row in state.get("stability") or []:
        lines.append(
            f"  stable   {row['surface']} · κ {fmt_num(row['kappa_average'], 3)} over {row['window']} pairs"
            f"{' · stable' if row['stable'] else ''} · {row['states']} states · {row['versions']} checkpoints · {row['last']}"
        )
    for surface, flags in sorted(state["flags_7d"].items()):
        lines.append(
            f"  flags 7d {surface}: {flags['decisions']} decisions · {flags['shown']} shown · "
            f"{flags['dismissed']} dismissed · {flags['accepted']} accepted"
        )
    if not state["lab"]["ok"]:
        lines.append(f"  lab      not installed: {state['lab']['hint']}")
    return lines


if __name__ == "__main__":
    sys.exit(main())
