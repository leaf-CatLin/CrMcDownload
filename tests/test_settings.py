"""持久数据目录默认值与自动创建。"""

from pathlib import Path

from server_mirror.config import DEFAULT_DATA_ROOT, Settings


def test_from_env_defaults_to_db_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in (
        "MIRROR_DATA_DIR",
        "MIRROR_DB_PATH",
        "MIRROR_POPULAR_DATA",
        "MIRROR_SKINS_DIR",
        "MIRROR_PLUGIN_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert settings.data_root == DEFAULT_DATA_ROOT
    assert settings.db_path == DEFAULT_DATA_ROOT / "mirror.db"
    assert settings.popular_data == DEFAULT_DATA_ROOT / "popular_servers.json"
    assert settings.skins_dir == DEFAULT_DATA_ROOT / "skins"
    assert settings.plugin_dir == DEFAULT_DATA_ROOT / "plugins"


def test_from_env_data_dir_drives_subpaths(monkeypatch):
    monkeypatch.setenv("MIRROR_DATA_DIR", "E:/mirror-data")
    settings = Settings.from_env()
    assert settings.db_path == Path("E:/mirror-data/mirror.db")
    assert settings.skins_dir == Path("E:/mirror-data/skins")
    assert settings.plugin_dir == Path("E:/mirror-data/plugins")
    # 单独覆盖优先
    monkeypatch.setenv("MIRROR_DB_PATH", "E:/other/custom.db")
    assert Settings.from_env().db_path == Path("E:/other/custom.db")


def test_prepare_creates_all_directories(tmp_path):
    settings = Settings(
        data_root=tmp_path / "data",
        db_path=tmp_path / "data" / "db" / "mirror.db",
        popular_data=tmp_path / "data" / "popular_servers.json",
        skins_dir=tmp_path / "data" / "skins",
        plugin_dir=tmp_path / "data" / "plugins",
    )
    settings.prepare()
    assert (tmp_path / "data").is_dir()
    assert settings.db_path.parent.is_dir()
    assert settings.skins_dir.is_dir()
    assert settings.plugin_dir.is_dir()
    # 幂等
    settings.prepare()


def test_host_port_ipv4():
    settings = Settings(listen="127.0.0.1:8081")
    assert settings.host == "127.0.0.1"
    assert settings.port == 8081


def test_host_port_ipv6_bracketed():
    settings = Settings(listen="[::1]:8081")
    assert settings.host == "::1"
    assert settings.port == 8081


def test_host_port_ipv6_multi_colon_bracketed():
    settings = Settings(listen="[2001:db8::1]:443")
    assert settings.host == "2001:db8::1"
    assert settings.port == 443


def test_host_bare_ipv6_no_brackets_returns_full_address():
    # 裸 IPv6（未加括号）无法拆出端口，应把整串作为 host、端口回落 0，而不是截坏地址
    settings = Settings(listen="fe80::1")
    assert settings.host == "fe80::1"
    assert settings.port == 0
