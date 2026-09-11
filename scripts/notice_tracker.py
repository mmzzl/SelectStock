"""公告下载去重/增量跟踪：分段 Bitmap 记录已下载公告 ID。

适用前提：公告源的公告 ID 是单调递增的整数（东财 art_code 尾部数字 / 巨潮 annId 等），
全局唯一即可，无需连续。

配置：从 stock_selector/config/config.yaml 读取 notice 节：
    notice:
      track_dir: "data/downloaded_notices"   # bitmap 去重跟踪目录
      detail_dir: "data/notices"             # 公告详情落盘目录（爬虫用）
      bucket_bits: 20
      log_dir: "logs"

数据结构（bitmap 分桶，惰性创建）：
    {track_dir}/
        state.json                        # 元数据 {version, bucket_bits, max_id, count}
        buckets/{bucket_id}.bm            # 每个桶一个 bitmap 文件
                                          #   bucket_id = id >> BUCKET_BITS
                                          #   桶内偏移 = id & 0xFFFFF (bucket_bits=20)
                                          #   bit 置位 => 该公告已成功下载

为什么分桶：
  公告 ID 稀疏（删除/合并会留空洞，max 可达上亿），单一大位图浪费空间；
  分桶后只有真实下载过的段才产生文件（惰性创建），桶文件 ~128KB，可独立刷盘、
  独立加锁、并发写安全。

日志：输出到控制台 + 文件 {log_dir}/notice_tracker_YYYYMMDD.log。

增量下载主循环（用法示意）：
    tracker = DownloadedTracker(cfg_track_dir)
    for doc in fetch_notice_list(pn, rn):          # 列表分页
        nid = int(doc["art_code"][-12:])            # 从公告标识解析 ID
        if tracker.has(nid):
            continue                                # 已下载过 -> 跳过
        download_detail(doc)                        # 下载详情并落盘
        tracker.mark(nid)                           # 成功后置位
    tracker.flush()                                  # 刷盘（或 mark 后定期刷）
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "stock_selector" / "config" / "config.yaml"

logger = logging.getLogger("notice_tracker")

BUCKET_BITS = 20           # 每个桶 2^20 个 bit = 128KB bitmap
_BUCKET_SIZE = 1 << BUCKET_BITS
_BUCKET_MASK = _BUCKET_SIZE - 1
_STATE_VERSION = 1
_FLUSH_EVERY = 256         # 每积累 N 个 mark 后刷盘一次


def load_config() -> dict:
    """读取 config.yaml 中的 notice 配置。"""
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    return cfg.get("notice", {}) or {}


def setup_logging(log_dir: Path) -> None:
    """日志：控制台 + 按日滚动写入文件。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    fh = logging.FileHandler(log_dir / f"notice_tracker_{time.strftime('%Y%m%d')}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)


class DownloadedTracker:
    """用分桶 bitmap 记录已下载的公告 ID，支持增量去重与恢复。"""

    def __init__(self, root: Path, bucket_bits: int = BUCKET_BITS):
        self.root = Path(root)
        self.bucket_bits = bucket_bits
        self.bucket_size = 1 << bucket_bits
        self.bucket_mask = self.bucket_size - 1
        self._lock = threading.Lock()
        self._buckets: dict[int, bytearray] = {}   # bucket_id -> bitmap
        self._dirty: set[int] = set()
        self.max_id = 0
        self.count = 0
        self._pending = 0
        self._load_state()

    # -- 状态持久化 ------------------------------------------------------
    def _state_path(self) -> Path:
        return self.root / "state.json"

    def _bucket_dir(self) -> Path:
        return self.root / "buckets"

    def _bucket_path(self, bucket_id: int) -> Path:
        return self._bucket_dir() / f"{bucket_id}.bm"

    def _load_state(self) -> None:
        self._bucket_dir().mkdir(parents=True, exist_ok=True)
        if not self._state_path().exists():
            return
        meta = json.loads(self._state_path().read_text(encoding="utf-8"))
        if meta.get("version") != _STATE_VERSION:
            raise ValueError(f"state.json 版本不兼容: {meta.get('version')}")
        self.max_id = int(meta.get("max_id", 0))
        self.count = int(meta.get("count", 0))

    def save_state(self) -> None:
        meta = {
            "version": _STATE_VERSION,
            "bucket_bits": self.bucket_bits,
            "max_id": self.max_id,
            "count": self.count,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = self._state_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._state_path())

    # -- bitmap 读写 ------------------------------------------------------
    def _load_bucket(self, bucket_id: int) -> bytearray:
        """惰性加载桶；不存在则创建空 bitmap。"""
        buf = self._buckets.get(bucket_id)
        if buf is None:
            path = self._bucket_path(bucket_id)
            buf = bytearray(path.read_bytes()) if path.exists() else bytearray()
            self._buckets[bucket_id] = buf
        return buf

    def _bit_pos(self, nid: int) -> tuple[int, int]:
        """nid -> (bucket_id, bit_index)。"""
        bucket_id = nid >> self.bucket_bits
        bit = nid & self.bucket_mask
        return bucket_id, bit

    def has(self, nid: int) -> bool:
        """该公告是否已下载。"""
        bucket_id, bit = self._bit_pos(nid)
        buf = self._buckets.get(bucket_id)
        if buf is None:
            buf = self._load_bucket(bucket_id)
        byte_idx, bit_idx = bit >> 3, bit & 7
        # 越界视为未下载（惰性桶可能比标记位短）
        return byte_idx < len(buf) and bool(buf[byte_idx] & (1 << bit_idx))

    def mark(self, nid: int) -> None:
        """标记公告已下载。幂等。"""
        with self._lock:
            bucket_id, bit = self._bit_pos(nid)
            buf = self._load_bucket(bucket_id)
            byte_idx, bit_idx = bit >> 3, bit & 7
            if byte_idx >= len(buf):
                buf.extend(b"\x00" * (byte_idx + 1 - len(buf)))
            if not (buf[byte_idx] & (1 << bit_idx)):
                buf[byte_idx] |= 1 << bit_idx
                self.count += 1
            self._dirty.add(bucket_id)
            self.max_id = max(self.max_id, nid)
            self._pending += 1
            if self._pending >= _FLUSH_EVERY:
                self._pending = 0
                self.flush()

    # -- 刷盘 ------------------------------------------------------------
    def flush(self) -> None:
        """把脏桶原子写盘并保存状态。"""
        for bucket_id in list(self._dirty):
            buf = self._buckets.get(bucket_id)
            if buf is None:
                continue
            target = self._bucket_path(bucket_id)
            tmp = target.with_suffix(".bm.tmp")
            tmp.write_bytes(buf)
            os.replace(tmp, target)
            self._dirty.discard(bucket_id)
        self.save_state()

    def size_on_disk(self) -> int:
        """已占磁盘字节数（含 bucket 文件与 state）。"""
        total = sum(f.stat().st_size for f in self._bucket_dir().glob("*.bm"))
        total += self._state_path().stat().st_size if self._state_path().exists() else 0
        return total


# -- 增量下载辅助 ----------------------------------------------------------
def extract_numeric_id(art_code: str) -> int:
    """从东财 art_code（如 'AN202404191638842361'）提取可用于位图的整数 ID。

    规则：取末尾最多 15 位数字；不足 15 位时左补 0，保证同源 ID 单调性。
    """
    digits = "".join(ch for ch in art_code if ch.isdigit())
    if not digits:
        raise ValueError(f"公告标识无可解析数字: {art_code!r}")
    return int(digits[-15:])


def skip_duplicates(list_docs, tracker: DownloadedTracker):
    """过滤掉已下载的公告（保持顺序），供列表页增量使用。"""
    for doc in list_docs:
        nid = extract_numeric_id(doc["art_code"])
        if not tracker.has(nid):
            yield doc, nid


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        t = DownloadedTracker(Path(tmp))
        ids = [199_000_008, 199_000_009, 200_000_002_912_345]
        for i in ids:
            t.mark(i)
        t.flush()
        t2 = DownloadedTracker(Path(tmp))          # 模拟重启恢复
        assert t2.has(ids[0]) and t2.has(ids[-1])
        assert not t2.has(ids[0] + 3)              # 未下载的 ID
        print(f"OK count={t2.count} max_id={t2.max_id} disk={t2.size_on_disk()}B "
              f"buckets={len(list(t2._bucket_dir().glob('*.bm')))}")