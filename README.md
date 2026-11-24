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

## 离线擦除位返读 API（readback）

`plan_readback` 在已有帧边界的捕获上决定该申请哪些返读窗口：先按单帧口径得到原最小距离与精确最优路径数，已唯一返回`unique`、原最优负载超过32个返回`too_many_candidates`、配额内无可行集合返回`unschedulable`，否则在配额内选出总费用最低、同费用ID列表字典序最小的窗口集合（`scheduled`）。区分对象是全部原最优负载而非重编码合并集合；任意一对负载必须被至少两个选中窗口在其请求的擦除坐标上区分，以承受任一已调度窗口整窗掉包。500位帧不枚举负载（前向Viterbi代价表+后向最优后缀回溯，仅在候选≤32时枚举负载），全擦除精确计数仍为`2**500`。

`scheduled`结果含接收端调度单`readback_order`（只含窗口ID、精确请求坐标、费用与总费用，不含原接收数组或候选负载）和自含的`continuation`续接资料；`schedule_id`由规范化完整请求派生，等价输入（字段或窗口目录换序）同标识，换证据或目录即换标识。续接资料可落盘后在新进程用`load_continuation`重载，`settle_readback`凭它与回包结算，不依赖旧对象、不查“最新捕获”。回包按窗口给最终结果（不可用且无位值，或请求坐标与严格0/1位值完全相等的一项）；漏报/多报窗口、未选窗口、越界坐标、跨调度资料、同坐标位值冲突一律拒绝且不产生部分结果。成功回包填入原擦除位后对整组合法路径重新译码，返回补入下标与完整单帧证据（距离、精确最优路径数、歧义、代表负载、状态路径、重编码、修正下标、剩余擦除下标）。字段与拒绝边界见 [数据契约](docs/contract.md)。

```python
from telemetry_trellis import plan_readback, settle_readback

plan = plan_readback({
    'capture_id': 'cap-1', 'constraint': 2, 'generators': [1, 2],
    'payload_length': 3, 'terminated': True,
    'received': [None, 0, 0, None, 0, None, 0, 0],
    'quota': 3,
    'windows': [
        {'id': 'w0', 'start': 0, 'end': 4, 'fee': 1},
        {'id': 'w1', 'start': 0, 'end': 1, 'fee': 3},
        {'id': 'w2', 'start': 3, 'end': 6, 'fee': 2},
    ],
})
# plan['selected_windows'] == ['w0', 'w2']，请求坐标分别为 [0, 3] 与 [3, 5]
settled = settle_readback(plan['continuation'], {
    'schedule_id': plan['schedule_id'],
    'windows': [
        {'window_id': 'w0', 'available': False},
        {'window_id': 'w2', 'available': True,
         'bits': {'3': 1, '5': 0}},
    ],
})
# settled['decode']['payload'] == [1, 0, 0]，唯一最优
```

离线合成演示（含落盘、新进程重载与结算）：

```sh
PYTHONPATH=src python3 examples/readback_demo.py
```

## 流式遥测提前交付 API（streaming）

`new_state`/`append`/`replace`/`confirm`/`finish` 面向“分片到达、采集端仍会更正、只有一个只前进的确认位置”的单条记录，在下游按负载顺序消费且不可撤回的前提下提前交付。每次确认后只用**全部码元都已确认的完整编码步**：对走到该步的每个可达状态，考察到达它的**所有**最低代价合法前缀，取负载位置共有的最长起始段，交付其中此前未交付的连续范围（给出绝对起点与新增位）。结束仅在全部收到并全部确认后进行，在完整合法路径上判全局最优，交付最优负载共有前缀的剩余连续位，并返回与一次性 `process` 一致的该帧证据（精确路径数、代表负载、终态、状态路径、重编码、距离、全局修正与擦除下标）。

交付后的完整编码步即退役：状态里不再保留或要求重传其原始观测，后续译码与诊断只靠已交付前缀、来源摘要与未退役块；`save_state`/`load_state` 让采集端在任意成功调用后把 JSON 状态交给新进程继续，已确认与仍可替换的边界、已交付位均不变。

```python
from telemetry_trellis import append, confirm, new_state, replace

state = new_state('rec-1', 2, [1, 2, 3], 4, False)
for fragment in ([0, None], [None, 0, 0], [0, 0], [0, 0]):
    state = append(state, fragment)['state']
result = confirm(state, 4)                     # 只确认第一个码组：不交付
state = replace(result['state'], 4, [1, 1])['state']  # 更正尚未确认的第二个码组
result = confirm(state, 9)                     # 交付 {'start': 0, 'bits': [1, 0], 'end': 2}
```

离线合成演示（分片、替换、确认切点、跨进程交接与最终证据）：

```sh
PYTHONPATH=src python3 examples/streaming_demo.py
```

## 双端累计流量的本机计量与参考时间对齐 API（metering / alignment）

`build_meter_facts(catalog, evidence)` 在不接触原始码元、不调用译码器的前提下，核验上游测点目录与已有单帧证据并生成本机计量事实：每份证据只消费 `id`、`payload`、`optimal_paths`、`ambiguous`，并与目录冻结的 SHA-256 摘要（紧凑 JSON `[表ID,滴答,id,payload,optimal_paths,ambiguous]` 的 UTF-8 哈希）逐一比对，未知帧、重复帧、字段非法或摘要不符一律拒绝整批；合法缺证与歧义保留为端点状态而非格式错误。每端按滴答排序后，目录中实际相邻的两个测点形成一条半开本机区间事实：两端唯一可读且计数不下降时给出精确增量（真零也算已知覆盖），否则给出区分 `missing_evidence`、`ambiguous_evidence`、`decreasing_reading` 的未知原因，不跳点、不推断回绕。

`align_meter_facts(facts, timing)` 复用同一批不可变的本机事实，按每端 0..4096 个锚点的允许连线做精确有理线性映射（禁用连线不插值不跨越、范围外不外推、单锚点无覆盖），把已知增量按原本机区间长度切开后在共同参考时间内归并配对：每个非空参考交集是一条带来源（两侧表、端点帧、锚点连线、实际本机子区间）的 `paired` 事实；未配对份额区分仅本机的 `uncalibrated` 与带参考范围的 `calibrated_unpaired`（不解释为对侧零流）。同一事实可换校时目录重复对齐；每条已知事实的子区间恰好覆盖原区间一次、份额之和精确等于原增量，未知事实不分摊。全部有理数以 `{"num","den"}` 无损 JSON 表达，输出顺序确定，工作量只随断点与输出片段数增长，不按时间跨度展开。

```python
from telemetry_trellis import (
    align_meter_facts, build_meter_facts, evidence_digest, process,
)
# 用现有公开 API 生成唯一译码帧，再模拟上游制作目录与摘要
frame = process({
    'constraint': 2, 'generators': [3, 1],
    'frames': [{'id': 'inlet-frame-1', 'payload_length': 8,
                'terminated': True, 'received': [...]}],
})['frames'][0]
digest = evidence_digest('bench-inlet', 6, 'inlet-frame-1', frame)
facts = build_meter_facts(catalog, evidence)          # 摘要核验 + 本机事实
aligned = align_meter_facts(facts, timing_catalogue)  # 可重复换校时目录
```

离线合成演示（验收几何、改锚点重对齐与精确份额）：

```sh
PYTHONPATH=src python3 examples/metering_demo.py
```

测试新增多站合并、撤销与版本仲裁、变更游标与发布边界、批次原子性（含提交前中断与应答丢失重开重试）、终止帧最大负载、全擦除精确路径计数等共 31 项归档/合并测试（含独立状态路径枚举Oracle），原有 `process`、编码器与文件 IO 的 6 项测试保持通过。返读 API 另增 24 项测试：覆盖配额/费用字典序选窗、整窗掉盘的两窗口区分保证、选窗与最优负载枚举的穷举Oracle、同码字不可调度、32候选边界、跨调度与全部非法回包拒绝、位值冲突不投票、拒绝时无部分结果且不改原输入、落盘后以 `python -I` 新进程重载结算、500位全擦除`2**500`与500位小量并列的实际调度/再译码，合计 61 项。流式 API 再增 23 项测试：直接复现四条验收故事（提前交付边界、不误用未确认步、逐码组确认、终止尾步切点、并列与退役），以每状态最低代价前缀的穷举Oracle核对提前交付与结束证据，覆盖非法偏移/提前结束/失败后接续、不可改的已确认前缀、新进程仅凭 JSON 状态接手、500位全擦除`2**500`且不交付、以及长记录分片确认中途交接。本机计量与参考时间对齐 API 再增 30 项测试：逐条复现三组验收故事（配对区间与两侧份额及来源、改锚点与禁用连线、缺证/歧义/摘要拒绝、错帧、下降读数与真零、部分校时覆盖、端点接触、10^12 微秒跨度下输出规模不增长），并以独立有理数 Oracle 做 40 组随机对齐与 300 组含禁用连线的压力核对，检查子区间无缝覆盖与份额守恒。运行时仍仅依赖标准库，无第三方依赖。

## 当前边界与来源

不含真实协议码表、软判概率、交织、同步或CRC。歧义时选择字典序代表但不能当确认信源。

全部输入样例原创合成，MIT许可，见 [来源](fixtures/SOURCES.md)。仅复用MIT通用构建、字段校验与原子新建IO脚手架；业务实现独立编写。当前精确公共依赖镜像准备查询结果为 `true`；这不影响已通过的宿主离线独立环境验证，也不构成容器验收记录。本次没有构建或启动容器。
