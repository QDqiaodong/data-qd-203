# 显式卷积码遥测块

这是离线遥测编码/解码数据API，不连接设备、不声称兼容某行业无线协议。输入constraint为2..7，generators为2..4个不同正整数掩码且<2^constraint；全部参数由调用者提供，不带标准码表。移位约定：register=(state<<1)|input_bit，输出依次为每个(register & generator)的置位数模2，下一状态为register低constraint-1位，初始状态0。code.encode同时输出码字与状态路径。

frames为1..20个唯一id，每个payload_length=1..500、terminated布尔、received数组。terminated=true时发送payload后补constraint-1个0并强制最终状态0；false不补尾且任意终态。received长度必须恰等于(负载长度+尾长)*生成多项式数，元素严格为整数0/1或null（擦除）；不能用bool。每帧独立，不延续前帧状态，不自动识别边界。

解码找全部合法状态路径中最小硬判汉明距离，null不计代价。返回字典序最小的负载、optimal_paths精确计数和ambiguous标志；多条最优路径的码字即使相同也不合并路径。状态、重编码、修正的已知位下标及擦除下标按原0起展开序列保留，distance只含已知错误。不声称歧义选中的负载已确认正确，也不以距离0保证真实信源正确；没有CRC校验或误码能力认证。

所有步骤数有界，使用Viterbi状态递推并保留每状态最优路径计数与字典序代表。没有软判概率、打孔、交织、同步或连续流恢复；未来帧结构、交织和打孔可接入bit index、帧身份和状态证据。样例参考负载101，constraint3/generators7,5，终止码字1110001011，将首位翻转后唯一最优仍101。

API输入错误抛ValueError，整个结果成功计算后可用io.publish创建新文件；拒绝已有目标和链接。examples为固定集成演示，不是通用CLI工具。

## 可重开归档API（archive.Archive）

`Archive(path)` 以目录路径打开归档（不存在则创建），支持上下文管理器；同库按顺序调用，进程间以目录内排他flock互斥，不支持同进程并发写。状态是单个`state.json`，经临时文件、fsync、`os.replace`与目录fsync原子替换；只使用标准库。三个入口：

- `submit(request, *, crash=None) -> receipt`：原子提交一批。
- `evidence(frame_id) -> report`：按帧查当前证据；未知帧抛`KeyError`。
- `read_changes(cursor, page_size) -> {"cursor":int,"changes":[...]}`：从非负游标分页读取严格大于游标的有序变更，页大小为1..1000的正整数；空页不推进游标。
- 另有 `receipt(batch_id)` 取已存批次回执（无则`KeyError`）、`close()`。输入、查询与回执全部是普通JSON数据结构。

批次请求字段固定为 `batch_id` 与 `updates`：`batch_id` 沿用ident约束（`[A-Za-z0-9_:-]{1,64}`）；`updates`为1..160项，最多涉及20个不同帧，顺序有意义。每项字段固定为 `frame_id`、`capture_id`（均为ident）、正整数`revision`（1..2^31-1）、`action`（`write`/`revoke`）以及帧描述 `constraint`、`generators`、`payload_length`、`terminated`，口径同上并沿用2..7、2..4个不同掩码、负载1..500。帧描述首次登记后不可变；同批或与已存描述不一致即拒绝整批。`write`必须带`received`，长度与符号/擦除规则同单帧契约（严格0/1整数或null，禁止bool）；`revoke`禁止带`received`。每帧最多8个接收身份（含已撤销），批内同一(frame_id,capture_id)只允许一次。

版本语义按接收身份比较：只取最大版本的动作；同版本同内容为空操作，同版本异内容拒绝整批；较旧版本不改变状态，先到的撤销也会记住版本，晚到的旧`write`不能复活该身份。格式与帧描述校验对过期版本照常执行，不因版本旧而跳过。

每帧把当前所有未撤销副本按原展开码元下标合并：候选路径代价是它与各副本全部非擦除观测的不符总数；同一下标两份冲突观测都计入，不做按位投票。`evidence`返回`distance`（最小总代价）、`optimal_paths`（精确最优路径数，Python大整数）、`ambiguous`、字典序最小代表`payload`及其`reencoded`、`state_path`、`selected_final_state`；`effective_payload`仅在恰好一条最优路径时等于`payload`否则null；逐有效副本给`capture_id`、`revision`、相对代表的`mismatch_indices`与`erasure_indices`，另有`has_valid_copy`与当前`published_seq`（无有效发布为null）。无有效副本按全擦除计分；未知帧与已登记但无有效副本可区分（后者返回报告且`has_valid_copy=false`）。支持负载上限500位，不枚举全部负载。

下游发布只认唯一最优路径。比较整批提交前后，每帧至多一条变更：无→有`write`、有效负载变为另一值`replace`、有→无`revoke`；唯一负载不变时即使版本、代价或不符位置变化也只更新证据、不发事件。批内中间译码状态不产生事件。变更保存全库从1起连续递增的`seq`、`frame_id`、`type`、`previous_seq`（此前无有效发布为null）；`write`/`replace`带`payload`，`revoke`不带。同批多事件按frame_id字典序分配序号。已发历史条目不可改写，也不能从当前证据重建旧负载。

观测与版本、下游变更、批次回执在一次原子提交内一起生效。回执至少含`batch_id`与本批实际`changes`（无变更也保存）。重试规则：先做格式校验，再以字段顺序无关、数组顺序有意义的规范化JSON比较请求内容；相同`batch_id`相同内容直接返回原回执（在重新做业务判断之前），相同ID异内容拒绝。同ID异内容、格式错误、新批次业务冲突均拒绝，不留部分更新、回执或跳号。重开后可继续查证据、拉变更或安全重试；只认完整提交，不负责修复介质损坏。`crash`参数（`staged`/`replaced`/`synced`）仅供测试在持久化边界模拟进程中断。
