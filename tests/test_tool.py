import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "immich_cross_user_duplicates.py"
SPEC = importlib.util.spec_from_file_location("immich_cross_user_duplicates", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def make_asset(asset_id, owner_id, owner_name, checksum=None, size=100):
    return MODULE.Asset(
        asset_id=asset_id,
        owner_id=owner_id,
        owner_name=owner_name,
        filename=f"{asset_id}.jpg",
        original_path=f"/photos/{asset_id}.jpg",
        width=100,
        height=100,
        file_size_bytes=size,
        local_date_time="2026-01-01T00:00:00+00:00",
        checksum_sha1=checksum,
        asset_type="IMAGE",
    )


class DuplicateReportTests(unittest.TestCase):
    def test_visual_query_parameter_order_includes_source_limit_and_distance(self):
        class RecordingCursor:
            def __init__(self):
                self.query = ""
                self.params = []

            def execute(self, query, params):
                self.query = query
                self.params = params

            def fetchall(self):
                return []

        args = MODULE.parse_args(
            [
                "--source-limit",
                "7",
                "--owner-id",
                "00000000-0000-0000-0000-000000000001",
                "--max-distance",
                "0.02",
            ]
        )
        cursor = RecordingCursor()

        MODULE.fetch_visual_edges(cursor, args)

        self.assertIn('LIMIT %s', cursor.query)
        self.assertEqual(cursor.params[-2:], [args.candidate_limit, args.max_distance])
        self.assertIn(7, cursor.params)

    def test_exact_groups_require_multiple_owners(self):
        assets = [
            make_asset("a", "owner-1", "User Z", "same"),
            make_asset("b", "owner-2", "User A", "same"),
            make_asset("c", "owner-1", "User Z", "only-user-z"),
        ]

        groups = MODULE.build_exact_groups(assets, 20)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].checksum_sha1, "same")
        self.assertEqual([asset.asset_id for asset in groups[0].assets], ["b", "a"])


    def test_visual_groups_merge_edges_into_connected_components(self):
        assets = {
            "a": make_asset("a", "owner-1", "User A"),
            "b": make_asset("b", "owner-2", "User B"),
            "c": make_asset("c", "owner-3", "User C"),
        }
        edges = [
            MODULE.VisualEdge("a", "b", 0.004),
            MODULE.VisualEdge("b", "c", 0.006),
        ]

        groups = MODULE.build_visual_groups(assets, edges, 20)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].min_distance, 0.004)
        self.assertEqual({asset.asset_id for asset in groups[0].assets}, {"a", "b", "c"})


    def test_asset_from_row_serializes_datetime_and_missing_values(self):
        asset = MODULE.asset_from_row(
            {
                "asset_id": "a",
                "owner_id": "owner-1",
                "owner_name": None,
                "filename": None,
                "original_path": None,
                "width": None,
                "height": 200,
                "file_size_bytes": None,
                "local_date_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "checksum_sha1": None,
                "asset_type": "IMAGE",
            }
        )

        self.assertEqual(asset.owner_name, "owner-1")
        self.assertEqual(asset.filename, "")
        self.assertEqual(asset.local_date_time, "2026-01-01T00:00:00+00:00")
        self.assertEqual(asset.height, 200)


    def test_bytes_human(self):
        self.assertEqual(MODULE.bytes_human(None), "—")
        self.assertEqual(MODULE.bytes_human(1024 * 1024), "1.0 MB")


if __name__ == "__main__":
    unittest.main()
