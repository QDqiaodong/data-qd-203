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
