"""exact DR 的 Compose 源卷必须与生产服务使用同一物理来源。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_minio_and_redis_backup_mounts_share_service_sources():
    production = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    development = (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")

    for body in (production, development):
        assert "command: redis-server --appendonly yes" in body
        assert "redis-data:/data" in body
        assert "redis-data:/dr-source/redis:ro" in body
        assert "${MINIO_DATA_DIR:-minio-data}:/dr-source/minio:ro" in body
    assert "${MINIO_DATA_DIR:-minio-data}:/data" in production
    assert "  minio-data:\n" in development
