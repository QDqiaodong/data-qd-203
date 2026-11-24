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

## 离线擦除位返读 API（readback）

`plan_readback(request)` 面向一个已有帧边界的`capture_id`（ident约束），在不连接设备、不改变现有单帧契约的前提下决定该申请哪些返读窗口；`settle_readback(continuation, reply, original_request=None)` 在另一处理进程凭保存的续接资料把实际回包并入发起时的捕获并按现有单帧口径重新译码。全部为普通JSON数据，仅用标准库。

### 发起请求与窗口

请求字段固定为 `capture_id`、`constraint`、`generators`、`payload_length`、`terminated`、`received`（参数范围、长度与严格0/1/null、生成元顺序、合法路径完全沿用单帧契约，布尔整数拒绝）、整数`quota`（0..16000，拒绝bool）和`windows`（1..16项）。每个窗口字段固定为 `id`（ident，全目录唯一）、整数`start`/`end`（0..码元总数，半开`[start,end)`非空且不越界，拒绝bool）与整数`fee`（1..1000，拒绝bool）。区间允许重叠，目录顺序没有业务含义；窗口实际请求坐标只是区间内在本次原始`received`中为null的位置，已知位永不返读，可能为空列表（该窗口仍合法但不区分任何候选）。

先用现有单帧Viterbi口径得到原最小汉明距离`distance`与精确最优路径数`optimal_paths`（Python大整数，500位全擦除帧可得到`2**500`，不枚举负载、不截断）。结果`status`四态可区分：

- `unique`：原输入已唯一最优，无需返读，不产生调度单。
- `too_many_candidates`：原最优**负载**数超过32（按去重后的负载计，多条同码字路径先去重；路径计数仍给精确值），不产生调度。
- `unschedulable`：候选可处理但配额内不存在满足保证的窗口集合，不下发近似结果。
- `scheduled`：给出费用最低的可行窗口集合。

无调度结果统一带原`distance`与`optimal_paths`，且`schedule_id`、`readback_order`、`continuation`为null、`selected_windows`为空、`total_fee`为0。

### 区分保证与选窗优化

需要互相区分的是**全部原最优负载**，不是按重编码合并后的集合；若两条最优负载的码字完全相同（任何擦除位都不可分），直接不可调度。对任意一对原最优负载，一个窗口仅当它请求的某个擦除坐标上两负载重编码值不同时，才算能区分该对。可行集合必须使每一对负载被至少两个选中窗口区分：这样即使任意一个已调度窗口整个没有回包，剩余窗口在各自请求坐标上仍能区分它们；一个窗口内有多处差异也只抵得住该窗口以外的掉包。调度保证只针对原最优负载与窗口覆盖，不承诺实际读回值符合这些负载。

在`quota`总费用内枚举窗口子集（最多16个，逐对维护覆盖计数并做不可行剪枝），选满足每对≥2覆盖的总费用最低集合；同费用选按窗口ID升序排列后字典序较小的ID列表。500位上限不枚举负载：最优负载只在数量≤32时，用前向Viterbi代价表加后向最优后缀表沿最优边回溯取得。

### 调度标识、调度单与续接资料

`schedule_id`为`rb-`加规范化完整请求SHA-256摘要前32位十六进制。规范化JSON对对象字段排序、窗口目录按ID排序，但保留有业务顺序的数组（`generators`、`received`、请求坐标）。因此等价输入（仅对象字段或窗口目录换序）产生相同标识；改接收证据、窗口目录或任何参数即产生不同标识，新资料不能当旧单使用。标识绑定发起时的完整请求，而非只绑定捕获ID或选中窗口。

`scheduled`结果另交付两份JSON：`readback_order`（接收端调度单，含`kind=telemetry_trellis.readback_order`、版本、`schedule_id`、`capture_id`、选中窗口ID与各自**精确请求坐标**及费用、总费用；不含原接收数组或候选负载）与`continuation`（调用方保存的续接资料，含`kind=telemetry_trellis.readback_continuation`、版本、标识、内嵌的完整原始请求、选中窗口ID、总费用与原距离/路径数）。`save_order(path, order)`与`save_continuation(path, continuation)`复用原子新建文件IO，拒绝覆盖已存在目标；`load_continuation(path)`重载并结构校验，返回续接字典。续接资料自含所需原始数据，因此新进程无需旧对象存活、也不查询“最新捕获”；也可向`settle_readback`另传`original_request`，缺失字段或规范化后不匹配即报`ReadbackError`。绑定用于检测误配，不抗恶意伪造。

### 回包归并、再译码与拒绝边界

回包为对象`{"schedule_id": <string>, "windows": [...]}`，窗口顺序无关，每个**选中**窗口恰一项、不多不少：`{"window_id", "available": true/false, "bits"?}`。`available=false`不得带任何位值；`available=true`必须给`bits`对象，键为该窗口调度时请求坐标的十进制字符串、值为严格0/1整数（拒绝bool），坐标集合与请求**完全相等**、每个坐标恰一次。冒用未选窗口、漏报/多报窗口、重复项、坐标越出该窗口请求、不同调度的资料（`schedule_id`不符）、续接资料被改到与自身标识不一致，全部抛`ReadbackError`。所有结构与绑定校验先于任何合并完成，任何非法数据都不产生部分结算，也不修改原输入或已保存资料。

多个成功窗口覆盖同一坐标只产生一份观测：值相同合并为一份；值冲突则拒绝整次结算，不择一、不投票。只把成功回包填入原帧对应擦除位，其余码元（含不可用窗口覆盖的位置）保持原样；结算不会写向原本已知的位。

结算返回`schedule_id`、`capture_id`、`filled_indices`（实际补入的展开下标，去重升序）、`unavailable_windows`（升序）与`decode`，后者完全是现有单帧译码证据：`distance`、精确`optimal_paths`、`ambiguous`、字典序代表`payload`、`selected_final_state`、`state_path`、`reencoded`、`corrected_indices`、`erasure_indices`。允许任意多个窗口不可用，仍用实际拿到的证据。再译码对补完观测后的**整组合法路径**重新判断，而不是只在原最优负载中筛选，因此原先不最优的路径可能进入并列，结果也不保证唯一。

## 流式遥测提前交付 API（streaming）

`new_state` 为一个尚未结束的记录建立处理状态；`append`、`replace`、`confirm` 推进接收与确认；`finish` 在收尾时给出与一次性 `process` 一致的完整单帧证据。全部为普通 JSON 数据，仅用标准库，不连接设备。每个入口都返回含新状态的报告对象，原状态不被修改；任何非法调用都在产生后继状态之前抛错，因此既不留部分改动，也不产生新交付。

### 状态建立与码元坐标

`new_state(frame_id, constraint, generators, payload_length, terminated)` 的参数范围完全沿用单帧契约：`frame_id` 为 ident（`[A-Za-z0-9_:-]{1,64}`）、`constraint` 为 2..7、`generators` 为 2..4 个不同的正整数掩码且 `<2**constraint`、`payload_length` 为 1..500、`terminated` 为严格布尔。总步数 `steps = payload_length + (constraint-1 if terminated else 0)` 与码元总数 `steps*width` 在建立时确定，后续不能超过。

一切按从 0 起展开的码元坐标操作：第 `j` 步产生码元区间 `[j*width, (j+1)*width)` 并消费信源位 `j`。只有信源位 `0..payload_length-1` 属于负载；终止记录的末尾 `constraint-1` 个零位是尾步，永不属于负载。编码沿用 `register=(state<<1)|bit`、零初态的既有约定。

### 追加、替换与确认

- `append(state, symbols)`：追加一段非空观测，起点必须等于当前已接收长度；分片可在码组内部结束，但不得越过总长。追加不隐式确认、也不交付。
- `replace(state, start, symbols)`：替换一段非空的、已接收但未确认的观测。目标区间须整体落在 `[confirmed, received_count)`；起点落在确认前缀内（即使新值相同）一律拒绝，跨确认位置也不接受只应用后半段。可在已知值与 null 之间切换。替换不隐式确认、也不交付。
- `confirm(state, confirmed)`：由调用方推进半开确认前缀的右端；不得倒退、不得超过已接收长度。确认位置不变时不会重发任何位。

`progress(state)` 返回 `received_count`、`confirmed`、`delivered`、`symbol_count` 与 `finished`，让调用方分别获知已接收长度、确认位置与已交付负载长度。

### 提前交付规则

每次确认后，只使用**全部码元都已确认的完整编码步**（数量 `K = confirmed // width`）判断可提前交付的负载。已收齐但未确认的码组、只确认了一半的码组都不参与。对走到第 `K` 步的每个可达状态，考虑到达该状态的**所有**最低代价合法前缀（不是只看全局代价最低的状态，也不是每个状态只取一个代表）。取这些前缀在负载位置上共有的最长起始段，交付其中此前没有交付的连续范围，回报里给出绝对负载起点 `start`、此次新增位 `bits` 与结束位置 `end`。这个规则既是上界也是下界：既不能猜，也不能一直积攒到结束。确认位置不变时不会重发。

规则实现的等价表述：某状态在第 `j` 步“存活”当且仅当它位于至少一条从锚点（已交付前缀唯一确定的状态）到确认边界的最低代价前缀上；从锚点前向走，只有当所有存活最低代价边都取同一位时才交付该位，遇到两种位都可取即停止。

### 结束与最终证据

`finish(state)` 仅当总长已全部收到且全部确认才允许结束，否则拒绝且不改状态。结束时按单帧口径在**完整合法路径**中判定全局最优，继续交付全部最优负载共有的最长起始段中尚未交付的连续位；若存在歧义，不跳过缺口去交付后面碰巧一致的位，也不以字典序代表填满缺口。

`finish` 报告另含 `evidence`，与对同一最终接收数组一次性调用 `process` 得到的该帧结果完全一致：`id`、字典序代表 `payload`、`selected_final_state`、精确 `optimal_paths`（Python 大整数，500 位全擦除为 `2**500`）、`ambiguous`、`state_path`、`reencoded`、`distance`、`corrected_indices` 与 `erasure_indices`。诊断中的代表只用于证据，不是交付给消费端的位。已结束状态不能再追加、替换、确认或结束。

### 已交付码元的退役与交接

一旦某个负载前缀交付，对应完整编码步的原始接收码元块即退出待保留的输入：状态里只保留已交付负载、每码元的 `match`/`mismatch`/`erased` 来源摘要、失配计数与一段短摘要哈希，不再保留原始观测，也无需重传。后续交付与结束诊断都由“锚点状态 + 已交付前缀 + 摘要”和尚未交付的原始块合成，绝不回看已退役坐标的原始观测；结束证据仍覆盖全部展开坐标，因为已交付位已唯一确定其期望码字。

`save_state(path, state)` 复用原子新建文件 IO，拒绝覆盖已存在目标；`load_state(path)` 重载并结构校验（码元布局、已交付与确认长度、分块连续性、来源摘要一致性、结束条件等），结构不符抛 `StreamingError`。采集端在任意一次成功调用后都能把 JSON 状态交给新进程继续：接手时已确认与仍可替换的边界不变，已交付位不会重发，`finish` 证据仍覆盖已退役坐标。

## 本机计量事实 API（metering）

`build_meter_facts(catalog, evidence)` 只消费上游已译码的单帧结果（`process(...)["frames"]` 中的对象，可来自多次调用），不接收原始码元、不调用译码器；`evidence_digest(meter_id, tick, frame_id, frame)` 供模拟上游发布者按同一口径生成摘要。输入目录是可信输入，校验只用于检出混入的旧结果、错置或改写的证据，不验证目录发布者，也不防目录与证据被一起改写。全部为普通 JSON 数据，仅用标准库，格式错误抛 `MeteringError`（`ValueError` 子类）并拒绝整批，不产生部分结果。

### 测点目录与帧证据

目录对象字段固定为 `inlet_meter_id`、`outlet_meter_id`、`points`：两只表 ID 均为 ident 且必须不同；每端 2..2048 个实际测点，目录总点数 4..4096，目录与证据数组均可乱序。每个测点字段固定为 `meter_id`（必须等于两个表 ID 之一）、`tick`（0..10^15 的严格整数，拒绝布尔）、`frame_id`（ident，**批内唯一**，一个帧结果只绑定一个测点）、`evidence_digest`（64 个小写十六进制字符）。同一表内同一滴答不允许两个测点。

证据数组每项只消费 `id`、`payload`、`optimal_paths`、`ambiguous` 四个字段（其他诊断字段不要求、不重验）：`id` 为 ident；`payload` 为 1..64 个严格 0/1 整数（布尔拒绝）；`optimal_paths` 为 1..2^负载长度 的严格整数（布尔拒绝，上界即负载长度次方）；`ambiguous` 必须是严格布尔且与 `optimal_paths != 1` 完全一致。未知帧（证据 `id` 不属于任何测点）、重复帧、消费字段非法均拒绝整批；绝不能把不匹配或未知证据悄悄当成缺证。

摘要是对数组 `[表ID, 本机滴答, id, payload, optimal_paths, ambiguous]` 用 Python 标准 JSON 紧凑序列化（`ensure_ascii=True, separators=(",", ":")`）、UTF-8 编码后取 SHA-256 的小写十六进制。表 ID、滴答与所属帧 ID 一律以测点目录为准，不从帧结果或数组位置推断。每份已提供的证据都必须与对应测点冻结的摘要完全一致，任一不匹配即拒绝整批。目录合法但某帧证据本次没到时不是格式错误：该端点记为缺证。

### 本机计量事实

每端按滴答排序后，**目录中实际相邻**的两个测点形成一条事实，不跳过不可读测点、不把隔点拼成区间。事实文档 `kind=telemetry_trellis.meter_facts`、`version=1`，先列入端再列出端，端内按滴答排序；每条含 `fact_id`（`inlet-fact-k`/`outlet-fact-k`，k 从 1 起）、`side`、`meter_id`、半开 `local_range`（严格正长的整数滴答区间）、两端 `endpoint_frame_ids`、两端 `endpoints`（每项 `status`/`frame_id`/`reading`）以及 `status`、`increment_ml`、`unknown_reason`。

端点三态：证据未到为 `missing_evidence`；证据已核验但 `ambiguous=true` 为 `ambiguous_evidence`，不以代表负载充当读数；仅当证据已核验且最优路径唯一时为 `readable`，负载按最高位在前解释为无符号累计毫升数（64 位上界 2^64-1）。两端均可读且后值不小于前值时 `status=known`，`increment_ml` 为差值（真零也是已知覆盖，取值 0），`unknown_reason=null`；否则 `status=unknown`、`increment_ml=null`，原因区分：任一端 `missing_evidence`、任一端 `ambiguous_evidence`、两端可读但计数下降的 `decreasing_reading`（以前端点优先取因）。不推断回绕或复位。

事实文档可不带任何锚点独立交付。`validate_facts(document)` 对重新导入的事实 JSON 做完整结构校验（种类/版本、表绑定、排序、半开区间、端点与增量一致性），不合规抛 `MeteringError`；对齐入口内部即调用它，且只读不改写本机事实。

## 参考时间对齐 API（alignment）

`align_meter_facts(facts, timing)` 使用同一批已制备事实与一份校时目录，把两只表在共同参考时间内的量对齐；同一事实可换不同校时目录重复调用，调用不需要也不接受帧证据，事实文档在任何调用后内容不变。校时格式错误抛 `AlignmentError`（`MeteringError` 子类），先于任何输出记录；合法缺证、歧义、缺少校时覆盖是数据而非错误。

### 锚点与连线

校时对象字段固定为 `inlet_meter_id`、`outlet_meter_id`、`inlet_anchors`、`outlet_anchors`，两个表 ID 必须与事实文档绑定一致。每端 0..4096 个锚点，每点为整数 `tick` 与整数 `reference_time`（同一原点的微秒，范围均为 0..10^15，拒绝布尔）加严格布尔 `interpolate`。按滴答排序后滴答列与参考时刻列都必须严格递增；第一个锚点的 `interpolate` 必须为假。标志为真表示允许从排序后前一个锚点**线性插值**到本点，由此形成的连线在其半开本机区间 `[前滴答, 本滴答)` 内有效，换算片段右端点时用同一连线公式；标志为假的连线不插值、不跨越；锚点覆盖范围之外不外推；单个锚点没有任何长度覆盖。

### 分摊、配对与未配对

每条已知本机增量按**原本机区间**长度均匀归属份额：先把原区间按可用连线切成原子片段，有映射的片段带精确参考区间，无映射的片段留在本机时间。两列已校时片段在参考时间上做归并扫描，每个非空交集生成一条 `paired` 下游事实；只有两端同时具备已知增量与映射的部分才配对，端点接触（一端区间右端等于另一端左端）不产生记录。任一侧原计量区间或所用锚点连线变化即断开。两端分别按**自己的本机子区间占原本机区间的比例**计量，绝不把跨斜率的增量直接按参考时间均摊。

每条 `paired` 记录含确定性 `record_id`（`paired-0001` 起）、精确 `reference_range`，以及两端各自的 `fact_id`、`meter_id`、`endpoint_frame_ids`、所用 `anchor_segment`（含 side/meter/`segment_index`/`anchor_index`/两端滴答与两端参考时刻）、实际占用的精确本机子区间与 `share_ml`。

未进入配对的份额分两类交付，均带原区间身份、实际本机子区间与精确份额：`uncalibrated`（未校时）只有本机范围，`reference_range` 与 `anchor_segment` 为 null；`calibrated_unpaired`（已校时但未配对）另给精确参考范围与所用连线，绝不解释为对侧零流量。未知区间不分摊、不配出零值，只在 `unknown_facts` 中原样列出（含 `unknown_reason`）。对每条已知事实，配对与未配对记录中属于它的本机子区间恰好覆盖原区间一次且无缝，份额之和精确等于原增量；真零增量同样遵循覆盖规则（份额全为 0 但覆盖不缺）。

### 精确数、顺序与复杂度

一切边界、比较与份额用精确有理数（`fractions.Fraction`），JSON 中每个有理数表达为 `{"num": 整数, "den": 正整数}`（分母为约分后的正值，整数表示为 den=1），不使用浮点、`NaN` 或 `Infinity`，因此 JSON 往返无损。输出顺序确定：配对记录按参考区间起点、终点再按两侧来源身份与本机起点排序；未配对记录先按侧（入端先于出端）、`fact_id` 再按本机起点排序，`record_id` 按该顺序连续编号（`unpaired-0001` 起）。除读取输入证据外，工作量只随输入断点与实际输出片段数增长（排序为对数开销），不逐滴答或微秒展开，也不遍历双端区间的笛卡尔积。

## 连续编码会话的联合译码 API（session）

`decode_session(request)` 处理一次“连续编码、分帧传输”的会话：采集端把一段不复位编码器状态的连续记录切成多个带独立帧 ID 的传输块，帧边界只供传输和按帧消费，编码状态原样跨帧延续。该入口是独立于 `process` 与单帧 `decode` 的离线数据 API，仅用标准库、只用合成数据；现有 `process`、单帧 `decode`、归档、返读、流式、计量与对齐行为完全不变。任何非法输入抛 `SessionError`（`ValueError` 子类），校验在译码前全部完成，拒绝时不返回任何部分帧结果，也不修改调用方输入。

### 请求结构与连续状态

请求对象字段固定为 `session_id`（ident 约束）、`constraint`（2..7 严格整数）、`generators`（2..4 个不同正整数掩码且 `<2**constraint`，顺序有意义）、`frames`（1..64 项）、严格布尔 `terminated` 与会话级 `tail_received`。帧数组顺序即编码顺序，**绝不按帧 ID 排序**；每帧字段固定为 `id`（ident，全会话唯一）、`payload_length`（1..500 严格整数）与 `received`。负载位为严格整数 0/1，观测码元为严格整数 0/1 或 null（擦除），布尔值一律拒绝；`null` 在距离计算中不计代价。

编码器会话开始时处于零状态；每帧 `received` 只对应该帧负载，长度必须恰等于 `payload_length * len(generators)`，帧内不带终止尾步。处理完一帧后状态原样延续到下一帧，帧边界不是终止或复位信号。会话负载总长不超过 20000 位；负载长度、会话总长、身份、数组长度或码元违规均拒绝整次请求。

所有帧负载结束后才应用一次会话级终止规则：`terminated=true` 时追加 `constraint-1` 个强制零尾位，`tail_received` 长度必须恰等于 `(constraint-1) * len(generators)`，且只在全会话末尾参与“最终状态为零”的约束，不在每帧后插尾或复位；`terminated=false` 时 `tail_received` 必须为空数组，最终状态不受限。尾位不属于任何帧。

### 联合译码、精确计数与帧级交付

在同一条连续状态路径上对全会话做一次 Viterbi 递推，求最小硬判汉明距离；每个可达状态保留最小度量、字典序最小的完整幸存位串与达到该最小值的**完整状态路径精确计数**（Python 大整数，不溢出、不截断、不按相同码元合并路径）。终止规则只在会话级应用：终止会话只接受零终态路径，非终止会话接受任意终态。返回全会话最小 `distance`、`optimal_paths`、`ambiguous`（`optimal_paths != 1`）与字典序最小的完整负载代表 `payload`。

随后做一次后向最优后缀扫描，对每个网格步判断全局最优路径在该步是否仍可取两种信源位；据此把完整会话的全部最优路径上的歧义归回帧。每个输入帧按请求顺序返回：对应 `id`、在拼接负载中的半开位范围 `range`（`[start,end)`）、代表负载切片 `payload` 与帧级 `ambiguous`。帧级 `ambiguous` 为真当且仅当所有全局最优会话路径在该帧的负载切片并非唯一；绝不把局部从零状态重译的结果当作帧结果，也不得因返回了一个代表就声称该帧已唯一确定。所有帧范围连续、互不重叠并恰好覆盖会话负载（首帧从 0 开始、末帧结束于总会话负载长度），终止尾位不出现在任何帧范围内。

全局与帧级歧义都从完整会话的全部最优路径判断，不枚举负载：500 位全擦除终止会话（分两个 250 位帧、尾观测全擦除）仍返回精确路径数并把两个帧都标为歧义；非终止全擦除 N 位会话的精确路径数为 `2**N`。代表负载仅作证据展示，歧义时不构成确认读数。

