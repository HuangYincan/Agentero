#!/usr/bin/env python3
"""figure-digitizer entry point (Agentero bundled skill).

Subcommands mirror the skill workflow:

    routes            machine-readable capability registry
    inspect           preflight: input composition + route proposal (never values)
    crop              cut a panel out of a page raster without resampling
    validate-spec     static check of a filled figure spec
    extract           deterministic extraction + evidence artifacts

Every numeric path is gated on a spec that the agent marked `verified` after
confirming the panel, axes and series with the user. Nothing here guesses.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digitizer_core as core  # noqa: E402


def _load_spec(path: str) -> dict:
    try:
        return core.load_json(path)
    except FileNotFoundError:
        raise SystemExit(f"spec not found: {path}")
    except json.JSONDecodeError as error:
        raise SystemExit(f"spec is not valid JSON ({path}): {error}")


def cmd_routes(args: argparse.Namespace) -> int:
    routes = core.route_table()
    if args.json:
        print(json.dumps({"tool_version": core.TOOL_VERSION, "routes": routes}, indent=2, ensure_ascii=False))
        return 0
    print(f"figure-digitizer {core.TOOL_VERSION}")
    for route in routes:
        types = ", ".join(route["chart_types"][:6])
        print(f"\n{route['id']}  [{route['maturity']}]")
        print(f"  chart types: {types}")
        print(f"  grammar:     {route['grammar']}")
        for refusal in route["refuses"]:
            print(f"  refuses:     {refusal}")
    print(
        "\nA route is a proposal, never an authorisation. Confirm the panel, axes and "
        "series with the user, then fill the spec and keep figure.verified=false until "
        "you have looked at the figure yourself."
    )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    source = core.RasterSource.load(args.input)
    report = core.preflight(source, args.chart_type)
    spec = core.spec_template(source, args.chart_type)
    if args.output_report:
        core.write_json(args.output_report, report)
    if args.output_spec:
        core.write_json(args.output_spec, spec)
    if args.json:
        print(json.dumps({"preflight": report, "spec_template": spec}, indent=2, ensure_ascii=False))
        return 0
    print(f"input      {source.path}")
    print(f"identity   sha256 {source.sha256[:16]}…  {source.width}x{source.height}")
    print(f"background {report['content']['background']}  content bbox {report['content']['content_bbox']}")
    print(f"proposal   {report['proposal']['route'] or '—'} ({report['proposal']['status']})")
    for note in report["notes"]:
        print(f"note       {note}")
    print("numeric_output_authorized: false (preflight never authorises values)")
    return 0


def cmd_crop(args: argparse.Namespace) -> int:
    source = core.RasterSource.load(args.input)
    bbox = [float(part) for part in args.bbox.split(",")]
    try:
        record = core.crop_region(
            source,
            bbox,
            args.output,
            bounds_mode=args.bbox_mode,
            review_scale=args.review_scale,
        )
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0
    crop = record["crop"]
    print(f"crop       {crop['path']}  {crop['width']}x{crop['height']}  sha256 {crop['sha256'][:16]}…")
    print(f"parent     {source.path}  page-relative bbox {record['bbox']} -> pixels {record['pixel_rect']}")
    print(f"measurable {record['measurable']}")
    print(f"sidecar    {Path(crop['path']).with_suffix(Path(crop['path']).suffix + '.crop.json')}")
    if not record["measurable"]:
        print("note       enlarged copies are for review only: never measure on them")
    return 0


def cmd_validate_spec(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    problems = core.validate_spec(spec)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2, ensure_ascii=False))
    elif problems:
        print(f"spec is not ready ({len(problems)} problem(s)):")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("spec is structurally valid; extraction gates still apply at run time")
    return 1 if problems else 0


def cmd_extract(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec)
    input_path = args.input or (spec.get("source") or {}).get("path")
    if not input_path:
        raise SystemExit("no raster to measure: pass --input or set source.path in the spec")
    if args.input and (spec.get("source") or {}).get("path") not in (None, "", args.input):
        # Measuring a different file than the spec recorded would break the
        # original-raster invariant, so refuse rather than silently re-anchor.
        raise SystemExit(
            "refusing: --input differs from source.path in the spec. Re-run `inspect` on the "
            "file you actually intend to measure and fill a fresh spec."
        )
    source = core.RasterSource.load(input_path)
    try:
        report = core.run_extraction(spec, source, args.output_dir)
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"status     {report['status']}")
    print(f"route      {report['route']['id']} ({report['route']['maturity']})")
    print(f"rows       {report['row_count']}")
    print(f"authorized {report['numeric_output_authorized']}")
    for blocker in report["authorization_blockers"]:
        print(f"blocker    {blocker}")
    print(f"csv        {report['artifacts'].get('data_csv')}")
    print(f"overlay    {report['artifacts'].get('overlay_png', {}).get('path')}")
    if "recreated_png" in report["artifacts"]:
        print(f"recreated  {report['artifacts']['recreated_png']['path']}")
    print(f"report     {Path(args.output_dir) / 'report.json'}")
    print("\nOpen the overlay at original resolution before quoting any value.")
    return 0 if report["numeric_output_authorized"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="figure_digitizer",
        description="Evidence-bound extraction of chart values from raster figures.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # `--json` is accepted before or after the subcommand; SUPPRESS keeps the
    # sub-parser copy from resetting the value the main parser already read.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")

    routes = subparsers.add_parser("routes", parents=[common], help="list the capability registry")
    routes.set_defaults(func=cmd_routes)

    inspect = subparsers.add_parser("inspect", parents=[common],
                                    help="preflight an image and draft a spec")
    inspect.add_argument("--input", required=True, help="original raster (PNG/JPG/TIFF)")
    inspect.add_argument("--chart-type", default=None, help="confirmed chart type, if known")
    inspect.add_argument("--output-report", default=None, help="preflight report path")
    inspect.add_argument("--output-spec", default=None, help="spec template path")
    inspect.set_defaults(func=cmd_inspect)

    crop = subparsers.add_parser("crop", parents=[common], help="cut a panel out of a page raster (no resampling)")
    crop.add_argument("--input", required=True, help="page raster produced by the PDF rasterizer")
    crop.add_argument("--bbox", required=True, help="x,y,width,height (page-relative by default)")
    crop.add_argument("--bbox-mode", choices=["normalized", "pixels"], default="normalized")
    crop.add_argument("--output", required=True, help="crop PNG path")
    crop.add_argument("--review-scale", type=float, default=1.0,
                      help="nearest-neighbour enlargement for review; >1 is not measurable")
    crop.set_defaults(func=cmd_crop)

    validate = subparsers.add_parser("validate-spec", parents=[common], help="check a filled spec")
    validate.add_argument("--spec", required=True)
    validate.set_defaults(func=cmd_validate_spec)

    extract = subparsers.add_parser("extract", parents=[common], help="run the registered extractor")
    extract.add_argument("--spec", required=True)
    extract.add_argument("--output-dir", required=True)
    extract.add_argument("--input", default=None, help="override only when it matches source.path")
    extract.set_defaults(func=cmd_extract)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
