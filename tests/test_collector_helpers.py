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
sanitize_hr_source_cache = scope["sanitize_hr_source_cache"]
HELPERS_SOURCE = REMOTE_COLLECTOR.split(
    'db = sqlite3.connect(f"file:{JELLYFIN_DB}?mode=ro", uri=True)',
    1,
)[0].replace("__HR_HASH_CACHE__", "{}")
HELPERS_SOURCE = HELPERS_SOURCE.replace("__HR_SOURCE_CACHE__", "{}")
HELPERS_SOURCE = HELPERS_SOURCE.replace("__QB_FILE_CACHE__", "{}")
HELPERS_SOURCE = HELPERS_SOURCE.replace("__TMDB_CACHE__", "{}")
HELPERS_SOURCE = HELPERS_SOURCE.replace("__TMDB_HINTS__", "[]")
helpers: dict[str, object] = {}
exec(compile(HELPERS_SOURCE, "<collector-helpers>", "exec"), helpers)


class CollectorHelperTests(unittest.TestCase):
    def test_disabled_hit_and_run_source_is_completely_empty(self):
        status = helpers["configured_hr_records"]([])

        self.assertFalse(status["enabled"])
        self.assertEqual(status["configured"], 0)
        self.assertEqual(status["effective"], 0)
        self.assertEqual(status["sources"], {})
        self.assertEqual(status["activeCount"], 0)

    def test_configured_source_needs_first_success_before_protecting(self):
        row = {
            "hash": "a" * 40,
            "name": "Fixture.2026.1080p",
            "tracker": "https://tracker.btschool.club/announce",
            "tags": "H&R",
            "private": True,
            "progress": 1,
        }
        source = {
            "site": "btschool.club",
            "taskLabel": "学校站",
            "validated": False,
            "available": False,
            "activeHashes": set(),
            "candidateHashes": set(),
        }
        task = helpers["make_task"](
            row,
            "整部",
            set(),
            set(),
            True,
            frozenset(),
            frozenset(),
            {"btschool.club": source},
        )
        self.assertFalse(task["hr"])
        self.assertFalse(task["hr_unknown"])

        source["validated"] = True
        source["available"] = False
        task = helpers["make_task"](
            row,
            "整部",
            set(),
            set(),
            True,
            frozenset(),
            frozenset(),
            {"btschool.club": source},
        )
        self.assertFalse(task["hr"])
        self.assertTrue(task["hr_unknown"])

        source["available"] = True
        source["activeHashes"] = {"a" * 40}
        task = helpers["make_task"](
            row,
            "整部",
            set(),
            set(),
            True,
            frozenset(),
            frozenset(),
            {"btschool.club": source},
        )
        self.assertTrue(task["hr"])
        self.assertFalse(task["hr_unknown"])

    def test_remote_collector_source_compiles(self):
        source = REMOTE_COLLECTOR.replace(
            "__HR_HASH_CACHE__", "{}"
        ).replace(
            "__HR_SOURCE_CACHE__", "{}"
        ).replace("__QB_FILE_CACHE__", "{}")

        compile(source, "<remote-collector>", "exec")

    def test_adjacent_nfo_provider_ids_are_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "The Moon (2023)"
            directory.mkdir()
            (directory / "The Moon (2023).nfo").write_text(
                """<?xml version="1.0"?>
<movie>
  <tmdbid>1366507</tmdbid>
  <uniqueid type="imdb" default="true">tt33576313</uniqueid>
</movie>
""",
                encoding="utf-8",
            )

            self.assertEqual(
                helpers["nfo_provider_ids"](directory),
                {"Tmdb": "1366507", "Imdb": "tt33576313"},
            )

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

    def test_self_publish_detection_accepts_exact_publication_evidence(self):
        task_hash = "a" * 40
        row = {
            "hash": task_hash,
            "category": "pt-cyanbug",
            "tags": "PT, 大青虫",
            "name": "Fixture.2024.1080p",
        }

        self.assertTrue(helpers["is_self_published"](row, {task_hash}))
        self.assertEqual(
            helpers["task_status"](row, {task_hash}),
            ("自发布", "warning"),
        )

    def test_self_publish_detection_accepts_audited_btschool_markers(self):
        task_hash = "b" * 40
        candidate = {
            "hash": "c" * 40,
            "category": "pt-btschool",
            "tags": "候选47422, 学校",
            "name": "Pressure.2026",
        }
        dedicated_path = {
            "hash": task_hash,
            "category": "pt-btschool",
            "tags": "学校",
            "content_path": (
                "/mnt/sdc/downloads/completed/pt-btschool/"
                + task_hash
                + "/Pressure"
            ),
            "name": "Pressure.2026",
        }
        incoming_crossseed = dict(dedicated_path)
        incoming_crossseed["content_path"] = (
            "/mnt/sdc/downloads/incoming/real-steal-btschool-47324/qb/"
            + task_hash
        )
        ordinary = dict(dedicated_path)
        ordinary["content_path"] = "/mnt/sdc/downloads/completed/Pressure"

        self.assertTrue(helpers["is_self_published"](candidate))
        self.assertTrue(helpers["is_self_published"](dedicated_path))
        self.assertFalse(helpers["is_self_published"](incoming_crossseed))
        self.assertFalse(helpers["is_self_published"](ordinary))

    def test_publication_ledger_json_is_strictly_hash_based(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "publication-ledger.json"
            path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {"infohash": "D" * 40},
                            {"infohash": "not-a-hash"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                helpers["publication_ledger_json_hashes"](path),
                {"d" * 40},
            )

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

    def test_jellyfin_disc_image_is_considered_a_media_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            iso = Path(temporary) / "The Girl with All the Gifts.iso"
            iso.write_bytes(b"fixture")

            self.assertEqual(helpers["video_files"](iso), [iso])

    def test_torrent_payload_files_parses_multifile_manifest(self):
        info = (
            b"d5:filesl"
            b"d6:lengthi14e4:pathl11:Episode.mkvee"
            b"d6:lengthi7e4:pathl10:poster.jpgee"
            b"e4:name7:Fixturee"
        )
        torrent = b"d4:info" + info + b"e"

        self.assertEqual(
            helpers["torrent_payload_files"](torrent),
            [
                {"name": "Fixture/Episode.mkv", "size": 14},
                {"name": "Fixture/poster.jpg", "size": 7},
            ],
        )

    def test_hr_candidate_assignment_requires_exact_verified_payload(self):
        torrents = [
            {
                "hash": "a" * 40,
                "name": "Way.of.Choices.2026.S01.2160p.WEB-DL",
                "progress": 1,
                "_file_list_verified": True,
                "_exact_files": [
                    {"name": "Way of Choices/E01.mkv", "size": 14}
                ],
            },
            {
                "hash": "b" * 40,
                "name": "Way.of.Choices.2026.S01.2160p.WEB-DL.REPACK",
                "progress": 1,
                "_file_list_verified": True,
                "_exact_files": [
                    {"name": "Way of Choices/E01.mkv", "size": 15}
                ],
            }
        ]
        assignments, covered_titles = helpers["assign_hr_candidates"](
            torrents,
            [
                {
                    "normalizedTitle": "way of choices 2026 s01",
                    "payloadSignature": (
                        ("e01.mkv", 14),
                    ),
                },
                {
                    "normalizedTitle": "missing release 2026",
                    "payloadSignature": (
                        ("missing.mkv", 99),
                    ),
                },
            ],
        )

        self.assertEqual(assignments, {"a" * 40})
        self.assertEqual(covered_titles, {"way of choices 2026 s01"})

    def test_hr_candidate_rejects_incomplete_or_unverified_qb_payload(self):
        signature = (("e01.mkv", 14),)
        torrents = [
            {
                "hash": "a" * 40,
                "progress": 0.9,
                "_file_list_verified": True,
                "_exact_files": [{"name": "E01.mkv", "size": 14}],
            },
            {
                "hash": "b" * 40,
                "progress": 1,
                "_file_list_verified": False,
                "_exact_files": [{"name": "E01.mkv", "size": 14}],
            },
        ]

        assignments, covered_titles = helpers["assign_hr_candidates"](
            torrents,
            [
                {
                    "normalizedTitle": "way of choices",
                    "payloadSignature": signature,
                }
            ],
        )

        self.assertEqual(assignments, set())
        self.assertEqual(covered_titles, set())

    def test_hr_match_requires_verified_complete_qb_payload(self):
        torrents = [
            {
                "hash": "a" * 40,
                "progress": 1,
                "_file_list_verified": True,
                "_exact_files": [
                    {"name": "complete.mkv", "size": 14, "progress": 1}
                ],
            },
            {
                "hash": "b" * 40,
                "progress": 0,
                "_file_list_verified": True,
                "_exact_files": [
                    {"name": "missing.mkv", "size": 14, "progress": 0}
                ],
            },
            {
                "hash": "c" * 40,
                "progress": 1,
                "_file_list_verified": False,
                "_exact_files": [
                    {"name": "unverified.mkv", "size": 14, "progress": 1}
                ],
            },
        ]

        self.assertEqual(
            helpers["verified_complete_qb_hashes"](torrents),
            {"a" * 40},
        )

    def test_non_school_private_sites_are_pending_until_an_adapter_exists(self):
        row = {
            "hash": "d" * 40,
            "name": "Fixture.2026.1080p",
            "category": "pt-crabpt",
            "tags": "蟹黄堡",
            "private": True,
            "progress": 1,
        }
        task = helpers["make_task"](
            row,
            "整部",
            set(),
            set(),
            True,
            frozenset(),
            {"蟹黄堡"},
        )

        self.assertFalse(task["hr"])
        self.assertTrue(task["hr_unknown"])
        self.assertEqual(task["status"], "待核 H&R")
        self.assertEqual(task["tone"], "protected")

    def test_hr_source_cache_discards_credentials_and_invalid_manifests(self):
        cache = sanitize_hr_source_cache(
            {
                "sites": {
                    "btschool.club": {
                        "records": {"123": "Fixture", "bad": "ignored"},
                        "fetchedAt": 1700000000,
                        "cookie": "secret",
                    }
                },
                "manifests": {
                    "btschool.club:123": {
                        "hash": "a" * 40,
                        "payloadSignature": [["Fixture.mkv", 14]],
                        "videoSizes": [14],
                        "fetchedAt": 1700000000,
                    },
                    "btschool.club:bad": {
                        "hash": "not-a-hash",
                        "payloadSignature": [["bad.mkv", 1]],
                        "fetchedAt": 1700000000,
                    },
                },
            }
        )

        self.assertEqual(
            cache,
            {
                "version": 1,
                "sites": {
                    "btschool.club": {
                        "records": {"123": "Fixture"},
                        "fetchedAt": 1700000000,
                    }
                },
                "manifests": {
                    "btschool.club:123": {
                        "hash": "a" * 40,
                        "payloadSignature": [["Fixture.mkv", 14]],
                        "videoSizes": [14],
                        "fetchedAt": 1700000000,
                    }
                },
            },
        )

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

    def test_hardlink_index_skips_transaction_quarantine_inside_discovery_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            volume = root / "volume"
            library = volume / "library"
            transaction_quarantine = volume / ".storage-cleanup-quarantine"
            for directory in (library, transaction_quarantine):
                directory.mkdir(parents=True)
            media = library / "Fixture.mkv"
            media.write_bytes(b"fixture")
            staged = transaction_quarantine / "plan_a/000000-Fixture.mkv"
            staged.parent.mkdir()
            os.link(media, staged)
            # Discovery over the whole volume must still exclude the
            # transaction quarantine where files are staged for deletion.
            helpers["HARDLINK_DISCOVERY_ROOTS"] = (str(volume),)
            helpers["QUARANTINE_ROOTS"] = (transaction_quarantine,)

            index, verified = helpers["hardlink_path_index"]()

            self.assertTrue(verified)
            inode = (media.stat().st_dev, media.stat().st_ino)
            self.assertEqual(index[inode], {str(media)})
            self.assertNotIn(str(staged), index[inode])

    def test_tv_expected_total_uses_cached_tmdb_season_counts(self):
        self.assertEqual(
            helpers["tv_expected_total"](
                "tv:tmdb:224372",
                {(1, 1), (1, 2)},
                {"224372": {1: 6}},
            ),
            6,
        )

    def test_tv_expected_episodes_catches_a_gap_even_when_count_matches(self):
        expected = helpers["tv_expected_episodes"](
            "tv:tmdb:224372",
            {(1, 1), (1, 3)},
            {"224372": {1: {"count": 3, "episodes": [1, 2, 3]}}},
        )
        self.assertEqual(expected, {(1, 1), (1, 2), (1, 3)})

    def test_special_episode_zero_is_not_a_regular_episode(self):
        self.assertFalse(helpers["is_regular_episode"](8, 0))
        self.assertTrue(helpers["is_regular_episode"](8, 1))
        self.assertFalse(helpers["is_regular_episode"](0, 1))

    def test_episode_gaps_are_detected_without_a_provider_identity(self):
        self.assertEqual(
            helpers["episode_gaps"]({(1, 1), (1, 3), (2, 1)}),
            {(1, 2)},
        )

    def test_episode_gaps_include_missing_leading_episodes(self):
        self.assertEqual(
            helpers["episode_gaps"]({(1, 5)}),
            {(1, 1), (1, 2), (1, 3), (1, 4)},
        )

    def test_cached_metadata_identity_is_used_for_providerless_tv(self):
        helpers["TMDB_METADATA_HINTS"] = [
            {
                "kind": "tv",
                "title": "黑袍纠察队",
                "englishTitle": "The Boys",
                "query": "The Boys",
                "tmdbId": 76479,
            }
        ]
        self.assertEqual(
            helpers["metadata_tmdb_identity"](
                {
                    "names": ["黑袍纠察队 第一季 (2019)"],
                    "original_titles": [],
                    "paths": ["/media/TV/黑袍纠察队 第一季 (2019)"],
                }
            ),
            "tv:tmdb:76479",
        )
        self.assertEqual(
            helpers["tv_expected_total"](
                "tv:tmdb:224372",
                {(1, 1), (2, 1)},
                {"224372": {1: 6, 2: 8}},
            ),
            14,
        )

    def test_tmdb_api_key_falls_back_to_moviepilot_builtin_default(self):
        default = helpers["DEFAULT_TMDB_API_KEY"]
        self.assertEqual(helpers["tmdb_api_key"](""), default)
        self.assertEqual(helpers["tmdb_api_key"]("# no key here\n\n"), default)
        self.assertEqual(
            helpers["tmdb_api_key"]("TMDB_API_KEY=\nPROXY_HOST=http://x"),
            default,
        )

    def test_tmdb_api_key_prefers_explicit_env_value(self):
        self.assertEqual(
            helpers["tmdb_api_key"](
                "PROXY_HOST=http://127.0.0.1:11082\nTMDB_API_KEY=abc123\n"
            ),
            "abc123",
        )

    def test_tv_expected_total_returns_none_without_cache_or_tmdb_identity(self):
        self.assertIsNone(
            helpers["tv_expected_total"](
                "tv:tmdb:999999",
                {(1, 1)},
                {"224372": {1: 6}},
            )
        )
        self.assertIsNone(
            helpers["tv_expected_total"](
                "movie:tmdb:123456",
                {(1, 1)},
                {"123456": {1: 6}},
            )
        )
        self.assertIsNone(
            helpers["tv_expected_total"](
                "tv:tmdb:224372",
                set(),
                {"224372": {1: 6}},
            )
        )

    def test_episode_regex_extracts_regular_episode_numbers_only(self):
        match = helpers["EPISODE_RE"].search(
            "/media/TV/示例 - S01E03 - 第 3 集.mkv"
        )
        self.assertEqual(match.group(1), "03")
        self.assertIsNone(
            helpers["EPISODE_RE"].search("/media/TV/示例 - S01 - 全季.mkv")
        )
        self.assertIsNone(
            helpers["EPISODE_RE"].search("/media/TV/示例.mkv")
        )

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
