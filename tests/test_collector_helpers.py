#!/opt/homebrew/bin/python3.12

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
scope = runpy.run_path(
    PROJECT_ROOT / "scripts/collect-readonly-snapshot.py",
    run_name="collector_module",
)
REMOTE_COLLECTOR = scope["REMOTE_COLLECTOR"]
sanitize_qb_file_cache = scope["sanitize_qb_file_cache"]
HELPERS_SOURCE = REMOTE_COLLECTOR.split(
    'db = sqlite3.connect(f"file:{JELLYFIN_DB}?mode=ro", uri=True)',
    1,
)[0].replace("__HR_HASH_CACHE__", "{}")
HELPERS_SOURCE = HELPERS_SOURCE.replace("__QB_FILE_CACHE__", "{}")
helpers: dict[str, object] = {}
exec(compile(HELPERS_SOURCE, "<collector-helpers>", "exec"), helpers)


class CollectorHelperTests(unittest.TestCase):
    def test_remote_collector_source_compiles(self):
        source = REMOTE_COLLECTOR.replace(
            "__HR_HASH_CACHE__", "{}"
        ).replace("__QB_FILE_CACHE__", "{}")

        compile(source, "<remote-collector>", "exec")

    def test_v1_torrent_infohash_uses_raw_info_dictionary(self):
        info = b"d4:name7:Fixture6:lengthi14ee"
        torrent = b"d4:info" + info + b"e"

        self.assertEqual(
            helpers["torrent_infohash"](torrent),
            hashlib.sha1(info).hexdigest(),
        )

    def test_invalid_torrent_without_info_is_rejected(self):
        with self.assertRaises(ValueError):
            helpers["torrent_infohash"](b"d4:name7:Fixturee")

    def test_stable_resource_id_is_not_row_position_based(self):
        first = helpers["stable_resource_id"]("tv:tmdb:1399")
        second = helpers["stable_resource_id"]("tv:tmdb:1399")
        other = helpers["stable_resource_id"]("tv:tmdb:70523")

        self.assertEqual(first, second)
        self.assertRegex(first, r"^res_[0-9a-f]{20}$")
        self.assertNotEqual(first, other)

    def test_infohash_names_are_detectable_for_public_redaction(self):
        self.assertTrue(helpers["looks_like_infohash"]("a" * 40))
        self.assertTrue(helpers["looks_like_infohash"]("B" * 40))
        self.assertFalse(helpers["looks_like_infohash"]("a" * 39))
        self.assertFalse(helpers["looks_like_infohash"]("Movie Title"))

    def test_hr_candidate_assignment_reports_exactly_covered_titles(self):
        torrents = [
            {
                "hash": "a" * 40,
                "name": "Way.of.Choices.2026.S01.2160p.WEB-DL",
            }
        ]
        assignments, covered_titles = helpers["assign_hr_candidates"](
            torrents,
            {"way of choices 2026 s01", "missing release 2026"},
        )

        self.assertEqual(assignments, {"a" * 40})
        self.assertEqual(covered_titles, {"way of choices 2026 s01"})

    def test_qb_file_cache_rejects_invalid_or_empty_entries(self):
        valid_hash = "a" * 40
        cache = sanitize_qb_file_cache(
            {
                valid_hash: [
                    {"name": "Title/file.mkv", "size": 14, "progress": 1}
                ],
                "not-a-hash": [
                    {"name": "bad", "size": 1, "progress": 1}
                ],
                "b" * 40: [
                    {"name": "", "size": -1, "progress": 2}
                ],
            }
        )

        self.assertEqual(
            cache,
            {
                valid_hash: [
                    {
                        "name": "Title/file.mkv",
                        "size": 14,
                        "progress": 1.0,
                    }
                ]
            },
        )

    def test_movie_sidecars_are_included_only_in_dedicated_title_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            movies_root = Path(temporary).resolve() / "Movies"
            title_root = movies_root / "Fixture (2026)"
            title_root.mkdir(parents=True)
            movie = title_root / "Fixture.mkv"
            movie.write_bytes(b"movie")
            (title_root / "poster.jpg").write_bytes(b"poster")
            helpers["ALLOWED_ROOTS"] = (os.path.realpath(movies_root),)
            group = {
                "media_type": "movie",
                "paths": [str(movie)],
                "files": [movie],
            }

            self.assertEqual(
                helpers["library_cleanup_roots"](group),
                [title_root],
            )

            (title_root / "unrelated-trailer.mp4").write_bytes(b"other")
            self.assertEqual(
                helpers["library_cleanup_roots"](group),
                [movie],
            )

    def test_hardlink_index_finds_download_and_legacy_quarantine_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            library = root / "library"
            downloads = root / "downloads"
            legacy_quarantine = root / ".media-quarantine"
            transaction_quarantine = root / ".storage-cleanup-quarantine"
            for directory in (
                library,
                downloads,
                legacy_quarantine,
                transaction_quarantine,
            ):
                directory.mkdir()
            media = library / "Fixture.mkv"
            media.write_bytes(b"fixture")
            download_link = downloads / "Fixture.Source.mkv"
            legacy_link = legacy_quarantine / "Fixture.Legacy.mkv"
            transaction_link = transaction_quarantine / "Fixture.Pending.mkv"
            os.link(media, download_link)
            os.link(media, legacy_link)
            os.link(media, transaction_link)
            symlink = downloads / "Fixture.Symlink.mkv"
            symlink.symlink_to(media)
            helpers["HARDLINK_DISCOVERY_ROOTS"] = (
                str(library),
                str(downloads),
                str(legacy_quarantine),
            )

            index, verified = helpers["hardlink_path_index"]()
            inode = (media.stat().st_dev, media.stat().st_ino)

            self.assertTrue(verified)
            self.assertEqual(
                index[inode],
                {str(media), str(download_link), str(legacy_link)},
            )
            self.assertNotIn(str(transaction_link), index[inode])
            self.assertNotIn(str(symlink), index[inode])

    def test_unresolved_transactions_include_nonterminal_state_and_quarantine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            backup = root / "backups"
            quarantine = root / "quarantine"
            complete = backup / "plan_complete"
            uncertain = backup / "plan_uncertain"
            orphan = backup / "plan_orphan"
            complete.mkdir(parents=True)
            uncertain.mkdir()
            orphan.mkdir()
            quarantine.mkdir()
            (quarantine / "plan_quarantined").mkdir()
            (complete / "transaction.json").write_text(
                json.dumps({"phase": "complete"}),
                encoding="utf-8",
            )
            (uncertain / "transaction.json").write_text(
                json.dumps({"phase": "uncertain"}),
                encoding="utf-8",
            )
            helpers["EXECUTION_BACKUP"] = backup
            helpers["QUARANTINE_ROOTS"] = (quarantine,)

            self.assertEqual(
                helpers["unresolved_transactions"](),
                ["plan_orphan", "plan_quarantined", "plan_uncertain"],
            )


if __name__ == "__main__":
    unittest.main()
