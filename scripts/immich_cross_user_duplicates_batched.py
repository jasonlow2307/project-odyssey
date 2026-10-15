#!/usr/bin/env python3
"""Resumable cross-user duplicate reporting for an Immich PostgreSQL database.

The export stage reads Immich's existing CLIP vectors once in small batches.
The index and report stages run locally, so no large vector-search statement is
held open in PostgreSQL and an interrupted run can resume from its checkpoint.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_BATCH_SIZE = 2_000
DEFAULT_MAX_DISTANCE = 0.01
DEFAULT_CANDIDATE_POOL = 256
DEFAULT_GROUP_LIMIT = 20
DEFAULT_STATEMENT_TIMEOUT_MS = 600_000

MANIFEST_NAME = "export-manifest.json"
EMBEDDINGS_NAME = "embeddings.npy"
ASSETS_NAME = "assets.jsonl"
INDEX_NAME = "embeddings.hnsw"
INDEX_MANIFEST_NAME = "index-manifest.json"
EDGES_NAME = "visual-edges.csv"
REPORT_PROGRESS_NAME = "report-progress.json"


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def import_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as error:
        raise RuntimeError(
            "numpy is required. Install dependencies with: "
            "python3 -m pip install -r requirements-batched.txt"
        ) from error
    return np


def import_hnswlib() -> Any:
    try:
        import hnswlib
    except ImportError as error:
        raise RuntimeError(
            "hnswlib is required. Install dependencies with: "
            "python3 -m pip install -r requirements-batched.txt"
        ) from error
    return hnswlib


def connect(args: argparse.Namespace) -> Any:
    try:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
    except ImportError as error:
        raise RuntimeError(
            "psycopg is required. Install dependencies with: "
            "python3 -m pip install -r requirements-batched.txt"
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
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(args.statement_timeout_ms),),
            )
    return connection


def build_asset_filters(alias: str, args: argparse.Namespace) -> tuple[list[str], list[Any]]:
    clauses = [
        f'{alias}."deletedAt" IS NULL',
        f'{alias}."stackId" IS NULL',
    ]
    params: list[Any] = []
    if not args.include_hidden:
        clauses.append(f'{alias}."visibility" IN (\'timeline\', \'archive\')')
    if args.asset_type:
        clauses.append(f'{alias}."type" = ANY(%s::text[])')
        params.append(args.asset_type)
    if args.owner_id:
        clauses.append(f'{alias}."ownerId" = ANY(%s::uuid[])')
        params.append(args.owner_id)
    if args.exclude_owner_id:
        clauses.append(f'{alias}."ownerId" <> ALL(%s::uuid[])')
        params.append(args.exclude_owner_id)
    return clauses, params


def metadata_projection() -> str:
    return """
        a."id"::text AS asset_id,
        a."ownerId"::text AS owner_id,
        COALESCE(u."name", a."ownerId"::text) AS owner_name,
        a."originalFileName" AS filename,
        a."originalPath" AS original_path,
        COALESCE(a."width", e."exifImageWidth") AS width,
        COALESCE(a."height", e."exifImageHeight") AS height,
        e."fileSizeInByte" AS file_size_bytes,
        a."localDateTime" AS local_date_time,
        encode(a."checksum", 'hex') AS checksum_sha1,
        a."type" AS asset_type,
        ss."embedding"::text AS embedding_text
    """


def serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def row_to_asset(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": str(row["asset_id"]),
        "owner_id": str(row["owner_id"]),
        "owner_name": str(row.get("owner_name") or row["owner_id"]),
        "filename": str(row.get("filename") or ""),
        "original_path": str(row.get("original_path") or ""),
        "width": _as_int(row.get("width")),
        "height": _as_int(row.get("height")),
        "file_size_bytes": _as_int(row.get("file_size_bytes")),
        "local_date_time": serialize_value(row.get("local_date_time")),
        "checksum_sha1": row.get("checksum_sha1"),
        "asset_type": str(row.get("asset_type") or ""),
    }


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def parse_embedding(value: Any, np: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return np.fromstring(text, sep=",", dtype=np.float32)
    return np.asarray(value, dtype=np.float32)


def state_paths(state_dir: Path) -> dict[str, Path]:
    return {
        "manifest": state_dir / MANIFEST_NAME,
        "embeddings": state_dir / EMBEDDINGS_NAME,
        "assets": state_dir / ASSETS_NAME,
        "index": state_dir / INDEX_NAME,
        "index_manifest": state_dir / INDEX_MANIFEST_NAME,
        "edges": state_dir / EDGES_NAME,
        "report_progress": state_dir / REPORT_PROGRESS_NAME,
    }


def invalidate_derived_state(paths: Mapping[str, Path]) -> None:
    for name in ("index", "index_manifest", "edges", "report_progress"):
        paths[name].unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def query_export_count(args: argparse.Namespace) -> tuple[int, int, int]:
    filters, params = build_asset_filters("a", args)
    filter_sql = " AND ".join(filters)
    connection = connect(args)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS asset_count
                FROM "asset" AS a
                WHERE {filter_sql}
                """,
                params,
            )
            count = int(cursor.fetchone()["asset_count"])
            cursor.execute(
                f"""
                SELECT COUNT(*) AS embedding_count, vector_dims(ss."embedding") AS dimension
                FROM "asset" AS a
                INNER JOIN "smart_search" AS ss ON ss."assetId" = a."id"
                WHERE {filter_sql}
                GROUP BY vector_dims(ss."embedding")
                ORDER BY embedding_count DESC
                LIMIT 1
                """,
                params,
            )
            row = cursor.fetchone()
            if row is None:
                return count, 0, 0
            return count, int(row["embedding_count"]), int(row["dimension"])
    finally:
        connection.rollback()
        connection.close()


def fetch_export_batch(
    args: argparse.Namespace,
    last_asset_id: str | None,
) -> list[dict[str, Any]]:
    filters, params = build_asset_filters("a", args)
    filter_sql = " AND ".join(filters)
    if last_asset_id:
        filters.append('a."id" > %s::uuid')
        params.append(last_asset_id)
        filter_sql = " AND ".join(filters)
    params.append(args.batch_size)

    connection = connect(args)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {metadata_projection()}
                FROM "asset" AS a
                LEFT JOIN "smart_search" AS ss ON ss."assetId" = a."id"
                LEFT JOIN "user" AS u ON u."id" = a."ownerId"
                LEFT JOIN "asset_exif" AS e ON e."assetId" = a."id"
                WHERE {filter_sql}
                ORDER BY a."id"
                LIMIT %s
                """,
                params,
            )
            return list(cursor.fetchall())
    finally:
        connection.rollback()
        connection.close()


def export_embeddings(args: argparse.Namespace) -> None:
    np = import_numpy()
    paths = state_paths(args.state_dir)
    args.state_dir.mkdir(parents=True, exist_ok=True)

    if paths["manifest"].exists() and not args.restart_export:
        manifest = read_json(paths["manifest"])
        expected_filters = {
            "asset_type": args.asset_type,
            "include_hidden": args.include_hidden,
            "owner_id": args.owner_id,
            "exclude_owner_id": args.exclude_owner_id,
        }
        if manifest.get("filters") != expected_filters:
            raise RuntimeError(
                "Export filters differ from the existing checkpoint; use --restart-export "
                "or resume with the original filters"
            )
        if manifest.get("status") == "complete":
            print(f"Export already complete: {manifest['rows_exported']} assets")
            return
        count = int(manifest["asset_count"])
        embedding_count = int(manifest["embedding_count"])
        dimension = int(manifest["dimension"])
        rows_exported = int(manifest["rows_exported"])
        embedding_rows_exported = int(manifest["embedding_rows_exported"])
        last_asset_id = manifest.get("last_asset_id")
        metadata_offset = int(manifest["metadata_offset"])
        embeddings = np.lib.format.open_memmap(
            paths["embeddings"], mode="r+", dtype=np.float32, shape=(embedding_count, dimension)
        )
        metadata_handle = paths["assets"].open("r+b")
        metadata_handle.truncate(metadata_offset)
        metadata_handle.seek(metadata_offset)
        print(f"Resuming export at {rows_exported}/{count} assets ({embedding_rows_exported}/{embedding_count} embeddings)")
    else:
        if args.restart_export:
            invalidate_derived_state(paths)
            for path in (paths["manifest"], paths["embeddings"], paths["assets"]):
                path.unlink(missing_ok=True)
        print("Counting eligible assets and reading embedding dimensions...")
        count, embedding_count, dimension = query_export_count(args)
        if count == 0:
            raise RuntimeError("No eligible assets were found")
        if embedding_count == 0:
            raise RuntimeError("No eligible assets with smart_search embeddings were found")
        embeddings = np.lib.format.open_memmap(
            paths["embeddings"], mode="w+", dtype=np.float32, shape=(embedding_count, dimension)
        )
        metadata_handle = paths["assets"].open("wb")
        rows_exported = 0
        embedding_rows_exported = 0
        last_asset_id = None
        metadata_offset = 0
        manifest = {
            "version": 1,
            "status": "in_progress",
            "asset_count": count,
            "embedding_count": embedding_count,
            "dimension": dimension,
            "rows_exported": 0,
            "embedding_rows_exported": 0,
            "last_asset_id": None,
            "metadata_offset": 0,
            "filters": {
                "asset_type": args.asset_type,
                "include_hidden": args.include_hidden,
                "owner_id": args.owner_id,
                "exclude_owner_id": args.exclude_owner_id,
            },
        }
        atomic_write_json(paths["manifest"], manifest)

    try:
        while rows_exported < count:
            batch_started = time.monotonic()
            rows = fetch_export_batch(args, last_asset_id)
            if not rows:
                raise RuntimeError(
                    f"Export ended after {rows_exported} rows but expected {count}; "
                    "the database contents may have changed during export"
                )
            batch_assets: list[dict[str, Any]] = []
            batch_embeddings: list[Any] = []
            for row in rows:
                asset = row_to_asset(row)
                embedding_text = row.get("embedding_text")
                if embedding_text is not None:
                    embedding = parse_embedding(embedding_text, np)
                    if embedding.size != dimension:
                        raise RuntimeError(
                            f"Asset {row['asset_id']} has dimension {embedding.size}; expected {dimension}"
                        )
                    asset["visual_index"] = embedding_rows_exported + len(batch_embeddings)
                    batch_embeddings.append(embedding)
                else:
                    asset["visual_index"] = None
                batch_assets.append(asset)

            if batch_embeddings:
                embeddings[
                    embedding_rows_exported : embedding_rows_exported + len(batch_embeddings)
                ] = np.asarray(batch_embeddings, dtype=np.float32)
            metadata_handle.write(
                "".join(json.dumps(asset, ensure_ascii=False) + "\n" for asset in batch_assets).encode(
                    "utf-8"
                )
            )
            embeddings.flush()
            metadata_handle.flush()
            os.fsync(metadata_handle.fileno())
            rows_exported += len(rows)
            embedding_rows_exported += len(batch_embeddings)
            last_asset_id = batch_assets[-1]["asset_id"]
            metadata_offset = metadata_handle.tell()
            manifest.update(
                {
                    "rows_exported": rows_exported,
                    "embedding_rows_exported": embedding_rows_exported,
                    "last_asset_id": last_asset_id,
                    "metadata_offset": metadata_offset,
                }
            )
            atomic_write_json(paths["manifest"], manifest)
            elapsed = time.monotonic() - batch_started
            print(
                f"Exported {rows_exported}/{count} assets "
                f"({rows_exported / count:.1%}; {embedding_rows_exported}/{embedding_count} embeddings, "
                f"{elapsed:.1f}s batch)",
                flush=True,
            )
    finally:
        metadata_handle.close()
        del embeddings

    manifest["status"] = "complete"
    atomic_write_json(paths["manifest"], manifest)
    print(
        f"Export complete: {rows_exported} assets, {embedding_rows_exported} "
        f"{dimension}-dimensional embeddings"
    )


def load_manifest(args: argparse.Namespace) -> dict[str, Any]:
    paths = state_paths(args.state_dir)
    if not paths["manifest"].exists():
        raise RuntimeError("Export state not found; run the export stage first")
    manifest = read_json(paths["manifest"])
    if manifest.get("status") != "complete":
        raise RuntimeError("Export is incomplete; resume the export stage first")
    return manifest


def build_index(args: argparse.Namespace) -> None:
    np = import_numpy()
    hnswlib = import_hnswlib()
    manifest = load_manifest(args)
    paths = state_paths(args.state_dir)
    index_manifest = paths["index_manifest"]
    if index_manifest.exists() and paths["index"].exists() and not args.rebuild_index:
        existing = read_json(index_manifest)
        if (
            existing.get("rows") == manifest["embedding_rows_exported"]
            and existing.get("dimension") == manifest["dimension"]
        ):
            print("Index already complete; use --rebuild-index to rebuild it")
            return

    embeddings = np.load(paths["embeddings"], mmap_mode="r")
    rows = int(manifest["embedding_rows_exported"])
    dimension = int(manifest["dimension"])
    index = hnswlib.Index(space="cosine", dim=dimension)
    index.init_index(
        max_elements=rows,
        ef_construction=args.ef_construction,
        M=args.hnsw_m,
    )
    index.set_num_threads(args.index_threads)
    for start in range(0, rows, args.batch_size):
        end = min(rows, start + args.batch_size)
        labels = np.arange(start, end, dtype=np.int32)
        index.add_items(embeddings[start:end], labels)
        print(f"Indexed {end}/{rows} assets ({end / rows:.1%})", flush=True)
    index.set_ef(max(args.candidate_pool, 100))
    temporary = paths["index"].with_suffix(".hnsw.tmp")
    temporary.unlink(missing_ok=True)
    index.save_index(str(temporary))
    os.replace(temporary, paths["index"])
    atomic_write_json(
        index_manifest,
        {
            "version": 1,
            "status": "complete",
            "rows": rows,
            "dimension": dimension,
            "space": "cosine",
            "hnsw_m": args.hnsw_m,
            "ef_construction": args.ef_construction,
        },
    )
    print(f"Index complete: {rows} assets")


def load_assets(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def asset_sort_key(asset: Mapping[str, Any]) -> tuple[str, str, str]:
    return str(asset["owner_name"]).lower(), str(asset["filename"]).lower(), str(asset["asset_id"])


def exact_groups(assets: Sequence[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for asset in assets:
        checksum = asset.get("checksum_sha1")
        if checksum:
            grouped[str(checksum)].append(dict(asset))
    groups = [
        {"group_id": index, "checksum_sha1": checksum, "assets": sorted(group, key=asset_sort_key)}
        for index, (checksum, group) in enumerate(sorted(grouped.items()), start=1)
        if len({asset["owner_id"] for asset in group}) > 1
    ]
    return groups[:limit] if limit > 0 else groups


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def visual_groups(
    assets: Sequence[Mapping[str, Any]],
    edge_path: Path,
    limit: int,
) -> list[dict[str, Any]]:
    union_find = UnionFind(len(assets))
    with edge_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            union_find.union(int(row["source_index"]), int(row["candidate_index"]))

    components: defaultdict[int, list[int]] = defaultdict(list)
    for index in range(len(assets)):
        components[union_find.find(index)].append(index)

    eligible: dict[int, dict[str, Any]] = {}
    for root, indexes in components.items():
        owners = {assets[index]["owner_id"] for index in indexes}
        if len(owners) > 1:
            eligible[root] = {"indexes": indexes, "min_distance": 1.0, "edges": []}

    with edge_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            root = union_find.find(int(row["source_index"]))
            if root in eligible:
                distance = float(row["distance"])
                eligible[root]["min_distance"] = min(eligible[root]["min_distance"], distance)

    selected_roots = [
        root
        for root, value in sorted(
            eligible.items(), key=lambda item: (item[1]["min_distance"], len(item[1]["indexes"]))
        )[:limit]
    ]
    selected = set(selected_roots)
    for root in selected_roots:
        eligible[root]["edges"] = []
    with edge_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            root = union_find.find(int(row["source_index"]))
            if root in selected:
                eligible[root]["edges"].append(
                    {
                        "left_id": assets[int(row["source_index"])]["asset_id"],
                        "right_id": assets[int(row["candidate_index"])]["asset_id"],
                        "distance": float(row["distance"]),
                    }
                )

    groups: list[dict[str, Any]] = []
    for group_id, root in enumerate(selected_roots, start=1):
        value = eligible[root]
        groups.append(
            {
                "group_id": group_id,
                "min_distance": value["min_distance"],
                "assets": sorted((dict(assets[index]) for index in value["indexes"]), key=asset_sort_key),
                "edges": value["edges"],
            }
        )
    return groups


def report(args: argparse.Namespace) -> None:
    np = import_numpy()
    hnswlib = import_hnswlib()
    manifest = load_manifest(args)
    paths = state_paths(args.state_dir)
    if not paths["index"].exists():
        raise RuntimeError("HNSW index not found; run the index stage first")
    assets = load_assets(paths["assets"])
    if len(assets) != int(manifest["rows_exported"]):
        raise RuntimeError("Asset metadata and embedding files do not have the same row count")
    visual_assets = [asset for asset in assets if asset.get("visual_index") is not None]
    if len(visual_assets) != int(manifest["embedding_rows_exported"]):
        raise RuntimeError("Visual asset metadata and embedding files do not have the same row count")
    embeddings = np.load(paths["embeddings"], mmap_mode="r")
    index = hnswlib.Index(space="cosine", dim=int(manifest["dimension"]))
    index.load_index(str(paths["index"]), max_elements=len(visual_assets))
    index.set_ef(max(args.candidate_pool, 100))

    requested_sources = (
        min(args.source_limit, len(visual_assets)) if args.source_limit > 0 else len(visual_assets)
    )
    progress = {
        "version": 1,
        "next_source": 0,
        "source_limit": requested_sources,
        "max_distance": args.max_distance,
        "candidate_pool": args.candidate_pool,
        "include_exact_in_visual": args.include_exact_in_visual,
    }
    if paths["report_progress"].exists() and not args.restart_report:
        previous = read_json(paths["report_progress"])
        if all(previous.get(key) == progress[key] for key in progress if key != "next_source"):
            progress.update(previous)
            print(f"Resuming visual report at {progress['next_source']}/{requested_sources} sources")
        else:
            raise RuntimeError(
                "Existing report settings differ; use --restart-report or choose another --state-dir"
            )

    if args.restart_report or not paths["edges"].exists():
        paths["edges"].write_text("source_index,candidate_index,distance\n", encoding="utf-8")
        progress["next_source"] = 0
    else:
        edge_offset = int(progress.get("edge_offset", 0))
        with paths["edges"].open("r+b") as handle:
            handle.truncate(edge_offset)

    with paths["edges"].open("a", newline="", encoding="utf-8") as edge_handle:
        writer = csv.writer(edge_handle)
        query_k = min(max(args.candidate_pool, 1), len(visual_assets))
        for start in range(int(progress["next_source"]), requested_sources, args.batch_size):
            end = min(requested_sources, start + args.batch_size)
            labels, distances = index.knn_query(embeddings[start:end], k=query_k, num_threads=args.index_threads)
            batch_edges: dict[tuple[int, int], float] = {}
            for offset, source_index in enumerate(range(start, end)):
                source = visual_assets[source_index]
                for candidate_index, distance in zip(labels[offset], distances[offset]):
                    candidate_index = int(candidate_index)
                    distance = float(distance)
                    if candidate_index < 0 or candidate_index == source_index:
                        continue
                    candidate = visual_assets[candidate_index]
                    if candidate["owner_id"] == source["owner_id"]:
                        continue
                    if candidate["asset_type"] != source["asset_type"]:
                        continue
                    if not args.include_exact_in_visual and source.get("checksum_sha1"):
                        if source["checksum_sha1"] == candidate.get("checksum_sha1"):
                            continue
                    if distance > args.max_distance:
                        continue
                    left, right = sorted((source_index, candidate_index))
                    key = (left, right)
                    batch_edges[key] = min(distance, batch_edges.get(key, distance))
            for (source_index, candidate_index), distance in sorted(batch_edges.items()):
                writer.writerow((source_index, candidate_index, f"{distance:.8f}"))
            edge_handle.flush()
            os.fsync(edge_handle.fileno())
            progress["next_source"] = end
            progress["edge_offset"] = edge_handle.tell()
            atomic_write_json(paths["report_progress"], progress)
            print(f"Searched {end}/{requested_sources} sources ({end / requested_sources:.1%})", flush=True)

    exact = exact_groups(assets, args.limit_groups) if not args.skip_exact else []
    visual = visual_groups(visual_assets, paths["edges"], args.limit_groups)
    write_report(args, exact, visual)
    print(f"Report written to {args.output_dir / 'report.html'}")


def bytes_human(value: int | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def redundant_bytes(assets: Sequence[Mapping[str, Any]]) -> int:
    sizes = [asset.get("file_size_bytes") for asset in assets if asset.get("file_size_bytes") is not None]
    return max(0, sum(sizes) - max(sizes, default=0))


def html_asset(asset: Mapping[str, Any]) -> str:
    dimensions = "—"
    if asset.get("width") or asset.get("height"):
        dimensions = f"{asset.get('width') or '?'}×{asset.get('height') or '?'}"
    return """<tr>
        <td>{owner}</td><td>{filename}</td><td>{dimensions}</td><td>{size}</td>
        <td>{date}</td><td><code>{checksum}</code></td><td><code>{asset_id}</code></td>
    </tr>""".format(
        owner=html.escape(f"{asset['owner_name']} ({asset['owner_id']})"),
        filename=html.escape(asset.get("filename", "")),
        dimensions=html.escape(dimensions),
        size=html.escape(bytes_human(asset.get("file_size_bytes"))),
        date=html.escape(asset.get("local_date_time") or "—"),
        checksum=html.escape(asset.get("checksum_sha1") or "—"),
        asset_id=html.escape(asset["asset_id"]),
    )


def html_table(assets: Sequence[Mapping[str, Any]]) -> str:
    return f"""<table><thead><tr>
        <th>Owner</th><th>Filename</th><th>Resolution</th><th>Size</th>
        <th>Captured</th><th>SHA-1</th><th>Asset ID</th>
    </tr></thead><tbody>{''.join(html_asset(asset) for asset in assets)}</tbody></table>"""


def write_report(
    args: argparse.Namespace,
    exact: Sequence[Mapping[str, Any]],
    visual: Sequence[Mapping[str, Any]],
) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for group in exact:
        for asset in group["assets"]:
            rows.append({"kind": "exact", "group_id": group["group_id"], "distance": "", **asset})
    for group in visual:
        for asset in group["assets"]:
            rows.append(
                {
                    "kind": "visual",
                    "group_id": group["group_id"],
                    "distance": f"{group['min_distance']:.8f}",
                    **asset,
                }
            )
    fields = [
        "kind", "group_id", "distance", "asset_id", "owner_id", "owner_name", "filename",
        "original_path", "width", "height", "file_size_bytes", "local_date_time", "checksum_sha1", "asset_type",
    ]
    with (args.output_dir / "assets.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with (args.output_dir / "visual_pairs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["group_id", "left_asset_id", "right_asset_id", "distance"])
        writer.writeheader()
        for group in visual:
            for edge in group["edges"]:
                writer.writerow({"group_id": group["group_id"], **edge})
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "read_only": True,
        "architecture": "streamed export + local hnswlib index",
        "settings": {
            "max_distance": args.max_distance,
            "candidate_pool": args.candidate_pool,
            "source_limit": args.source_limit or None,
            "include_exact_in_visual": args.include_exact_in_visual,
        },
        "exact_groups": exact,
        "visual_groups": visual,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    exact_html = []
    for group in exact:
        exact_html.append(
            f"<article><h3>Exact group {group['group_id']}</h3>"
            f"<p>SHA-1 <code>{html.escape(group['checksum_sha1'])}</code>; {len(group['assets'])} assets; "
            f"approximately {html.escape(bytes_human(redundant_bytes(group['assets'])))} redundant by file size.</p>"
            f"{html_table(group['assets'])}</article>"
        )
    visual_html = []
    for group in visual:
        visual_html.append(
            f"<article><h3>Visual group {group['group_id']}</h3>"
            f"<p>Minimum cosine distance: <code>{group['min_distance']:.8f}</code>; {len(group['assets'])} assets; "
            f"approximately {html.escape(bytes_human(redundant_bytes(group['assets'])))} redundant by file size.</p>"
            f"{html_table(group['assets'])}</article>"
        )
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Immich duplicate report</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;color:#202124}}article{{border:1px solid #dadce0;border-radius:.5rem;margin:1rem 0;padding:1rem;overflow-x:auto}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #eee;padding:.45rem;text-align:left;vertical-align:top}}th{{background:#f8f9fa}}code{{overflow-wrap:anywhere}}.warning{{background:#fff4ce;border-left:.25rem solid #f9ab00;padding:.75rem 1rem}}</style>
</head><body><h1>Immich cross-user duplicate report</h1>
<p>Generated {html.escape(datetime.now().astimezone().isoformat())}. PostgreSQL was read-only; embeddings were indexed locally.</p>
<p class="warning">Detection only. Review ownership, albums, favorites, sharing, and metadata before deleting anything.</p>
<ul><li>Exact checksum groups: {len(exact)}</li><li>Visual groups: {len(visual)} at distance ≤ {args.max_distance:.8f}</li><li>Candidate pool per source: {args.candidate_pool}</li></ul>
<p><a href="assets.csv">assets.csv</a> · <a href="visual_pairs.csv">visual_pairs.csv</a> · <a href="summary.json">summary.json</a></p>
<h2>Exact checksum matches</h2>{''.join(exact_html) or '<p>None found.</p>'}
<h2>Visual matches</h2>{''.join(visual_html) or '<p>None found.</p>'}</body></html>"""
    (args.output_dir / "report.html").write_text(document, encoding="utf-8")


def add_database_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", default=os.environ.get("IMMICH_DATABASE_URL"))
    parser.add_argument("--host", default=os.environ.get("PGHOST"))
    parser.add_argument("--port", default=os.environ.get("PGPORT"))
    parser.add_argument("--database", default=os.environ.get("PGDATABASE", "immich"))
    parser.add_argument("--user", default=os.environ.get("PGUSER"))
    parser.add_argument("--password", default=os.environ.get("PGPASSWORD"))
    parser.add_argument("--schema", default="public")
    parser.add_argument("--statement-timeout-ms", type=int, default=DEFAULT_STATEMENT_TIMEOUT_MS)
    parser.add_argument("--type", dest="asset_type", action="append", choices=("IMAGE", "VIDEO"))
    parser.add_argument("--owner-id", action="append", default=[])
    parser.add_argument("--exclude-owner-id", action="append", default=[])
    parser.add_argument("--include-hidden", action="store_true")


def add_export_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", type=Path, default=Path("immich-duplicate-state"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--restart-export", action="store_true")


def add_index_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", type=Path, default=Path("immich-duplicate-state"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--candidate-pool", type=int, default=DEFAULT_CANDIDATE_POOL)
    parser.add_argument("--hnsw-m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=200)
    parser.add_argument("--index-threads", type=int, default=max(1, min(os.cpu_count() or 2, 8)))
    parser.add_argument("--rebuild-index", action="store_true")


def add_report_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", type=Path, default=Path("immich-duplicate-state"))
    parser.add_argument("--output-dir", type=Path, default=Path("immich-duplicate-report"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--candidate-pool", type=int, default=DEFAULT_CANDIDATE_POOL)
    parser.add_argument("--max-distance", type=float, default=DEFAULT_MAX_DISTANCE)
    parser.add_argument("--limit-groups", type=int, default=DEFAULT_GROUP_LIMIT)
    parser.add_argument("--source-limit", type=int, default=0)
    parser.add_argument("--index-threads", type=int, default=max(1, min(os.cpu_count() or 2, 8)))
    parser.add_argument("--include-exact-in-visual", action="store_true")
    parser.add_argument("--skip-exact", action="store_true")
    parser.add_argument("--restart-report", action="store_true")


def add_all_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", type=Path, default=Path("immich-duplicate-state"))
    parser.add_argument("--output-dir", type=Path, default=Path("immich-duplicate-report"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--candidate-pool", type=int, default=DEFAULT_CANDIDATE_POOL)
    parser.add_argument("--max-distance", type=float, default=DEFAULT_MAX_DISTANCE)
    parser.add_argument("--limit-groups", type=int, default=DEFAULT_GROUP_LIMIT)
    parser.add_argument("--source-limit", type=int, default=0)
    parser.add_argument("--hnsw-m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=200)
    parser.add_argument("--index-threads", type=int, default=max(1, min(os.cpu_count() or 2, 8)))
    parser.add_argument("--restart-export", action="store_true")
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--include-exact-in-visual", action="store_true")
    parser.add_argument("--skip-exact", action="store_true")
    parser.add_argument("--restart-report", action="store_true")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="stream embeddings and metadata into local state")
    add_database_options(export_parser)
    add_export_options(export_parser)

    index_parser = subparsers.add_parser("index", help="build the local HNSW index")
    add_index_options(index_parser)

    report_parser = subparsers.add_parser("report", help="search locally and write the report")
    add_report_options(report_parser)

    all_parser = subparsers.add_parser("all", help="run export, index, and report in sequence")
    add_database_options(all_parser)
    add_all_options(all_parser)

    args = parser.parse_args(argv)
    if getattr(args, "asset_type", None) is None:
        args.asset_type = ["IMAGE"]
    if args.command in {"report", "all"}:
        if not 0 < args.max_distance <= 1:
            parser.error("--max-distance must be greater than 0 and no greater than 1")
        if args.candidate_pool < 1:
            parser.error("--candidate-pool must be at least 1")
        if args.limit_groups < 0 or args.source_limit < 0:
            parser.error("group and source limits cannot be negative")
    if args.command in {"export", "all"} and args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    return args


def run(args: argparse.Namespace) -> None:
    if args.command == "export":
        export_embeddings(args)
    elif args.command == "index":
        build_index(args)
    elif args.command == "report":
        report(args)
    else:
        export_embeddings(args)
        build_index(args)
        report(args)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        run(args)
    except KeyboardInterrupt:
        print("Interrupted. Resume with the same command; no Immich records were changed.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
