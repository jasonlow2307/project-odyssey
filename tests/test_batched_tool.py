import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "immich_cross_user_duplicates_batched.py"
SPEC = importlib.util.spec_from_file_location("immich_cross_user_duplicates_batched", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def asset(asset_id, owner_id, checksum=None):
    return {
        "asset_id": asset_id,
        "owner_id": owner_id,
        "owner_name": owner_id,
        "filename": f"{asset_id}.jpg",
        "original_path": f"/photos/{asset_id}.jpg",
        "width": 100,
        "height": 100,
        "file_size_bytes": 100,
        "local_date_time": "2026-01-01T00:00:00+00:00",
        "checksum_sha1": checksum,
        "asset_type": "IMAGE",
    }


class BatchedDuplicateReportTests(unittest.TestCase):
    def test_all_parser_does_not_duplicate_shared_options(self):
        args = MODULE.parse_args(["all", "--skip-exact", "--source-limit", "10"])

        self.assertEqual(args.command, "all")
        self.assertEqual(args.source_limit, 10)
        self.assertTrue(args.skip_exact)

    def test_exact_groups_require_multiple_owners(self):
        groups = MODULE.exact_groups(
            [asset("a", "user-a", "same"), asset("b", "user-b", "same"), asset("c", "user-a", "one")],
            20,
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["checksum_sha1"], "same")

    def test_union_find_merges_components(self):
        union_find = MODULE.UnionFind(3)
        union_find.union(0, 1)
        union_find.union(1, 2)

        self.assertEqual(union_find.find(0), union_find.find(2))


if __name__ == "__main__":
    unittest.main()
