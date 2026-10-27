# -*- coding: utf-8 -*-
"""远控画面服务：采集 → 编码 → 供手机端拉取。

对外只暴露一个 GET 接口 /api/remote/frame，手机端用 <img> 直接拉，
浏览器原生就能解码，不需要播放器。

分辨率策略（网易 UU 式）
------------------------
不管电脑是 2K 还是 4K，远控时都可以「降成跟手机一样的分辨率」：
  * 自适应：按手机短边等比缩放（不放大超过电脑原始分辨率）
  * 预设档：480p / 720p / 1080p / 1440p / 原始分辨率
  * 用户可在手机端随时切换，立刻生效

画面尺寸变化时，手机端的点击坐标映射也要跟着变：
    屏幕真实坐标 = 画面内相对坐标 ×（虚拟屏幕宽/画面宽）
所以这里每次都把当前的「画面尺寸 / 屏幕原尺寸」一起发给前端。
"""
from __future__ import annotations

import threading
import time

from . import config, screencap


class ScreenService:
    """屏幕画面服务。单例，按需采集，带节流。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_bytes: bytes = b""
        self._last_ts: float = 0.0
        self._last_w: int = 0
        self._last_h: int = 0
        self._mode: str = "auto"
        self._client_w: int = 0
        self._client_h: int = 0
        self._fps: float = 0.0
        self._frames: int = 0
        self._fps_mark: float = time.time()

    # -- 配置 ----------------------------------------------------------
    def set_mode(self, mode: str, client_w: int = 0, client_h: int = 0) -> None:
        with self._lock:
            self._mode = (mode or "auto").strip() or "auto"
            if client_w > 0:
                self._client_w = int(client_w)
            if client_h > 0:
                self._client_h = int(client_h)
            # 换了模式，缓存的画面尺寸就不再有效
            self._last_ts = 0.0

    def status(self) -> dict:
        sw, sh = screencap.screen_size()
        with self._lock:
            mode = self._mode
            cw, ch = self._client_w, self._client_h
            lw, lh = self._last_w, self._last_h
            fps = self._fps
        tw, th = screencap.resolve_target(mode, cw, ch, sw, sh)
        return {
            "mode": mode,
            "presets": [{"id": p[0], "label": p[1]} for p in screencap.PRESETS],
            "screen": {"w": sw, "h": sh},
            "client": {"w": cw, "h": ch},
            "target": {"w": tw, "h": th},
            "frame": {"w": lw, "h": lh},
            "fps": round(fps, 1),
            "jpeg": screencap._encode_jpeg.__module__ is not None,
        }

    # -- 取帧 ----------------------------------------------------------
    def frame(self, max_age: float = 0.0, min_interval: float = 0.04) -> tuple[bytes, str]:
        """返回 (图像字节, content-type)。

        min_interval：两次采集之间的最小间隔，避免手机疯狂拉取把 CPU 打满。
        max_age：缓存的最大有效期（秒）；0 表示总是重新采集。
        """
        now = time.time()
        with self._lock:
            if (self._last_bytes and max_age > 0
                    and now - self._last_ts <= max_age):
                return self._last_bytes, self._content_type()
            if now - self._last_ts < min_interval:
                return self._last_bytes, self._content_type()
            mode = self._mode
            cw, ch = self._client_w, self._client_h

        sw, sh = screencap.screen_size()
        tw, th = screencap.resolve_target(mode, cw, ch, sw, sh)
        quality = 72 if tw <= 1280 else 62
        try:
            data = screencap.get_capture().capture(tw, th, quality=quality)
        except Exception:
            data = b""

        if not data:
            with self._lock:
                return self._last_bytes, self._content_type()

        with self._lock:
            self._last_bytes = data
            self._last_ts = time.time()
            self._last_w, self._last_h = tw, th
            self._frames += 1
            el = time.time() - self._fps_mark
            if el >= 1.0:
                self._fps = self._frames / el
                self._frames = 0
                self._fps_mark = time.time()
        return data, self._content_type()

    @staticmethod
    def _content_type() -> str:
        return "image/jpeg"

    # -- 坐标映射 ------------------------------------------------------
    @staticmethod
    def map_point(rel_x: float, rel_y: float,
                  frame_w: int, frame_h: int) -> tuple[int, int]:
        """把画面内的相对坐标（0~1）映射成电脑屏幕的真实像素坐标。

        画面是整块虚拟屏幕等比缩放来的，所以直接按比例还原即可。
        """
        vx, vy, vw, vh = screencap.virtual_screen_rect()
        if frame_w <= 0 or frame_h <= 0:
            return (int(rel_x * vw), int(rel_y * vh))
        px = vx + max(0.0, min(1.0, rel_x)) * vw
        py = vy + max(0.0, min(1.0, rel_y)) * vh
        return (int(px), int(py))


service = ScreenService()
