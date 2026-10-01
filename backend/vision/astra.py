# -*- coding: utf-8 -*-
"""Orbbec Astra Pro 结构光深度相机（Astra SDK C API）。

**为什么不用 OpenNI2**：这台是 Astra Pro，由两个 USB 设备组成——
  · PID_0403  ORBBEC Depth Sensor（结构光深度，厂商私有接口）
  · PID_0501  Astra Pro HD Camera（RGB，走 UVC，OpenCV 看到的就是它）
实测 OpenNI2（含 SDK 自带的 orbbec.dll 驱动）**枚举到 0 个设备**，
`primesense` 这个 Python 封装在 Python 3.13 上也直接 import 失败（CEnum 元类报错）。
厂商自己的 C++/C 示例（DepthReaderPoll）却能正常出深度帧，
所以这里直接 ctypes 调 **Astra SDK 的 C 接口**。

**符号分散在两个 DLL**（实测出来的，不看文档猜）：
  · astra_core.dll —— initialize / terminate / update / streamset / reader / stream_start
  · astra.dll      —— reader_get_depthstream / frame_get_depthframe / depthframe_get_data_ptr / hfov
另外 `astra_depthframe_get_width|height` **根本没导出**，帧尺寸只能由数据字节数反推
（16bit 单通道：像素数 = byteLength / 2）。

深度值单位是**毫米**，0 表示无效（无回波）。
"""
from __future__ import annotations

import ctypes
import os
import time
from typing import Optional

# 注意：**不要**在模块顶层 import numpy。
# numpy 只在 read()/depth_at() 里真正用到，而本模块要在没有第三方依赖的环境里
# 也能被 import（CI 的静态检查就是在 pip install 之前跑的），
# 顶层 import 会让 check_vision.py 在 CI 里直接 ModuleNotFoundError。

# Astra 深度帧的常见分辨率（按面积反推，见模块 docstring 的说明）
_KNOWN_SHAPES = {
    640 * 480: (480, 640),
    320 * 240: (240, 320),
    1280 * 960: (960, 1280),
}


class AstraUnavailable(Exception):
    """SDK 缺失或设备不可用。上层据此关掉深度、退回纯视觉检测。"""


class AstraDepth:
    """Astra Pro 结构光深度。open() 失败时抛 AstraUnavailable，绝不静默坏掉。"""

    def __init__(self, sdk_dir: str, warmup_timeout_s: float = 5.0):
        self.sdk_dir = sdk_dir
        self.warmup_timeout_s = warmup_timeout_s
        self._core = None
        self._depth = None
        self._reader = None
        self._streamset = None
        self._opened = False
        # 水平视场角（度）。open() 成功后填真实值；没打开时保持 None，
        # 这样 bearing_deg() 无论何时被调用都不会 AttributeError。
        self.hfov_deg = None

    # ---------- 符号解析 ----------
    @staticmethod
    def _bind(dll, name, argtypes, restype=ctypes.c_int):
        """按名字取符号；缺失就抛，避免调用时才炸出难懂的 AttributeError。"""
        try:
            fn = getattr(dll, name)
        except AttributeError as exc:  # noqa: PERF203
            raise AstraUnavailable(
                f"Astra SDK 缺少符号 {name}（SDK 版本不匹配？）") from exc
        fn.argtypes = argtypes
        fn.restype = restype
        return fn

    def open(self) -> bool:
        bin_dir = self.sdk_dir
        core_path = os.path.join(bin_dir, "astra_core.dll")
        astra_path = os.path.join(bin_dir, "astra.dll")
        for p in (core_path, astra_path):
            if not os.path.isfile(p):
                raise AstraUnavailable(f"找不到 {os.path.basename(p)}（检查 VISION_ASTRA_SDK_DIR）")

        # SDK 的插件（orbbec.dll 等）靠 PATH 找同级依赖
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(bin_dir)
        except (AttributeError, OSError):
            pass

        P, U32, INT = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        try:
            core = ctypes.CDLL(core_path)
            depth = ctypes.CDLL(astra_path)
        except OSError as exc:
            raise AstraUnavailable(f"载入 Astra SDK 失败：{exc}") from exc

        self._core, self._depth = core, depth

        # ---- astra_core.dll ----
        astra_initialize = self._bind(core, "astra_initialize", [])
        astra_update = self._bind(core, "astra_update", [])
        streamset_open = self._bind(core, "astra_streamset_open", [ctypes.c_char_p, ctypes.POINTER(P)])
        reader_create = self._bind(core, "astra_reader_create", [P, ctypes.POINTER(P)])
        reader_open_frame = self._bind(core, "astra_reader_open_frame", [P, INT, ctypes.POINTER(P)])
        reader_close_frame = self._bind(core, "astra_reader_close_frame", [ctypes.POINTER(P)])
        stream_start = self._bind(core, "astra_stream_start", [P])

        # ---- astra.dll ----
        get_depthstream = self._bind(depth, "astra_reader_get_depthstream", [P, ctypes.POINTER(P)])
        frame_get_depthframe = self._bind(depth, "astra_frame_get_depthframe", [P, ctypes.POINTER(P)])
        data_ptr = self._bind(
            depth, "astra_depthframe_get_data_ptr",
            [P, ctypes.POINTER(ctypes.POINTER(ctypes.c_int16)), ctypes.POINTER(U32)])
        get_hfov = self._bind(depth, "astra_depthstream_get_hfov",
                              [P, ctypes.POINTER(ctypes.c_float)])

        self._f_update = astra_update
        self._f_open_frame = reader_open_frame
        self._f_close_frame = reader_close_frame
        self._f_get_depth = frame_get_depthframe
        self._f_data_ptr = data_ptr

        rc = astra_initialize()
        if rc != 0:
            raise AstraUnavailable(f"astra_initialize 失败（rc={rc}）")
        time.sleep(0.5)

        sensor = P()
        rc = streamset_open(b"device/default", ctypes.byref(sensor))
        if rc != 0:
            raise AstraUnavailable(f"astra_streamset_open 失败（rc={rc}）：深度设备没被识别到")
        self._streamset = sensor

        reader = P()
        rc = reader_create(sensor, ctypes.byref(reader))
        if rc != 0:
            raise AstraUnavailable(f"astra_reader_create 失败（rc={rc}）")
        self._reader = reader

        ds = P()
        rc = get_depthstream(reader, ctypes.byref(ds))
        if rc != 0:
            raise AstraUnavailable(f"取 depth stream 失败（rc={rc}）")
        self._stream = ds

        # 水平视场角：把像素列换算成方位角时要靠它（比拿焦距更准，官方给的就是这个）
        hf = ctypes.c_float()
        if get_hfov(ds, ctypes.byref(hf)) == 0 and hf.value > 0:
            self.hfov_deg = hf.value * 57.29577951308232

        rc = stream_start(ds)
        if rc != 0:
            raise AstraUnavailable(f"astra_stream_start 失败（rc={rc}）")

        self._opened = True
        return True

    # ---------- 读帧 ----------
    def read(self) -> Optional["np.ndarray"]:
        """返回 (H,W) uint16 深度图（毫米，0=无效），暂无帧返回 None。"""
        import numpy as np   # 延迟导入，见模块顶部说明

        if not self._opened:
            return None
        self._f_update()
        frame = ctypes.c_void_p()
        if self._f_open_frame(self._reader, 0, ctypes.byref(frame)) != 0:
            return None
        try:
            df = ctypes.c_void_p()
            if self._f_get_depth(self._reader_frame_arg(frame), ctypes.byref(df)) != 0:
                return None
            ptr = ctypes.POINTER(ctypes.c_int16)()
            blen = ctypes.c_uint32()
            if self._f_data_ptr(df, ctypes.byref(ptr), ctypes.byref(blen)) != 0:
                return None
            if not ptr or blen.value == 0:
                return None
            shape = _KNOWN_SHAPES.get(blen.value // 2)
            if shape is None:
                # 未知分辨率：按 4:3 反推，至少不崩
                px = blen.value // 2
                h = int((px * 3 / 4) ** 0.5)
                w = px // h if h else 0
                shape = (h, w)
            h, w = shape
            arr = np.ctypeslib.as_array(ptr, shape=(h * w,)).reshape(h, w)
            return arr.copy()      # 必须拷贝：帧关掉后这块内存就失效了
        finally:
            self._f_close_frame(ctypes.byref(frame))

    def _reader_frame_arg(self, frame):
        return frame

    def bearing_deg(self, x_pixel: int, width: int) -> float:
        """像素列 → 方位角（度），负=左。用官方 hfov 线性换算。"""
        if not self.hfov_deg or width <= 0:
            return 0.0
        # 图像中心为 0；hfov 是全角，所以每像素 = hfov/width
        return (x_pixel - width / 2.0) * (self.hfov_deg / width)

    @staticmethod
    def depth_at(depth: "np.ndarray", x: int, y: int, window: int = 10) -> Optional[float]:
        """取 (x,y) 邻域内有效深度的中位数，返回米；全无效返回 None。

        用中位数而不是均值：结构光在边缘/反光处会有少量离谱的野值，
        均值会被它们带偏。mask 掉 0（无回波）再算。
        """
        import numpy as np   # 延迟导入，见模块顶部说明

        if depth is None:
            return None
        h, w = depth.shape[:2]
        x1, x2 = max(0, x - window), min(w, x + window + 1)
        y1, y2 = max(0, y - window), min(h, y + window + 1)
        roi = depth[y1:y2, x1:x2]
        valid = roi[roi > 0]
        if valid.size == 0:
            return None
        return round(float(np.median(valid)) / 1000.0, 2)

    def close(self):
        # 顺序要紧：先停流、销毁 reader，最后 terminate，否则可能残留句柄
        for name, holder in (("astra_stream_stop", getattr(self, "_stream", None)),):
            if holder is None:
                continue
            try:
                fn = getattr(self._core, name)
                fn.argtypes = [ctypes.c_void_p]
                fn(holder)
            except Exception:
                pass
        self._opened = False
        for fn_name, obj in (("astra_reader_destroy", self._reader),
                             ("astra_streamset_close", self._streamset)):
            if obj is None:
                continue
            try:
                fn = getattr(self._core, fn_name)
                fn.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
                fn(ctypes.byref(obj))
            except Exception:
                pass
        self._reader = self._streamset = self._stream = None
        try:
            self._core.astra_terminate()
        except Exception:
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass