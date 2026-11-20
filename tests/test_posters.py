import os
import shutil
from pathlib import Path

import pytest
from conftest import prepare_server
from fastapi.testclient import TestClient

from video_turbo.orchestrator import poster_service
from video_turbo.orchestrator.app import app
from video_turbo.posters import find_poster

EXTENSIONS = ["mp4", "mkv"]


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_prefixed_poster_wins(tmp_path: Path) -> None:
    video = _touch(tmp_path / "ABC-123.mp4")
    _touch(tmp_path / "poster.jpg")
    _touch(tmp_path / "ABC-123.jpg")
    _touch(tmp_path / "ABC-123-poster.png")
    _touch(tmp_path / "ABC-123-poster.jpg")
    assert find_poster(video, EXTENSIONS) == tmp_path / "ABC-123-poster.jpg"


@pytest.mark.parametrize("name", ["ABC-123.poster.jpeg", "abc-123_poster.BMP", "ABC-123 - poster.jpg"])
def test_prefixed_variants(tmp_path: Path, name: str) -> None:
    video = _touch(tmp_path / "ABC-123.mp4")
    _touch(tmp_path / name)
    assert find_poster(video, EXTENSIONS) == tmp_path / name


@pytest.mark.parametrize("extension", ["jpg", "png", "jpeg", "bmp", "JPG", "PNG", "JPEG", "BMP"])
@pytest.mark.parametrize("multiple_videos", [False, True])
def test_same_name_image(tmp_path: Path, extension: str, multiple_videos: bool) -> None:
    video = _touch(tmp_path / "NCY-135.mp4")
    poster = _touch(tmp_path / f"ncy-135.{extension}")
    _touch(tmp_path / "poster.jpg")
    if multiple_videos:
        _touch(tmp_path / "NCY-135-C.mkv")
    assert find_poster(video, EXTENSIONS) == poster


def test_same_name_image_requires_exact_stem(tmp_path: Path) -> None:
    video = _touch(tmp_path / "NCY-135.mp4")
    _touch(tmp_path / "NCY-135-C.jpg")
    _touch(tmp_path / "NCY-1350.png")
    (tmp_path / "NCY-135.jpeg").mkdir()
    assert find_poster(video, EXTENSIONS) is None


def test_plain_poster_only_in_single_movie_folder(tmp_path: Path) -> None:
    video = _touch(tmp_path / "ABC-123.mp4")
    _touch(tmp_path / "poster.jpeg")
    assert find_poster(video, EXTENSIONS) == tmp_path / "poster.jpeg"
    _touch(tmp_path / "XYZ-999.mkv")
    assert find_poster(video, EXTENSIONS) is None


def test_other_movie_poster_not_used(tmp_path: Path) -> None:
    video = _touch(tmp_path / "ABC-123.mp4")
    _touch(tmp_path / "ABC-123-C.mp4")
    _touch(tmp_path / "ABC-123-C-poster.jpg")
    assert find_poster(video, EXTENSIONS) is None


def test_nfo_local_paths(tmp_path: Path) -> None:
    video = _touch(tmp_path / "ABC-123.mp4")
    _touch(tmp_path / "art" / "cover.jpg")
    (tmp_path / "ABC-123.nfo").write_text(
        "<movie><thumb>https://example.com/a.jpg</thumb><art><poster>art/cover.jpg</poster></art></movie>",
        encoding="utf-8",
    )
    assert find_poster(video, EXTENSIONS) == tmp_path / "art" / "cover.jpg"


def test_nfo_urls_ignored_and_broken_xml_tolerated(tmp_path: Path) -> None:
    video = _touch(tmp_path / "ABC-123.mp4")
    absolute = _touch(tmp_path / "elsewhere" / "p.png")
    (tmp_path / "ABC-123.nfo").write_text(
        f'<movie><thumb aspect="poster">http://x/y.jpg</thumb><thumb aspect="poster">{absolute}</thumb> & broken',
        encoding="utf-8",
    )
    assert find_poster(video, EXTENSIONS) == absolute
    (tmp_path / "ABC-123.nfo").write_text("<movie><thumb>http://x/y.jpg</thumb></movie>", encoding="utf-8")
    assert find_poster(video, EXTENSIONS) is None


def test_movie_nfo_used_for_single_movie_folder(tmp_path: Path) -> None:
    video = _touch(tmp_path / "ABC-123.mp4")
    _touch(tmp_path / "folder-art.jpg")
    (tmp_path / "movie.nfo").write_text("<movie><thumb>folder-art.jpg</thumb></movie>", encoding="utf-8")
    assert find_poster(video, EXTENSIONS) == tmp_path / "folder-art.jpg"


def test_poster_cache_and_invalidation(tmp_path: Path, monkeypatch) -> None:
    library = tmp_path / "library"
    prepare_server(tmp_path, monkeypatch, {"media_libraries": [{"id": "lib-lib", "name": "lib", "path": str(library)}]})
    calls: list[Path] = []
    real_find = poster_service.find_poster

    def counting_find(video, extensions, source=None):
        calls.append(video)
        return real_find(video, extensions, source)

    monkeypatch.setattr(poster_service, "find_poster", counting_find)
    thumbnails: list[Path] = []

    def fake_thumbnail(poster: Path, target: Path) -> bool:
        thumbnails.append(poster)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(poster, target)
        return True

    monkeypatch.setattr(poster_service, "make_thumbnail", fake_thumbnail)
    video = _touch(library / "ABC-123" / "ABC-123.mp4")
    poster = _touch(library / "ABC-123" / "ABC-123-poster.jpg", b"first")
    lonely = _touch(library / "XYZ-999.mp4")

    with TestClient(app) as client:
        database = app.state.database
        assert client.get("/api/settings/ui").json() == {"show_posters": True, "notice_sound": True}
        assert client.put("/api/settings/ui", json={"show_posters": False}).status_code == 200
        assert client.get("/api/status").json()["ui"] == {"show_posters": False, "notice_sound": True}
        assert client.put("/api/settings/ui", json={"show_posters": True}).status_code == 200
        assert client.get("/api/status").json()["ui"] == {"show_posters": True, "notice_sound": True}

        ids = {}
        for path in (video, lonely):
            stat = path.stat()
            ids[path], _ = database.upsert_media(
                path=path, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                duration=None, probe=None, decision="done", reason="", file_key=None,
            )

        def urls() -> dict[int, str | None]:
            items = client.get("/api/restore/media", params={"status": "all"}).json()["items"]
            return {item["media_id"]: item["poster_url"] for item in items}

        # 未缓存时给出地址，由图片请求懒加载查找。
        assert urls() == {ids[video]: f"/api/media/{ids[video]}/poster", ids[lonely]: f"/api/media/{ids[lonely]}/poster"}
        first = client.get(f"/api/media/{ids[video]}/poster")
        assert first.status_code == 200 and first.content == b"first"
        assert client.get(f"/api/media/{ids[lonely]}/poster").status_code == 404
        assert len(calls) == 2

        # 已缓存：列表带版本号；没海报的不再请求。重复请求不再查找目录或重新生成。
        cached = urls()
        assert cached[ids[video]].startswith(f"/api/media/{ids[video]}/poster?v=")
        assert cached[ids[lonely]] is None
        client.get(f"/api/media/{ids[video]}/poster")
        assert client.get(f"/api/media/{ids[lonely]}/poster").status_code == 404
        assert len(calls) == 2 and len(thumbnails) == 1

        # 海报被替换：stat 不一致，重新查找并生成新缩略图，旧缩略图清理。
        poster.write_bytes(b"second-version")
        os.utime(poster, ns=(1, 1))
        assert client.get(f"/api/media/{ids[video]}/poster").content == b"second-version"
        assert len(calls) == 3 and len(thumbnails) == 2
        assert len(list((tmp_path / "cache" / "thumbnails").glob(f"{ids[video]}-*.jpg"))) == 1

        # 影片改名（例如影片修复改名后媒体记录指向新路径）：缓存失效，按新文件名查找。
        renamed = video.with_name("ABC-123-U.mp4")
        video.rename(renamed)
        poster.rename(poster.with_name("ABC-123-U-poster.jpg"))
        stat = renamed.stat()
        database.relocate_media(ids[video], path=renamed, size=stat.st_size, mtime_ns=stat.st_mtime_ns, file_key=None)
        assert urls()[ids[video]] == f"/api/media/{ids[video]}/poster"
        assert client.get(f"/api/media/{ids[video]}/poster").content == b"second-version"

        # 扫描后重新尝试之前没找到海报的影片。
        _touch(library / "XYZ-999-poster.png", b"new")
        stat = lonely.stat()
        database.upsert_media(
            path=lonely, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            duration=None, probe=None, decision="done", reason="", file_key=None,
        )
        assert urls()[ids[lonely]] == f"/api/media/{ids[lonely]}/poster"
        assert client.get(f"/api/media/{ids[lonely]}/poster").content == b"new"


def test_concurrent_thumbnail_requests_share_one_result(tmp_path: Path) -> None:
    import concurrent.futures

    from video_turbo.config import AppConfig, MediaLibraryConfig
    from video_turbo.database import Database
    from video_turbo.orchestrator.poster_service import PosterService
    from video_turbo.orchestrator.sources import MediaSources

    if shutil.which("ffmpeg") is None:
        pytest.skip("需要 FFmpeg")
    import subprocess

    video = _touch(tmp_path / "lib" / "ABC-123.mp4")
    poster = tmp_path / "lib" / "ABC-123-poster.jpg"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=600x900", "-frames:v", "1", str(poster)],
        check=True,
    )
    database = Database(tmp_path / "app.db")
    database.initialize()
    stat = video.stat()
    media_id, _ = database.upsert_media(
        path=video, library_id="lib-lib", size=stat.st_size, mtime_ns=stat.st_mtime_ns,
        duration=None, probe=None, decision="done", reason="", file_key=None,
    )
    config = AppConfig(
        media_libraries=[MediaLibraryConfig(id="lib-lib", name="lib", path=tmp_path / "lib")]
    )
    service = PosterService(config, database, tmp_path / "thumbs", MediaSources(database, config))
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: service._thumbnail_sync(media_id), range(4)))
    assert len(set(results)) == 1 and results[0].parent == tmp_path / "thumbs"
    assert [path.name for path in (tmp_path / "thumbs").iterdir()] == [results[0].name]
