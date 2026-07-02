"""B站扫码登录前端契约守卫:确保 BiliLogin.vue 与后端 /api/bili/* 严格对齐。

无 JS 测试框架(宿主无 node 依赖),故以源文件静态断言守住契约字段名/路径,
防止前后端漂移。可在测试容器内随 pytest 运行。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMP = ROOT / "frontend" / "src" / "components" / "settings" / "BiliLogin.vue"
TYPES = ROOT / "frontend" / "src" / "types" / "index.ts"
SETTINGS = ROOT / "frontend" / "src" / "views" / "SettingsView.vue"

# 后端测试容器不挂载 frontend/,此时整体跳过,仅在前端源码可见时守契约。
pytestmark = pytest.mark.skipif(
    not COMP.parent.parent.exists(), reason="frontend 源码未挂载"
)


@pytest.fixture(scope="module")
def comp_src() -> str:
    return COMP.read_text(encoding="utf-8")


def test_component_exists():
    assert COMP.is_file(), "BiliLogin.vue 应存在于 components/settings"


@pytest.mark.parametrize(
    "path",
    [
        "/api/bili/status",
        "/api/bili/login/start",
        "/api/bili/login/poll?qrcode_key=",
        "/api/bili/logout",
    ],
)
def test_contract_paths_present(comp_src: str, path: str):
    assert path in comp_src, f"组件未使用契约路径 {path}"


@pytest.mark.parametrize("method", ["api.get", "api.post"])
def test_uses_api_wrapper(comp_src: str, method: str):
    assert method in comp_src, "应复用 useApi 的 get/post 封装"


@pytest.mark.parametrize(
    "state",
    ["waiting", "scanned", "confirmed", "expired"],
)
def test_poll_states_handled(comp_src: str, state: str):
    assert state in comp_src, f"未处理轮询状态 {state}"


def test_contract_field_names(comp_src: str):
    # 字段名严格对齐契约:start 返回 qr_png/qrcode_key/url,status 用 logged_in/uname。
    for field in ("qr_png", "qrcode_key", "logged_in", "uname"):
        assert field in comp_src, f"缺少契约字段 {field}"


def test_poll_interval_is_2s(comp_src: str):
    assert "2000" in comp_src, "轮询间隔应为 2s"


def test_clears_timer_on_unmount(comp_src: str):
    assert "onUnmounted" in comp_src and "clearInterval" in comp_src, "卸载时应清除轮询定时器"


def test_logout_button_path(comp_src: str):
    # 注销走 POST /api/bili/logout 后刷新 status。
    assert "logout" in comp_src and "refreshStatus" in comp_src


def test_types_declare_contract():
    src = TYPES.read_text(encoding="utf-8")
    for name in ("BiliStatus", "BiliLoginStart", "BiliLoginPoll", "BiliLoginState"):
        assert name in src, f"types 缺少 {name}"


def test_mounted_in_settings_view():
    src = SETTINGS.read_text(encoding="utf-8")
    assert "BiliLogin" in src and "components/settings/BiliLogin.vue" in src, "BiliLogin 未挂载到 SettingsView"
