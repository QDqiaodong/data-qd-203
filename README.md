# data-qd-203 · 遥测卷积编码与纠错解码

显式移位约定的卷积编码器和有终态约束的最小汉明距离解码器，保留最优路径数、歧义标志、重编码和逐位来源。

本仓主要交付为Python业务API与下游可消费的数据，不以通用CLI、报表或后台模板为产品。可观察字段、单位、身份、上限及失败边界见 [数据契约](docs/contract.md)。

## 独立安装与验证

Python >=3.11，实际验证CPython3.12.14、uv0.12.19。运行和测试仅标准库；构建固定setuptools80.9.0、wheel0.45.1。各仓独立 `.venv`，可以共享公共下载缓存，不共享site-packages。

从本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/python -I examples/run_sample.py
```

以上命令在本地现有公共缓存上真实执行通过，无需新增下载。新机器缓存为空时，先准备匹配Python与锁定公共构建包，或在可联网安装环境移除安装命令中的offline；API、测试与样例运行不需要网络。示例创建 `run/sample.json`，目标已存在会拒绝覆盖；重复运行请先自行保留产物并选择新的演示工作副本。

## 使用与实际结果

```python
from telemetry_trellis import process
from telemetry_trellis.io import load, publish

result = process(load("fixtures/input.json"))
publish("run/another-sample.json", result)
```

参考负载101的终止码首位被翻转后，唯一最优解仍为101，汉明距离1、修正下标[0]。

安装后的样例摘要：

```json
{"ambiguous": 0, "erasures": 0, "frames": 1, "hamming_distance": 1}
```

已通过 6 项业务测试，另实际核对已有目标、符号链接目标、JSON重复键、非法业务输入和临时发布文件清理。数值和排序语义见契约，不以文件哈希代替业务结果断言。

## 可落盘、可重开的归档 API

`telemetry_trellis.Archive` 在目录上提供三个入口：`submit` 原子提交观测批次（含版本仲裁、回执与下游变更）、`evidence` 按帧查多副本合并证据、`read_changes` 从游标分页拉有序变更。状态经临时文件+fsync+`os.replace`原子落盘，重开后证据、历史变更与批次回执均保留；相同 `batch_id` 的相同请求安全重试并返回原回执。完整字段、版本与发布语义见 [数据契约](docs/contract.md)。

```python
from telemetry_trellis import Archive

with Archive("run/telemetry-archive") as archive:
    receipt = archive.submit({
        "batch_id": "batch-0001",
        "updates": [{
            "frame_id": "frame-7", "capture_id": "station-A",
            "revision": 1, "action": "write",
            "constraint": 2, "generators": [3, 1],
            "payload_length": 1, "terminated": False,
            "received": [0, 1],
        }],
    })
    evidence = archive.evidence("frame-7")
    page = archive.read_changes(cursor=0, page_size=100)
```

离线合成数据演示（无设备连接）：

```sh
PYTHONPATH=src python3 examples/archive_demo.py
```

测试新增多站合并、撤销与版本仲裁、变更游标与发布边界、批次原子性（含提交前中断与应答丢失重开重试）、终止帧最大负载、全擦除精确路径计数等共 31 项归档/合并测试（含独立状态路径枚举Oracle），原有 `process`、编码器与文件 IO 的 6 项测试保持通过，合计 37 项。运行时仍仅依赖标准库，无第三方依赖。

## 当前边界与来源

不含真实协议码表、软判概率、交织、同步或CRC。歧义时选择字典序代表但不能当确认信源。

全部输入样例原创合成，MIT许可，见 [来源](fixtures/SOURCES.md)。仅复用MIT通用构建、字段校验与原子新建IO脚手架；业务实现独立编写。当前精确公共依赖镜像准备查询结果为 `true`；这不影响已通过的宿主离线独立环境验证，也不构成容器验收记录。本次没有构建或启动容器。
