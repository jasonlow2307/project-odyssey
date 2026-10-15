#!/usr/bin/env python3
"""Read-only cross-user duplicate reporting for an Immich PostgreSQL database.

The tool reuses Immich's existing checksum and CLIP embedding data. It never
updates Immich tables and runs all database work in a read-only transaction.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_MAX_DISTANCE = 0.01
DEFAULT_CANDIDATE_LIMIT = 64
DEFAULT_GROUP_LIMIT = 20


@dataclass(frozen=True)
class Asset:
    asset_id: str
    owner_id: str
    owner_name: str
    filename: str
    original_path: str
    width: int | None
    height: int | None
    file_size_bytes: int | None
    local_date_time: str | None
    checksum_sha1: str | None
    asset_type: str


@dataclass(frozen=True)
class VisualEdge:
    left_id: str
    right_id: str
    distance: float


@dataclass(frozen=True)
class ExactGroup:
    group_id: int
    checksum_sha1: str
    assets: tuple[Asset, ...]


@dataclass(frozen=True)
class VisualGroup:
    group_id: int
    min_distance: float
    assets: tuple[Asset, ...]
    edges: tuple[VisualEdge, ...]


class UnionFind:
    """Small disjoint-set implementation used to form visual groups."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, value: str) -> None:
        self._parent.setdefault(value, value)

    def find(self, value: str) -> str:
        parent = self._parent[value]
        if parent != value:
            parent = self.find(parent)
            self._parent[value] = parent
        return parent

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


def asset_from_row(row: Mapping[str, Any], prefix: str = "") -> Asset:
    """Convert a psycopg dict row into a stable, JSON-friendly asset value."""

    def value(name: str) -> Any:
        return row.get(f"{prefix}{name}")

    local_date_time = value("local_date_time")
    if isinstance(local_date_time, datetime):
        local_date_time = local_date_time.isoformat()

    return Asset(
        asset_id=str(value("asset_id")),
        owner_id=str(value("owner_id")),
        owner_name=str(value("owner_name") or value("owner_id")),
        filename=str(value("filename") or ""),
        original_path=str(value("original_path") or ""),
        width=_as_int(value("width")),
        height=_as_int(value("height")),
        file_size_bytes=_as_int(value("file_size_bytes")),
        local_date_time=local_date_time,
        checksum_sha1=value("checksum_sha1"),
        asset_type=str(value("asset_type") or ""),
    )


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def build_owner_filters(alias: str, args: argparse.Namespace) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if args.owner_id:
        clauses.append(f'{alias}."ownerId" = ANY(%s::uuid[])')
        params.append(args.owner_id)
    if args.exclude_owner_id:
        clauses.append(f'{alias}."ownerId" <> ALL(%s::uuid[])')
        params.append(args.exclude_owner_id)
    return clauses, params


def build_asset_filters(
    alias: str,
    args: argparse.Namespace,
    *,
    include_types: bool,
) -> tuple[list[str], list[Any]]:
    clauses = [
        f'{alias}."deletedAt" IS NULL',
        f'{alias}."stackId" IS NULL',
    ]
    params: list[Any] = []
    if not args.include_hidden:
        clauses.append(f'{alias}."visibility" IN (\'timeline\', \'archive\')')
    if include_types and args.asset_type:
        clauses.append(f'{alias}."type" = ANY(%s::text[])')
        params.append(args.asset_type)
    owner_clauses, owner_params = build_owner_filters(alias, args)
    clauses.extend(owner_clauses)
    params.extend(owner_params)
    return clauses, params


def asset_projection(
    alias: str,
    user_alias: str,
    exif_alias: str,
    *,
    include_checksum_value: bool = False,
) -> str:
    checksum_value = f', {alias}."checksum" AS checksum_value' if include_checksum_value else ""
    return f"""
        {alias}."id"::text AS asset_id,
        {alias}."ownerId"::text AS owner_id,
        COALESCE({user_alias}."name", {alias}."ownerId"::text) AS owner_name,
        {alias}."originalFileName" AS filename,
        {alias}."originalPath" AS original_path,
        COALESCE({alias}."width", {exif_alias}."exifImageWidth") AS width,
        COALESCE({alias}."height", {exif_alias}."exifImageHeight") AS height,
        {exif_alias}."fileSizeInByte" AS file_size_bytes,
        {alias}."localDateTime" AS local_date_time,
        encode({alias}."checksum", 'hex') AS checksum_sha1,
        {alias}."type" AS asset_type{checksum_value}
    """


def fetch_exact_assets(cursor: Any, args: argparse.Namespace) -> list[Asset]:
    filters, params = build_asset_filters("a", args, include_types=True)
    filter_sql = " AND ".join(filters)
    query = f"""
        WITH eligible AS (
            SELECT
                {asset_projection("a", "u", "e", include_checksum_value=True)}
            FROM "asset" AS a
            LEFT JOIN "user" AS u ON u."id" = a."ownerId"
            LEFT JOIN "asset_exif" AS e ON e."assetId" = a."id"
            WHERE {filter_sql}
              AND a."checksum" IS NOT NULL
        ), cross_user_checksums AS (
            SELECT checksum_value
            FROM eligible
            GROUP BY checksum_value
            HAVING COUNT(DISTINCT owner_id) > 1
        )
        SELECT e.*
        FROM eligible AS e
        INNER JOIN cross_user_checksums AS c
            ON c.checksum_value = e.checksum_value
        ORDER BY e.checksum_sha1, e.owner_id, e.asset_id
    """
    cursor.execute(query, params)
    return [asset_from_row(row) for row in cursor.fetchall()]


def fetch_visual_edges(
    cursor: Any,
    args: argparse.Namespace,
) -> tuple[dict[str, Asset], list[VisualEdge]]:
    source_filters, source_params = build_asset_filters("a", args, include_types=True)
    candidate_filters, candidate_params = build_asset_filters("c", args, include_types=False)
    source_filter_sql = " AND ".join(source_filters)
    candidate_filter_sql = " AND ".join(candidate_filters)

    source_limit_sql = ""
    source_limit_params: list[Any] = []
    if args.source_limit > 0:
        source_limit_sql = " LIMIT %s"
        source_limit_params.append(args.source_limit)

    exact_filter = ""
    if not args.include_exact_in_visual:
        exact_filter = "AND candidate.checksum_sha1 IS DISTINCT FROM source.checksum_sha1"

    query = f"""
        SELECT
            source.asset_id AS source_asset_id,
            source.owner_id AS source_owner_id,
            source.owner_name AS source_owner_name,
            source.filename AS source_filename,
            source.original_path AS source_original_path,
            source.width AS source_width,
            source.height AS source_height,
            source.file_size_bytes AS source_file_size_bytes,
            source.local_date_time AS source_local_date_time,
            source.checksum_sha1 AS source_checksum_sha1,
            source.asset_type AS source_asset_type,
            candidate.asset_id AS candidate_asset_id,
            candidate.owner_id AS candidate_owner_id,
            candidate.owner_name AS candidate_owner_name,
            candidate.filename AS candidate_filename,
            candidate.original_path AS candidate_original_path,
            candidate.width AS candidate_width,
            candidate.height AS candidate_height,
            candidate.file_size_bytes AS candidate_file_size_bytes,
            candidate.local_date_time AS candidate_local_date_time,
            candidate.checksum_sha1 AS candidate_checksum_sha1,
            candidate.asset_type AS candidate_asset_type,
            candidate.distance
        FROM (
            SELECT
                {asset_projection("a", "u", "e")},
                ss."embedding"
            FROM "asset" AS a
            INNER JOIN "smart_search" AS ss ON ss."assetId" = a."id"
            LEFT JOIN "user" AS u ON u."id" = a."ownerId"
            LEFT JOIN "asset_exif" AS e ON e."assetId" = a."id"
            WHERE {source_filter_sql}
            ORDER BY a."id"{source_limit_sql}
        ) AS source
        CROSS JOIN LATERAL (
            SELECT
                {asset_projection("c", "cu", "ce")},
                cs."embedding" <=> source."embedding" AS distance
            FROM "asset" AS c
            INNER JOIN "smart_search" AS cs ON cs."assetId" = c."id"
            LEFT JOIN "user" AS cu ON cu."id" = c."ownerId"
            LEFT JOIN "asset_exif" AS ce ON ce."assetId" = c."id"
            WHERE {candidate_filter_sql}
              AND c."ownerId" <> source.owner_id::uuid
              AND c."type" = source.asset_type
              AND c."id" <> source.asset_id::uuid
            ORDER BY cs."embedding" <=> source."embedding"
            LIMIT %s
        ) AS candidate
        WHERE candidate.distance <= %s
        {exact_filter}
        ORDER BY candidate.distance, source.asset_id, candidate.asset_id
    """
    params = source_params + source_limit_params + candidate_params
    params.extend([args.candidate_limit, args.max_distance])
    cursor.execute(query, params)

    assets: dict[str, Asset] = {}
    edges: dict[frozenset[str], VisualEdge] = {}
    for row in cursor.fetchall():
        source = asset_from_row(
            {
                "asset_id": row["source_asset_id"],
                "owner_id": row["source_owner_id"],
                "owner_name": row["source_owner_name"],
                "filename": row["source_filename"],
                "original_path": row["source_original_path"],
                "width": row["source_width"],
                "height": row["source_height"],
                "file_size_bytes": row["source_file_size_bytes"],
                "local_date_time": row["source_local_date_time"],
                "checksum_sha1": row["source_checksum_sha1"],
                "asset_type": row["source_asset_type"],
            }
        )
        candidate = asset_from_row(
            {
                "asset_id": row["candidate_asset_id"],
                "owner_id": row["candidate_owner_id"],
                "owner_name": row["candidate_owner_name"],
                "filename": row["candidate_filename"],
                "original_path": row["candidate_original_path"],
                "width": row["candidate_width"],
                "height": row["candidate_height"],
                "file_size_bytes": row["candidate_file_size_bytes"],
                "local_date_time": row["candidate_local_date_time"],
                "checksum_sha1": row["candidate_checksum_sha1"],
                "asset_type": row["candidate_asset_type"],
            }
        )
        assets[source.asset_id] = source
        assets[candidate.asset_id] = candidate
        left_id, right_id = sorted((source.asset_id, candidate.asset_id))
        edge = VisualEdge(left_id, right_id, float(row["distance"]))
        edge_key = frozenset((left_id, right_id))
        if edge_key not in edges or edge.distance < edges[edge_key].distance:
            edges[edge_key] = edge
    return assets, list(edges.values())


def build_exact_groups(assets: Iterable[Asset], limit: int) -> list[ExactGroup]:
    grouped: defaultdict[str, list[Asset]] = defaultdict(list)
    for asset in assets:
        if asset.checksum_sha1:
            grouped[asset.checksum_sha1].append(asset)

    groups = [
        ExactGroup(group_id=index, checksum_sha1=checksum, assets=tuple(sorted(group, key=asset_sort_key)))
        for index, (checksum, group) in enumerate(sorted(grouped.items()), start=1)
        if len({asset.owner_id for asset in group}) > 1
    ]
    return groups[:limit] if limit > 0 else groups


def build_visual_groups(
    assets: Mapping[str, Asset],
    edges: Iterable[VisualEdge],
    limit: int,
) -> list[VisualGroup]:
    edge_list = list(edges)
    union_find = UnionFind()
    for edge in edge_list:
        union_find.union(edge.left_id, edge.right_id)

    component_assets: defaultdict[str, set[str]] = defaultdict(set)
    for asset_id in assets:
        if asset_id in union_find._parent:
            component_assets[union_find.find(asset_id)].add(asset_id)

    component_edges: defaultdict[str, list[VisualEdge]] = defaultdict(list)
    for edge in edge_list:
        component_edges[union_find.find(edge.left_id)].append(edge)

    candidates: list[VisualGroup] = []
    for component_root, asset_ids in component_assets.items():
        component = tuple(sorted((assets[asset_id] for asset_id in asset_ids), key=asset_sort_key))
        if len({asset.owner_id for asset in component}) < 2:
            continue
        component_edges_sorted = tuple(
            sorted(component_edges[component_root], key=lambda edge: edge.distance)
        )
        candidates.append(
            VisualGroup(
                group_id=0,
                min_distance=component_edges_sorted[0].distance,
                assets=component,
                edges=component_edges_sorted,
            )
        )

    candidates.sort(key=lambda group: (group.min_distance, len(group.assets)))
    selected = candidates[:limit] if limit > 0 else candidates
    return [
        VisualGroup(
            group_id=index,
            min_distance=group.min_distance,
            assets=group.assets,
            edges=group.edges,
        )
        for index, group in enumerate(selected, start=1)
    ]


def asset_sort_key(asset: Asset) -> tuple[str, str, str]:
    return asset.owner_name.lower(), asset.filename.lower(), asset.asset_id


def bytes_human(value: int | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(amount) < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


def redundant_bytes(assets: Sequence[Asset]) -> int:
    sizes = [asset.file_size_bytes for asset in assets if asset.file_size_bytes is not None]
    return max(0, sum(sizes) - max(sizes, default=0))


def output_rows(
    exact_groups: Sequence[ExactGroup],
    visual_groups: Sequence[VisualGroup],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in exact_groups:
        for asset in group.assets:
            rows.append(
                {
                    "kind": "exact",
                    "group_id": group.group_id,
                    "distance": "",
                    **asset_to_dict(asset),
                }
            )
    for group in visual_groups:
        for asset in group.assets:
            rows.append(
                {
                    "kind": "visual",
                    "group_id": group.group_id,
                    "distance": f"{group.min_distance:.6f}",
                    **asset_to_dict(asset),
                }
            )
    return rows


def asset_to_dict(asset: Asset) -> dict[str, Any]:
    return asdict(asset)


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = [
        "kind",
        "group_id",
        "distance",
        "asset_id",
        "owner_id",
        "owner_name",
        "filename",
        "original_path",
        "width",
        "height",
        "file_size_bytes",
        "local_date_time",
        "checksum_sha1",
        "asset_type",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_pair_csv(path: Path, groups: Sequence[VisualGroup]) -> None:
    fields = ["group_id", "left_asset_id", "right_asset_id", "distance"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for group in groups:
            for edge in group.edges:
                writer.writerow(
                    {
                        "group_id": group.group_id,
                        "left_asset_id": edge.left_id,
                        "right_asset_id": edge.right_id,
                        "distance": f"{edge.distance:.6f}",
                    }
                )


def write_json(
    path: Path,
    exact_groups: Sequence[ExactGroup],
    visual_groups: Sequence[VisualGroup],
    args: argparse.Namespace,
) -> None:
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "read_only": True,
        "settings": {
            "max_distance": args.max_distance,
            "candidate_limit": args.candidate_limit,
            "include_exact_in_visual": args.include_exact_in_visual,
            "asset_type": args.asset_type or ["ALL"],
            "source_limit": args.source_limit or None,
        },
        "exact_groups": [
            {
                "group_id": group.group_id,
                "checksum_sha1": group.checksum_sha1,
                "assets": [asset_to_dict(asset) for asset in group.assets],
            }
            for group in exact_groups
        ],
        "visual_groups": [
            {
                "group_id": group.group_id,
                "min_distance": group.min_distance,
                "assets": [asset_to_dict(asset) for asset in group.assets],
                "edges": [asdict(edge) for edge in group.edges],
            }
            for group in visual_groups
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def html_asset(asset: Asset) -> str:
    return """<tr>
        <td>{owner}</td>
        <td>{filename}</td>
        <td>{dimensions}</td>
        <td>{size}</td>
        <td>{date}</td>
        <td><code>{checksum}</code></td>
        <td><code>{asset_id}</code></td>
    </tr>""".format(
        owner=html.escape(f"{asset.owner_name} ({asset.owner_id})"),
        filename=html.escape(asset.filename),
        dimensions=html.escape(
            f"{asset.width or '?'}×{asset.height or '?'}" if asset.width or asset.height else "—"
        ),
        size=html.escape(bytes_human(asset.file_size_bytes)),
        date=html.escape(asset.local_date_time or "—"),
        checksum=html.escape(asset.checksum_sha1 or "—"),
        asset_id=html.escape(asset.asset_id),
    )


def html_group_table(assets: Sequence[Asset]) -> str:
    rows = "\n".join(html_asset(asset) for asset in assets)
    return f"""<table>
        <thead><tr><th>Owner</th><th>Filename</th><th>Resolution</th><th>Size</th><th>Captured</th><th>SHA-1</th><th>Asset ID</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>"""


def write_html(
    path: Path,
    exact_groups: Sequence[ExactGroup],
    visual_groups: Sequence[VisualGroup],
    args: argparse.Namespace,
) -> None:
    exact_total = sum(redundant_bytes(group.assets) for group in exact_groups)
    visual_total = sum(redundant_bytes(group.assets) for group in visual_groups)
    exact_sections = []
    for group in exact_groups:
        exact_sections.append(
            f"<article><h3>Exact group {group.group_id}</h3>"
            f"<p>SHA-1 <code>{html.escape(group.checksum_sha1)}</code>; "
            f"{len(group.assets)} assets; approximately {html.escape(bytes_human(redundant_bytes(group.assets)))} "
            "redundant by file size.</p>"
            f"{html_group_table(group.assets)}</article>"
        )
    visual_sections = []
    for group in visual_groups:
        visual_sections.append(
            f"<article><h3>Visual group {group.group_id}</h3>"
            f"<p>Minimum CLIP distance: <code>{group.min_distance:.6f}</code>; "
            f"{len(group.assets)} assets; approximately {html.escape(bytes_human(redundant_bytes(group.assets)))} "
            "redundant by file size.</p>"
            f"{html_group_table(group.assets)}</article>"
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Immich cross-user duplicate report</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #202124; }}
article {{ border: 1px solid #dadce0; border-radius: .5rem; margin: 1rem 0; padding: 1rem; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #eee; padding: .45rem; text-align: left; vertical-align: top; }}
th {{ background: #f8f9fa; }}
code {{ overflow-wrap: anywhere; }}
.warning {{ background: #fff4ce; border-left: .25rem solid #f9ab00; padding: .75rem 1rem; }}
</style>
</head>
<body>
<h1>Immich cross-user duplicate report</h1>
<p>Generated {html.escape(datetime.now().astimezone().isoformat())}. Database work was read-only.</p>
<p class="warning">This report detects candidates only. Do not delete assets automatically; review ownership, albums, favorites, sharing, and metadata first.</p>
<ul>
  <li>Exact checksum groups: {len(exact_groups)} ({html.escape(bytes_human(exact_total))} potentially redundant)</li>
  <li>Visual groups: {len(visual_groups)} at distance ≤ {args.max_distance:.6f} ({html.escape(bytes_human(visual_total))} potentially redundant)</li>
  <li>Nearest-neighbor candidates per source: {args.candidate_limit}</li>
</ul>
<p>See <a href="assets.csv">assets.csv</a>, <a href="visual_pairs.csv">visual_pairs.csv</a>, and <a href="summary.json">summary.json</a> for machine-readable output.</p>
<h2>Exact checksum matches</h2>
{''.join(exact_sections) or '<p>None found.</p>'}
<h2>Visual matches</h2>
{''.join(visual_sections) or '<p>None found.</p>'}
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def connect(args: argparse.Namespace) -> Any:
    try:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
    except ImportError as error:
        raise RuntimeError(
            "psycopg is required. Install dependencies with: "
            "python3 -m pip install -r requirements.txt"
        ) from error

    connection_kwargs: dict[str, Any] = {"row_factory": dict_row}
    if args.database_url:
        connection_kwargs["conninfo"] = args.database_url
    else:
        for key, value in (
            ("host", args.host),
            ("port", args.port),
            ("dbname", args.database),
            ("user", args.user),
            ("password", args.password),
        ):
            if value:
                connection_kwargs[key] = value

    connection = psycopg.connect(**connection_kwargs)
    connection.autocommit = False
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(args.schema)))
        if args.statement_timeout_ms > 0:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", (str(args.statement_timeout_ms),))
    return connection


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("IMMICH_DATABASE_URL"),
        help="PostgreSQL connection URL; defaults to IMMICH_DATABASE_URL.",
    )
    parser.add_argument("--host", default=os.environ.get("PGHOST"))
    parser.add_argument("--port", default=os.environ.get("PGPORT"))
    parser.add_argument("--database", default=os.environ.get("PGDATABASE", "immich"))
    parser.add_argument("--user", default=os.environ.get("PGUSER"))
    parser.add_argument("--password", default=os.environ.get("PGPASSWORD"))
    parser.add_argument("--schema", default="public")
    parser.add_argument("--max-distance", type=float, default=DEFAULT_MAX_DISTANCE)
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATE_LIMIT)
    parser.add_argument("--limit-groups", type=int, default=DEFAULT_GROUP_LIMIT)
    parser.add_argument(
        "--source-limit",
        type=int,
        default=0,
        help="Limit source assets for a trial run; 0 scans all assets with embeddings.",
    )
    parser.add_argument(
        "--type",
        dest="asset_type",
        action="append",
        choices=("IMAGE", "VIDEO"),
        help="Asset type to scan; repeat for both. Defaults to IMAGE.",
    )
    parser.add_argument("--owner-id", action="append", default=[])
    parser.add_argument("--exclude-owner-id", action="append", default=[])
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--include-exact-in-visual", action="store_true")
    parser.add_argument("--statement-timeout-ms", type=int, default=0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("immich-cross-user-duplicates-report"),
    )
    args = parser.parse_args(argv)
    if args.asset_type is None:
        args.asset_type = ["IMAGE"]
    if not 0 < args.max_distance <= 1:
        parser.error("--max-distance must be greater than 0 and no greater than 1")
    if args.candidate_limit < 1:
        parser.error("--candidate-limit must be at least 1")
    if args.limit_groups < 0 or args.source_limit < 0 or args.statement_timeout_ms < 0:
        parser.error("limits cannot be negative")
    return args


def run(args: argparse.Namespace) -> Path:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    connection = connect(args)
    try:
        with connection.cursor() as cursor:
            exact_assets = fetch_exact_assets(cursor, args)
            visual_assets, visual_edges = fetch_visual_edges(cursor, args)
        exact_groups = build_exact_groups(exact_assets, args.limit_groups)
        visual_groups = build_visual_groups(visual_assets, visual_edges, args.limit_groups)
    finally:
        connection.rollback()
        connection.close()

    rows = output_rows(exact_groups, visual_groups)
    write_csv(args.output_dir / "assets.csv", rows)
    write_pair_csv(args.output_dir / "visual_pairs.csv", visual_groups)
    write_json(args.output_dir / "summary.json", exact_groups, visual_groups, args)
    write_html(args.output_dir / "report.html", exact_groups, visual_groups, args)
    return args.output_dir / "report.html"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report_path = run(args)
    except KeyboardInterrupt:
        print("Interrupted; no Immich records were changed.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Wrote read-only report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
