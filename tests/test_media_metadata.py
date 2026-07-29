#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from scripts.media_metadata import (
    _metadata_query,
    _validated_result,
    apply_metadata_overrides,
    annotate_hr_missing_resources,
    enrich_and_merge_resources,
    make_hr_metadata_resources,
    metadata_cache_key,
    prune_metadata_cache,
    sanitize_metadata_cache,
    validate_metadata_overrides,
)


def private_record(
    *,
    identity: str,
    task_hash: str,
    path: str,
    inode: int,
    site: str = "学校站",
    scope: str = "S01",
    moviepilot_indexes: list[dict] | None = None,
    moviepilot_index_source_available: bool = True,
) -> dict:
    file_record = {
        "path": path,
        "dev": 2,
        "inode": inode,
        "size": 1024**3,
        "nlink": 1,
        "allowed": True,
    }
    cleanup_record = {
        **file_record,
        "source": "qb",
        "exists": True,
        "regular": True,
        "relativeSafe": True,
        "required": True,
        "qbExpectedSize": 1024**3,
        "qbProgress": 1.0,
    }
    return {
        "identity": identity,
        "allLinksKnown": True,
        "files": [file_record],
        "cleanupFiles": [cleanup_record],
        "cleanupLinksKnown": True,
        "libraryScanVerified": True,
        "qbFileListsVerified": True,
        "roots": [{"path": path.rsplit("/", 1)[0], "allowed": True}],
        "moviepilotIndexes": moviepilot_indexes or [],
        "moviepilotIndexSourceAvailable": moviepilot_index_source_available,
        "qbTasks": [
            {
                "hash": task_hash,
                "name": f"Fixture {scope}",
                "site": site,
                "scope": scope,
                "state": "forcedUP",
                "progress": 1.0,
                "private": True,
                "hr": False,
                "hrUnknown": False,
                "selfPublish": False,
                "contentPath": path,
                "savePath": path.rsplit("/", 1)[0],
                "category": "fixture",
                "tags": "",
                "fileListVerified": True,
                "exactFiles": [],
            }
        ],
    }


def raw_resource(
    *,
    resource_id: str,
    title: str,
    english: str,
    edition: str,
    media_type: str,
    private: dict,
    library: bool = False,
    year: str = "2005",
) -> dict:
    return {
        "id": resource_id,
        "title": title,
        "englishTitle": english,
        "edition": edition,
        "type": media_type,
        "year": year,
        "size": 1.0,
        "sizeLabel": "1.0 GB",
        "reclaimLabel": "完整删除可释放 1.0 GB",
        "library": library,
        "hr": False,
        "hrPending": False,
        "brush": False,
        "protected": False,
        "qbSummary": "1 个 qB 任务",
        "siteSummary": "学校站",
        "librarySummary": "已入库" if library else "未入库",
        "libraryDetail": "Jellyfin 可播放" if library else "仅在 qB / 下载区",
        "seedTasks": [],
        "impactTitle": "同时影响 1 个 qB / PT 任务",
        "impactDetail": "学校站 S01",
        "_private": private,
    }


class MediaMetadataTests(unittest.TestCase):
    def test_merge_preserves_moviepilot_index_identity(self):
        index = {
            "id": 188,
            "server": "jellyfin",
            "itemId": "dbb7950e10c17e266c363ac9bd664a5d",
            "itemType": "电视剧",
            "title": "Way of Choices",
            "originalTitle": "择天记",
            "year": "2026",
            "path": "/mnt/sdd/media/TV/择天记 (2026)",
            "seasonInfo": "{\"1\": [1, 2]}",
        }
        resource = raw_resource(
            resource_id="res_choices",
            title="择天记",
            english="Way of Choices",
            edition="S01 · 2 集",
            media_type="电视剧",
            library=True,
            year="2026",
            private=private_record(
                identity="tv:tmdb:123456",
                task_hash="a" * 40,
                path="/mnt/sdd/media/TV/择天记 (2026)/S01E01.mkv",
                inode=188,
                moviepilot_indexes=[index],
            ),
        )

        merged, _ = enrich_and_merge_resources(
            [resource], {"version": 1, "entries": {}}
        )

        self.assertEqual(merged[0]["_private"]["moviepilotIndexes"], [index])
        self.assertTrue(
            merged[0]["_private"]["moviepilotIndexSourceAvailable"]
        )

    def test_manual_override_requires_exact_query_and_preserves_identity(self):
        matching = raw_resource(
            resource_id="res_gifts",
            title="中文名待识别",
            english="THE GIRL WITH ALL THE GIFTS iso",
            edition="电影",
            media_type="电影",
            year="2016",
            private=private_record(
                identity="qb:gifts",
                task_hash="f" * 40,
                path="/allowed/gifts.iso",
                inode=80,
            ),
        )
        different = raw_resource(
            resource_id="res_other",
            title="中文名待识别",
            english="THE GIRL WITH ALL THE GIFTS remux",
            edition="电影",
            media_type="电影",
            year="2016",
            private=private_record(
                identity="qb:other",
                task_hash="e" * 40,
                path="/allowed/other.mkv",
                inode=81,
            ),
        )
        overrides = {
            "version": 1,
            "entries": [
                {
                    "query": "THE GIRL WITH ALL THE GIFTS iso",
                    "kind": "movie",
                    "title": "天赐之女",
                    "englishTitle": "The Girl with All the Gifts",
                    "year": "2016",
                    "identity": (
                        "movie:manual:the-girl-with-all-the-gifts"
                    ),
                    "source": (
                        "https://www.1905.com/mdb/film/2235218/info/"
                    ),
                    "verifiedAt": "2026-07-29T04:30:00+08:00",
                }
            ],
        }

        cache, applied = apply_metadata_overrides(
            [matching, different],
            {"version": 1, "entries": {}},
            overrides,
        )

        self.assertEqual(applied, 1)
        entry = cache["entries"][metadata_cache_key(matching)]
        self.assertEqual(entry["title"], "天赐之女")
        self.assertEqual(
            entry["identity"],
            "movie:manual:the-girl-with-all-the-gifts",
        )
        self.assertNotIn(metadata_cache_key(different), cache["entries"])

    def test_manual_override_rejects_year_conflict(self):
        item = raw_resource(
            resource_id="res_conflict",
            title="中文名待识别",
            english="Normal 2025",
            edition="电影",
            media_type="电影",
            year="2025",
            private=private_record(
                identity="qb:normal",
                task_hash="d" * 40,
                path="/allowed/normal.mkv",
                inode=82,
            ),
        )
        overrides = {
            "version": 1,
            "entries": [
                {
                    "query": "Normal 2025",
                    "kind": "movie",
                    "title": "普通人",
                    "englishTitle": "Normal",
                    "year": "2026",
                    "identity": "movie:manual:normal",
                    "source": "https://example.com/normal",
                    "verifiedAt": "2026-07-29T04:30:00+08:00",
                }
            ],
        }

        cache, applied = apply_metadata_overrides(
            [item],
            {"version": 1, "entries": {}},
            overrides,
        )

        self.assertEqual(applied, 0)
        self.assertEqual(cache["entries"], {})

    def test_invalid_or_duplicate_manual_override_fails_closed(self):
        with self.assertRaises(ValueError):
            validate_metadata_overrides(
                {
                    "version": 1,
                    "entries": [
                        {
                            "query": "Fixture",
                            "kind": "movie",
                            "title": "Fixture",
                            "englishTitle": "Fixture",
                            "year": "2026",
                            "identity": "movie:manual:fixture",
                            "source": "http://example.com",
                            "verifiedAt": "2026-07-29T04:30:00+08:00",
                        }
                    ],
                }
            )

    def test_metadata_cache_prunes_stale_queries(self):
        item = raw_resource(
            resource_id="res_current",
            title="中文名待识别",
            english="Current Movie 2026",
            edition="电影",
            media_type="电影",
            year="2026",
            private=private_record(
                identity="qb:current",
                task_hash="c" * 40,
                path="/allowed/current.mkv",
                inode=83,
            ),
        )
        current_key = metadata_cache_key(item)
        cache = {
            "version": 1,
            "entries": {
                current_key: {
                    "query": item["englishTitle"],
                    "kind": "movie",
                    "status": "unresolved",
                    "checkedAt": "2026-07-29T00:00:00+00:00",
                },
                "a" * 64: {
                    "query": "Stale Movie",
                    "kind": "movie",
                    "status": "unresolved",
                    "checkedAt": "2026-07-29T00:00:00+00:00",
                },
            },
        }

        got = prune_metadata_cache([item], cache)

        self.assertEqual(set(got["entries"]), {current_key})

    def test_missing_hr_is_attached_to_exact_library_identity_and_locked(self):
        resource = raw_resource(
            resource_id="res_fighter",
            title="择天记",
            english="Way of Choices",
            edition="S01 · 26 集",
            media_type="电视剧",
            library=True,
            year="2026",
            private=private_record(
                identity="tv:tmdb:282158",
                task_hash="a" * 40,
                path="/allowed/fighter.mkv",
                inode=60,
            ),
        )
        resource["_private"]["qbTasks"] = []
        merged, _ = enrich_and_merge_resources(
            [resource],
            {"version": 1, "entries": {}},
        )
        records = [
            {
                "id": "307598",
                "title": (
                    "Fighter of the Destiny S01 2026 2160p "
                    "WEB-DL HEVC"
                ),
                "coveredByCandidate": False,
            }
        ]
        pseudo = make_hr_metadata_resources(records)[0]
        cache = {
            "version": 1,
            "entries": {
                metadata_cache_key(pseudo): {
                    "query": pseudo["englishTitle"],
                    "kind": "tv",
                    "status": "resolved",
                    "checkedAt": "2026-07-29T00:00:00+00:00",
                    "title": "择天记",
                    "englishTitle": "Fighter of the Destiny",
                    "year": "2026",
                    "identity": "tv:tmdb:282158",
                    "tmdbId": 282158,
                }
            },
        }

        resources, annotated, stats = annotate_hr_missing_resources(
            merged,
            records,
            cache,
        )

        self.assertTrue(resources[0]["protected"])
        self.assertTrue(resources[0]["hrPending"])
        self.assertEqual(
            resources[0]["impactTitle"],
            "学校站 H&R 任务缺失",
        )
        self.assertIn(
            {
                "site": "学校站",
                "scope": "S01 · 2160p",
                "status": "H&R 缺失",
                "tone": "protected",
            },
            resources[0]["seedTasks"],
        )
        self.assertEqual(
            annotated[0]["linkedResourceTitle"],
            "择天记",
        )
        self.assertEqual(stats["hrMissingLinkedRecords"], 1)
        self.assertEqual(stats["hrMissingLinkedResources"], 1)
        self.assertEqual(stats["hrMissingUnassigned"], 0)

    def test_library_can_use_one_matching_qb_release_name(self):
        private = private_record(
            identity="movie:path:fixture",
            task_hash="d" * 40,
            path="/allowed/hollywood.mkv",
            inode=30,
        )
        private["qbTasks"][0]["name"] = (
            "[万圣夜之女].Hollywood.Grit.2025.1080p.WEB-DL"
        )
        item = raw_resource(
            resource_id="res_hollywood",
            title="Hollywood Grit (2025)",
            english="Hollywood Grit (2025)",
            edition="电影",
            media_type="电影",
            library=True,
            year="2025",
            private=private,
        )

        self.assertEqual(
            _metadata_query(item),
            "[万圣夜之女].Hollywood.Grit.2025.1080p.WEB-DL",
        )

    def test_library_rejects_qb_release_name_with_conflicting_year(self):
        private = private_record(
            identity="movie:path:fixture",
            task_hash="e" * 40,
            path="/allowed/normal.mkv",
            inode=31,
        )
        private["qbTasks"][0]["name"] = "Normal.2025.COMPLETE.BLURAY"
        item = raw_resource(
            resource_id="res_normal",
            title="Normal (2026)",
            english="Normal (2026)",
            edition="电影",
            media_type="电影",
            library=True,
            year="2026",
            private=private,
        )

        self.assertEqual(_metadata_query(item), "Normal (2026)")

    def test_parsed_bilingual_result_is_strictly_sanitized(self):
        item = {
            "title": "raw",
            "englishTitle": "[京城奇探] Cases Between Us 2026 S01",
            "type": "电视剧",
            "library": False,
        }
        got = _validated_result(
            item,
            {
                "query": item["englishTitle"],
                "kind": "tv",
                "status": "parsed",
                "parsedChinese": "京城奇探",
                "parsedEnglish": "Cases Between Us",
                "parsedYear": "2026",
            },
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(got["status"], "resolved")
        self.assertEqual(got["title"], "京城奇探")
        self.assertEqual(got["englishTitle"], "Cases Between Us")
        self.assertRegex(got["identity"], r"^tv:name:[a-f0-9]{24}$")

    def test_recognized_result_rejects_year_or_type_mismatch(self):
        item = {
            "title": "中文名待识别",
            "englishTitle": "Zootopia 2 2025 2160p",
            "type": "电影",
            "library": False,
        }
        bad = _validated_result(
            item,
            {
                "query": item["englishTitle"],
                "kind": "movie",
                "status": "recognized",
                "parsedChinese": "",
                "parsedEnglish": "Zootopia 2",
                "parsedYear": "2025",
                "title": "疯狂动物城2",
                "year": "2024",
                "resultType": "MediaType.TV",
                "tmdbId": 1084242,
            },
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(bad["status"], "unresolved")

    def test_exact_tmdb_identity_does_not_trust_a_stale_library_year(self):
        item = {
            "title": "Hitori No Shita - The Outcast",
            "englishTitle": "Hitori No Shita - The Outcast",
            "year": "2015",
            "type": "电视剧",
            "library": True,
            "_private": {"identity": "tv:tmdb:67063"},
        }
        got = _validated_result(
            item,
            {
                "query": item["englishTitle"],
                "kind": "tv",
                "status": "recognized",
                "parsedChinese": "",
                "parsedEnglish": "Hitori No Shita - The Outcast",
                "parsedYear": "",
                "title": "一人之下",
                "englishTitle": "Hitori no Shita: The Outcast",
                "year": "2016",
                "resultType": "MediaType.TV",
                "tmdbId": 67063,
            },
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(got["status"], "resolved")
        self.assertEqual(got["identity"], "tv:tmdb:67063")
        self.assertEqual(got["year"], "2016")

    def test_same_qb_only_series_merges_seasons_and_tasks(self):
        resources = []
        entries = {}
        for index, season in enumerate((3, 4, 5), start=1):
            item = raw_resource(
                resource_id=f"res_{index}",
                title="中文名待识别",
                english=(
                    "It's Always Sunny in Philadelphia 2005 "
                    f"S{season:02d} 1080p"
                ),
                edition=f"S{season:02d} · 未入库",
                media_type="电视剧",
                private=private_record(
                    identity=f"qb:{index}",
                    task_hash=str(index) * 40,
                    path=f"/allowed/s{season}.mkv",
                    inode=season,
                    scope=f"S{season:02d}",
                ),
            )
            resources.append(item)
            entries[metadata_cache_key(item)] = {
                "query": item["englishTitle"],
                "kind": "tv",
                "status": "resolved",
                "checkedAt": "2026-07-29T00:00:00+00:00",
                "title": "费城永远阳光灿烂",
                "englishTitle": "It's Always Sunny in Philadelphia",
                "year": "2005",
                "identity": "tv:tmdb:2710",
                "tmdbId": 2710,
            }

        merged, stats = enrich_and_merge_resources(
            resources,
            {"version": 1, "entries": entries},
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "费城永远阳光灿烂")
        self.assertEqual(
            merged[0]["englishTitle"],
            "It's Always Sunny in Philadelphia",
        )
        self.assertEqual(merged[0]["edition"], "S03–S05 · 未入库")
        self.assertEqual(merged[0]["qbSummary"], "3 个 qB 任务")
        self.assertEqual(len(merged[0]["seedTasks"]), 3)
        self.assertEqual(merged[0]["size"], 3.0)
        self.assertTrue(merged[0]["metadataVerified"])
        self.assertFalse(merged[0]["protected"])
        self.assertEqual(stats["metadataResolvedQbResources"], 3)

    def test_tmdb_identity_merges_library_and_separate_qb_payload(self):
        library = raw_resource(
            resource_id="res_library",
            title="疯狂动物城2",
            english="Zootopia 2",
            edition="电影",
            media_type="电影",
            library=True,
            year="2025",
            private=private_record(
                identity="movie:tmdb:1084242",
                task_hash="a" * 40,
                path="/allowed/library.mkv",
                inode=10,
                site="蟹黄堡",
                scope="整部",
            ),
        )
        qb_only = raw_resource(
            resource_id="res_qb",
            title="中文名待识别",
            english="Zootopia 2 2025 2160p WEB-DL",
            edition="下载区资源 · 未入库",
            media_type="电影",
            year="",
            private=private_record(
                identity="qb:fixture",
                task_hash="b" * 40,
                path="/allowed/download.mkv",
                inode=11,
                site="大青虫",
                scope="整部",
            ),
        )
        key = metadata_cache_key(qb_only)
        cache = {
            "version": 1,
            "entries": {
                key: {
                    "query": qb_only["englishTitle"],
                    "kind": "movie",
                    "status": "resolved",
                    "checkedAt": "2026-07-29T00:00:00+00:00",
                    "title": "疯狂动物城2",
                    "englishTitle": "Zootopia 2",
                    "year": "2025",
                    "identity": "movie:tmdb:1084242",
                    "tmdbId": 1084242,
                }
            },
        }

        merged, _ = enrich_and_merge_resources([library, qb_only], cache)

        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["library"])
        self.assertEqual(merged[0]["qbSummary"], "2 个 qB 任务")
        self.assertEqual(merged[0]["size"], 2.0)
        self.assertEqual(
            merged[0]["_private"]["identity"],
            "movie:tmdb:1084242",
        )

    def test_unresolved_qb_name_can_join_one_exact_tmdb_identity_without_unlocking(self):
        library = raw_resource(
            resource_id="res_library",
            title="Trapped",
            english="Trapped",
            edition="电影",
            media_type="电影",
            library=True,
            year="2016",
            private=private_record(
                identity="movie:tmdb:376257",
                task_hash="a" * 40,
                path="/allowed/library.mkv",
                inode=20,
            ),
        )
        qb_only = raw_resource(
            resource_id="res_qb",
            title="中文名待识别",
            english="Trapped 2016 1080p WEB-DL",
            edition="下载区资源 · 未入库",
            media_type="电影",
            year="",
            private=private_record(
                identity="qb:trapped",
                task_hash="b" * 40,
                path="/allowed/download.mkv",
                inode=21,
            ),
        )
        cache = {
            "version": 1,
            "entries": {
                metadata_cache_key(qb_only): {
                    "query": qb_only["englishTitle"],
                    "kind": "movie",
                    "status": "unresolved",
                    "checkedAt": "2026-07-29T00:00:00+00:00",
                    "parsedEnglish": "Trapped",
                    "parsedYear": "2016",
                }
            },
        }

        merged, _ = enrich_and_merge_resources([library, qb_only], cache)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["qbSummary"], "2 个 qB 任务")
        self.assertEqual(
            merged[0]["_private"]["identity"],
            "movie:tmdb:376257",
        )
        self.assertFalse(merged[0]["metadataVerified"])
        self.assertTrue(merged[0]["protected"])

    def test_bilingual_library_duplicate_without_year_joins_unique_tmdb_series(self):
        exact = raw_resource(
            resource_id="res_exact",
            title="悬案",
            english="Unsettled Case",
            edition="S01 · 17 集",
            media_type="电视剧",
            library=True,
            year="2026",
            private=private_record(
                identity="tv:tmdb:273114",
                task_hash="a" * 40,
                path="/allowed/exact.mkv",
                inode=40,
            ),
        )
        duplicate = raw_resource(
            resource_id="res_duplicate",
            title="悬案",
            english="Unsettled Case",
            edition="S01 · 9 集",
            media_type="电视剧",
            library=True,
            year="",
            private=private_record(
                identity="tv:path:duplicate",
                task_hash="b" * 40,
                path="/allowed/duplicate.mkv",
                inode=41,
            ),
        )

        merged, _ = enrich_and_merge_resources(
            [exact, duplicate],
            {"version": 1, "entries": {}},
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "悬案")
        self.assertEqual(merged[0]["qbSummary"], "2 个 qB 任务")
        self.assertEqual(
            merged[0]["_private"]["identity"],
            "tv:tmdb:273114",
        )

    def test_same_chinese_title_with_english_aliases_joins_unique_tmdb_series(self):
        exact = raw_resource(
            resource_id="res_portland_exact",
            title="砵兰街行动",
            english="Operation Portland Street",
            edition="S01 · 未入库",
            media_type="电视剧",
            library=False,
            year="2026",
            private=private_record(
                identity="tv:tmdb:297142",
                task_hash="a" * 40,
                path="/allowed/portland-exact.mkv",
                inode=70,
            ),
        )
        alias = raw_resource(
            resource_id="res_portland_alias",
            title="砵兰街行动",
            english="Portland Street Operation",
            edition="S01 · 未入库",
            media_type="电视剧",
            library=False,
            year="2026",
            private=private_record(
                identity="tv:name:alias",
                task_hash="b" * 40,
                path="/allowed/portland-alias.mkv",
                inode=71,
            ),
        )

        merged, _ = enrich_and_merge_resources(
            [exact, alias],
            {"version": 1, "entries": {}},
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "砵兰街行动")
        self.assertEqual(merged[0]["qbSummary"], "2 个 qB 任务")
        self.assertEqual(len(merged[0]["seedTasks"]), 1)
        self.assertEqual(merged[0]["seedTasks"][0]["count"], 2)
        self.assertFalse(merged[0]["protected"])
        self.assertEqual(
            merged[0]["_private"]["identity"],
            "tv:tmdb:297142",
        )

    def test_same_series_name_with_conflicting_tmdb_ids_is_one_locked_row(self):
        first = raw_resource(
            resource_id="res_first",
            title="江海潮生",
            english="Zhang Jian The Legendary Entrepreneur",
            edition="S01 · 未入库",
            media_type="电视剧",
            library=True,
            year="2026",
            private=private_record(
                identity="tv:tmdb:329132",
                task_hash="a" * 40,
                path="/allowed/first.mkv",
                inode=50,
            ),
        )
        second = raw_resource(
            resource_id="res_second",
            title="张謇",
            english="Zhang Jian The Legendary Entrepreneur",
            edition="S01 · 未入库",
            media_type="电视剧",
            library=True,
            year="",
            private=private_record(
                identity="tv:tmdb:294467",
                task_hash="b" * 40,
                path="/allowed/second.mkv",
                inode=51,
            ),
        )

        merged, stats = enrich_and_merge_resources(
            [first, second],
            {"version": 1, "entries": {}},
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "江海潮生（又名：张謇）")
        self.assertEqual(
            merged[0]["impactTitle"],
            "媒体身份冲突，暂不可清理",
        )
        self.assertTrue(merged[0]["protected"])
        self.assertFalse(merged[0]["metadataVerified"])
        self.assertEqual(stats["metadataUnverifiedResources"], 1)

    def test_unresolved_tv_keeps_tv_type_and_season(self):
        item = raw_resource(
            resource_id="res_unresolved",
            title="中文名待识别",
            english="Unknown Show 2026 S02 1080p",
            edition="S02 · 未入库",
            media_type="电视剧",
            private=private_record(
                identity="qb:unknown",
                task_hash="c" * 40,
                path="/allowed/unknown.mkv",
                inode=12,
                scope="S02",
            ),
        )

        merged, stats = enrich_and_merge_resources(
            [item],
            {"version": 1, "entries": {}},
        )

        self.assertEqual(merged[0]["type"], "电视剧")
        self.assertEqual(merged[0]["edition"], "S02 · 未入库")
        self.assertFalse(merged[0]["metadataVerified"])
        self.assertTrue(merged[0]["protected"])
        self.assertEqual(
            merged[0]["impactTitle"],
            "名称待核，暂不可清理",
        )
        self.assertFalse(merged[0]["_private"]["metadataVerified"])

    def test_hash_like_unresolved_name_is_redacted_from_public_identity(self):
        resource = raw_resource(
            resource_id="res_hash_name",
            title="未知资源",
            english="b16084f8ca9ed3d6914e2206951aa6a84d2b784e",
            edition="电影",
            media_type="电影",
            private=private_record(
                identity="movie:qb:hash-name",
                task_hash="a" * 40,
                path="/mnt/sdc/downloads/hash-name.mkv",
                inode=99,
            ),
        )
        merged, stats = enrich_and_merge_resources([resource], {})
        self.assertEqual(merged[0]["title"], "未知资源")
        self.assertEqual(merged[0]["englishTitle"], "Unresolved title")
        self.assertFalse(merged[0]["metadataVerified"])
        self.assertTrue(merged[0]["protected"])
        self.assertEqual(stats["metadataUnresolvedQbResources"], 1)

    def test_cache_drops_sensitive_or_invalid_entries(self):
        got = sanitize_metadata_cache(
            {
                "version": 1,
                "entries": {
                    "x": {"query": "bad"},
                    "a" * 64: {
                        "query": "Fixture",
                        "kind": "movie",
                        "status": "resolved",
                        "checkedAt": "",
                        "title": "Fixture",
                        "englishTitle": "Fixture",
                        "identity": "movie:tmdb:1",
                        "tmdbId": 1,
                    },
                },
            }
        )

        self.assertEqual(got, {"version": 1, "entries": {}})


if __name__ == "__main__":
    unittest.main()
