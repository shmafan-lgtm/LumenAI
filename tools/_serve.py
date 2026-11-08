"""后台拉起 gpt-oss-20b 并保活（禁用闲置释放，供测试期间常驻）。

只管自己 spawn 的 llama-server；绝不动同机其他进程。
"""
import os
import sys
import time

# 必须在 import core.llamacpp 之前设好：禁用闲置释放，避免测试中途被杀
os.environ.setdefault("LUMEN_MODEL_IDLE_MINUTES", "0")
os.environ.setdefault("LUMEN_MODEL_CTX", "16384")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.llamacpp as ll  # noqa: E402

if __name__ == "__main__":
    print("ensuring model...", flush=True)
    st = ll.ensure(ctx=16384)
    print("snapshot:", st.get("running"), st.get("backend"), st.get("model"), flush=True)
    # 常驻：保持进程活着，直到被外部停止
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        pass
